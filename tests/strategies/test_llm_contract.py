import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ConfigDict, ValidationError

from stock_agent.domain import Market, PortfolioSnapshot, Position, Side
from stock_agent.strategies.llm_contract import (
    DecisionPhase,
    LLMDecisionRecord,
    LLMDecisionRequest,
    LLMDecisionResponse,
    LLMDecisionSelection,
    LLMDecisionStatus,
    LLMInvocationAttempt,
    LLMInvocationMode,
    LLMRunAttestation,
    StrategyAActionTarget,
    StrategyACandidateEnvelope,
    StrategyAConfig,
    StrategyADataQuality,
    StrategyARegime,
    candidate_id_for,
    decision_id_for,
    portfolio_snapshot_id_for,
    request_fingerprint_for,
    response_digest_for,
)


def literal_digest(tag: str, fields: list[object]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{tag}-sha256:{hashlib.sha256(payload).hexdigest()}"


AS_OF = datetime(2026, 7, 31, 20, tzinfo=UTC)


class TupleSubclass(tuple[object, ...]):
    pass


def candidate_values(**overrides: object) -> dict[str, object]:
    portfolio_id = "portfolio-snapshot-sha256:" + "b" * 64
    evidence_id = "bar-sha256:" + "c" * 64
    values: dict[str, object] = {
        "schema_version": "strategy-a-candidate/v1",
        "strategy_id": "strategy-a-bounded-llm",
        "config_version": "strategy-a-v1",
        "market": Market.US,
        "symbol": "IBM",
        "as_of": AS_OF,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "regime": StrategyARegime.OFFENSIVE,
        "data_quality": StrategyADataQuality.COMPLETE,
        "short_window": 3,
        "long_window": 5,
        "volume_window": 4,
        "volume_confirmation_threshold": Decimal("1.2"),
        "short_sum": Decimal("330"),
        "long_sum": Decimal("530"),
        "latest_close": Decimal("112"),
        "prior_volume_sum": Decimal("4000"),
        "latest_volume": Decimal("1300"),
        "portfolio_snapshot_id": portfolio_id,
        "action_targets": (
            StrategyAActionTarget(action=Side.BUY, target_weight=Decimal("0.1")),
            StrategyAActionTarget(action=Side.HOLD, target_weight=Decimal("0")),
        ),
        "reason_codes": ("positive-trend", "volume-confirmed"),
        "evidence_ids": (evidence_id,),
    }
    symbol = str(overrides.get("symbol", "IBM"))
    values["candidate_id"] = literal_digest(
        "strategy-a-candidate",
        [
            "strategy-a-candidate/v1",
            "strategy-a-bounded-llm",
            "strategy-a-v1",
            "US",
            symbol,
            "2026-07-31T20:00:00.000000Z",
            "POST_CLOSE",
            "OFFENSIVE",
            "COMPLETE",
            3,
            5,
            4,
            "12e-1",
            "33e1",
            "53e1",
            "112e0",
            "4e3",
            "13e2",
            portfolio_id,
            [["BUY", "1e-1"], ["HOLD", "0"]],
            ["positive-trend", "volume-confirmed"],
            [evidence_id],
        ],
    )
    values.update(overrides)
    return values


def request_values(candidate: StrategyACandidateEnvelope, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": candidate.strategy_id,
        "config_version": candidate.config_version,
        "market": Market.US,
        "as_of": AS_OF,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": "prompt-sha256:" + "a" * 64,
        "candidates": (candidate,),
    }
    values["request_fingerprint"] = literal_digest(
        "llm-decision-request",
        [
            "llm-decision-request/v1",
            "strategy-a-bounded-llm",
            "strategy-a-v1",
            "US",
            "2026-07-31T20:00:00.000000Z",
            "POST_CLOSE",
            "exact-model-v1",
            "strategy-a-decision-v1",
            "prompt-sha256:" + "a" * 64,
            [candidate.candidate_id],
        ],
    )
    values.update(overrides)
    return values


def selection(symbol: str = "IBM") -> LLMDecisionSelection:
    return LLMDecisionSelection(
        symbol=symbol,
        action=Side.BUY,
        confidence=80,
        thesis="Trend and volume agree",
        invalidation="Trend loses long-window support",
    )


def config_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "config_version": "strategy-a-v1",
        "short_window": 3,
        "long_window": 5,
        "volume_window": 4,
        "volume_confirmation_threshold": Decimal("1.20"),
        "offensive_target_weight": Decimal("0.10"),
        "neutral_target_weight": Decimal("0.05"),
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": "prompt-sha256:" + "a" * 64,
    }
    values.update(overrides)
    return values


def test_strategy_a_config_is_exact_frozen_and_derives_required_history() -> None:
    config = StrategyAConfig(**config_values())

    assert tuple(StrategyAConfig.model_fields) == (
        "config_version",
        "short_window",
        "long_window",
        "volume_window",
        "volume_confirmation_threshold",
        "offensive_target_weight",
        "neutral_target_weight",
        "model_identity_policy_id",
        "prompt_template_id",
        "prompt_template_digest",
    )
    assert config.required_history == 5
    assert config.volume_confirmation_threshold == Decimal("1.20")
    with pytest.raises(ValidationError):
        config.short_window = 2
    with pytest.raises(ValidationError):
        StrategyAConfig(**config_values(extra=True))
    with pytest.raises(TypeError):
        config.model_copy(update={"long_window": 6})


@pytest.mark.parametrize(
    "overrides",
    [
        {"short_window": 0},
        {"short_window": True},
        {"long_window": 3},
        {"volume_window": 0},
        {"volume_confirmation_threshold": Decimal("NaN")},
        {"volume_confirmation_threshold": Decimal("-0.01")},
        {"volume_confirmation_threshold": 1.2},
        {"offensive_target_weight": Decimal("0")},
        {"offensive_target_weight": Decimal("1.01")},
        {"neutral_target_weight": Decimal("-0.01")},
        {"neutral_target_weight": Decimal("0.11")},
        {"prompt_template_digest": "a" * 64},
        {"config_version": "   "},
        {"model_identity_policy_id": ""},
    ],
)
def test_strategy_a_config_rejects_invalid_boundaries(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StrategyAConfig(**config_values(**overrides))


def test_successful_decision_contract_is_exact_and_linked() -> None:
    candidate = StrategyACandidateEnvelope(**candidate_values())
    request = LLMDecisionRequest(**request_values(candidate))
    chosen = selection()
    response = LLMDecisionResponse(
        schema_version="llm-decision-response/v1",
        request_fingerprint=request.request_fingerprint,
        selections=(chosen,),
        provider_response_id="response-1",
    )
    response_digest = response_digest_for(response)
    record = LLMDecisionRecord(
        decision_id=decision_id_for(request.request_fingerprint, response_digest),
        request_fingerprint=request.request_fingerprint,
        response_digest=response_digest,
        selections=response.selections,
        config_version=request.config_version,
        model_identity_policy_id=request.model_identity_policy_id,
        prompt_template_id=request.prompt_template_id,
        prompt_template_digest=request.prompt_template_digest,
        model_identity="recorded-fixture",
        model_revision="v1",
        provider_response_id=response.provider_response_id,
        started_at=AS_OF,
        ended_at=AS_OF,
    )

    assert request.candidates == (candidate,)
    assert response.request_fingerprint == request.request_fingerprint
    assert record.selections == response.selections
    assert record.decision_id.startswith("llm-decision-sha256:")


def test_attempts_and_run_attestations_are_separate_from_canonical_decisions() -> None:
    instant = datetime(2026, 7, 31, 20, tzinfo=UTC)
    request_fingerprint = "llm-decision-request-sha256:" + "e" * 64
    response_digest = "llm-decision-response-sha256:" + "1" * 64
    decision_id = literal_digest(
        "llm-decision", [request_fingerprint, response_digest]
    )

    success = LLMInvocationAttempt(
        attempt_id="attempt-1",
        request_fingerprint=request_fingerprint,
        status=LLMDecisionStatus.SUCCESS,
        response_digest=response_digest,
        decision_id=decision_id,
        provider_response_id="response-1",
        started_at=instant,
        ended_at=instant,
    )
    timeout = LLMInvocationAttempt(
        attempt_id="attempt-2",
        request_fingerprint=request_fingerprint,
        status=LLMDecisionStatus.TIMEOUT,
        response_digest=None,
        decision_id=None,
        provider_response_id=None,
        started_at=instant,
        ended_at=instant,
    )
    replay = LLMRunAttestation(
        attestation_id="attestation-1",
        execution_label="backtest-replay-1",
        invocation_mode=LLMInvocationMode.REPLAY,
        decision_ids=(decision_id,),
        occurred_at=instant,
    )

    assert success.decision_id == decision_id
    assert timeout.decision_id is None
    assert replay.decision_ids == (decision_id,)
    assert "decision_id" not in LLMRunAttestation.model_fields

    with pytest.raises(ValidationError):
        LLMInvocationAttempt(
            attempt_id="attempt-3",
            request_fingerprint=request_fingerprint,
            status=LLMDecisionStatus.TRANSPORT,
            response_digest=None,
            decision_id=decision_id,
            provider_response_id=None,
            started_at=instant,
            ended_at=instant,
        )


def test_canonical_helpers_match_independently_built_literal_json_bytes() -> None:
    candidate = StrategyACandidateEnvelope(**candidate_values())
    request = LLMDecisionRequest(**request_values(candidate))
    response = LLMDecisionResponse(
        schema_version="llm-decision-response/v1",
        request_fingerprint=request.request_fingerprint,
        selections=(selection(),),
        provider_response_id="transport-only",
    )
    expected_response_digest = literal_digest(
        "llm-decision-response",
        [
            "llm-decision-response/v1",
            request.request_fingerprint,
            [["IBM", "BUY", 80, "Trend and volume agree", "Trend loses long-window support"]],
        ],
    )
    expected_decision_id = literal_digest(
        "llm-decision",
        [request.request_fingerprint, expected_response_digest],
    )
    portfolio = PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("900.00"),
        nav=Decimal("1000.00"),
        peak_nav=Decimal("1100.00"),
        as_of=datetime(2026, 7, 31, 16, tzinfo=timezone(timedelta(hours=-4))),
        positions=(
            Position(
                symbol="IBM",
                quantity=Decimal("2.00"),
                average_cost=Decimal("45.00"),
                market_value=Decimal("100.00"),
            ),
        ),
    )
    expected_portfolio_id = literal_digest(
        "portfolio-snapshot",
        [
            "account-1",
            "US",
            "9e2",
            "1e3",
            "11e2",
            "2026-07-31T20:00:00.000000Z",
            [["IBM", "2e0", "45e0", "1e2"]],
        ],
    )

    assert candidate_id_for(candidate) == candidate.candidate_id
    assert request_fingerprint_for(request) == request.request_fingerprint
    assert response_digest_for(response) == expected_response_digest
    assert (
        decision_id_for(request.request_fingerprint, expected_response_digest)
        == expected_decision_id
    )
    assert portfolio_snapshot_id_for(portfolio) == expected_portfolio_id


def test_portfolio_snapshot_helper_requires_exact_revalidated_symbol_sorted_input() -> None:
    aapl = Position(
        symbol="AAPL",
        quantity=Decimal("1"),
        average_cost=Decimal("40"),
        market_value=Decimal("50"),
    )
    ibm = Position(
        symbol="IBM",
        quantity=Decimal("1"),
        average_cost=Decimal("40"),
        market_value=Decimal("50"),
    )
    unsorted = PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("900"),
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        positions=(ibm, aapl),
        as_of=AS_OF,
    )
    with pytest.raises(ValueError, match="symbol-sorted"):
        portfolio_snapshot_id_for(unsorted)

    polluted = PortfolioSnapshot.model_construct(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("900"),
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        positions=[],
        as_of=AS_OF,
    )
    with pytest.raises(ValueError, match="exact tuple"):
        portfolio_snapshot_id_for(polluted)

    class MutablePortfolio(PortfolioSnapshot):
        model_config = ConfigDict(frozen=False)

    subclass = MutablePortfolio(**unsorted.model_dump())
    with pytest.raises(TypeError, match="exact PortfolioSnapshot"):
        portfolio_snapshot_id_for(subclass)


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (
            StrategyACandidateEnvelope,
            candidate_values(candidate_id="strategy-a-candidate-sha256:" + "0" * 64),
        ),
    ],
)
def test_candidate_rejects_a_supplied_id_that_does_not_match_content(
    model: type[StrategyACandidateEnvelope], values: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="candidate_id"):
        model(**values)


def test_request_rejects_a_supplied_fingerprint_that_does_not_match_content() -> None:
    candidate = StrategyACandidateEnvelope(**candidate_values())
    with pytest.raises(ValidationError, match="request_fingerprint"):
        LLMDecisionRequest(
            **request_values(
                candidate,
                request_fingerprint="llm-decision-request-sha256:" + "0" * 64,
            )
        )


def test_record_rejects_a_supplied_decision_id_that_does_not_match_digests() -> None:
    request_fingerprint = "llm-decision-request-sha256:" + "e" * 64
    response_digest = literal_digest(
        "llm-decision-response",
        [
            "llm-decision-response/v1",
            request_fingerprint,
            [["IBM", "BUY", 80, "Trend and volume agree", "Trend loses long-window support"]],
        ],
    )
    with pytest.raises(ValidationError, match="decision_id"):
        LLMDecisionRecord(
            decision_id="llm-decision-sha256:" + "0" * 64,
            request_fingerprint=request_fingerprint,
            response_digest=response_digest,
            selections=(selection(),),
            config_version="strategy-a-v1",
            model_identity_policy_id="exact-model-v1",
            prompt_template_id="strategy-a-decision-v1",
            prompt_template_digest="prompt-sha256:" + "a" * 64,
            model_identity="provider/model",
            model_revision="revision-1",
            started_at=AS_OF,
            ended_at=AS_OF,
        )


def test_record_rejects_a_response_digest_for_different_selections() -> None:
    request_fingerprint = "llm-decision-request-sha256:" + "e" * 64
    selected = selection()
    wrong_response_digest = literal_digest(
        "llm-decision-response",
        [
            "llm-decision-response/v1",
            request_fingerprint,
            [["IBM", "HOLD", 80, selected.thesis, selected.invalidation]],
        ],
    )

    with pytest.raises(ValidationError, match="response_digest"):
        LLMDecisionRecord(
            decision_id=decision_id_for(request_fingerprint, wrong_response_digest),
            request_fingerprint=request_fingerprint,
            response_digest=wrong_response_digest,
            selections=(selected,),
            config_version="strategy-a-v1",
            model_identity_policy_id="exact-model-v1",
            prompt_template_id="strategy-a-decision-v1",
            prompt_template_digest="prompt-sha256:" + "a" * 64,
            model_identity="provider/model",
            model_revision="revision-1",
            started_at=AS_OF,
            ended_at=AS_OF,
        )


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (
            StrategyACandidateEnvelope,
            candidate_values(schema_version="strategy-a-candidate/v2"),
        ),
        (
            LLMDecisionRequest,
            request_values(
                StrategyACandidateEnvelope(**candidate_values()),
                schema_version="llm-decision-request/v2",
            ),
        ),
        (
            LLMDecisionResponse,
            {
                "schema_version": "llm-decision-response/v2",
                "request_fingerprint": "llm-decision-request-sha256:" + "e" * 64,
                "selections": (selection(),),
            },
        ),
    ],
)
def test_contract_schema_versions_are_exact(
    model: type[object], values: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model(**values)  # type: ignore[operator]


@pytest.mark.parametrize(
    "field",
    ["target", "target_weight", "weight", "quantity", "order", "api_key"],
)
def test_response_forbids_unknown_and_authority_fields(field: str) -> None:
    values: dict[str, object] = {
        "schema_version": "llm-decision-response/v1",
        "request_fingerprint": "llm-decision-request-sha256:" + "e" * 64,
        "selections": (selection(),),
        field: Decimal("0.5"),
    }
    with pytest.raises(ValidationError):
        LLMDecisionResponse(**values)


def test_response_has_only_exact_fields_and_requires_echoed_fingerprint() -> None:
    assert tuple(LLMDecisionResponse.model_fields) == (
        "schema_version",
        "request_fingerprint",
        "selections",
        "provider_response_id",
    )
    with pytest.raises(ValidationError):
        LLMDecisionResponse(
            schema_version="llm-decision-response/v1",
            selections=(selection(),),
        )


@pytest.mark.parametrize(
    ("model", "field", "bad_value"),
    [
        (StrategyACandidateEnvelope, "action_targets", []),
        (StrategyACandidateEnvelope, "reason_codes", ["positive-trend"]),
        (StrategyACandidateEnvelope, "evidence_ids", ["bar-sha256:" + "c" * 64]),
        (LLMDecisionRequest, "candidates", []),
        (LLMDecisionResponse, "selections", []),
        (LLMDecisionRecord, "selections", []),
        (LLMRunAttestation, "decision_ids", []),
    ],
)
def test_collection_fields_require_exact_tuples(
    model: type[object], field: str, bad_value: object
) -> None:
    candidate = StrategyACandidateEnvelope(**candidate_values())
    request = LLMDecisionRequest(**request_values(candidate))
    response_digest = response_digest_for(
        LLMDecisionResponse(
            schema_version="llm-decision-response/v1",
            request_fingerprint=request.request_fingerprint,
            selections=(selection(),),
        )
    )
    values_by_model: dict[type[object], dict[str, object]] = {
        StrategyACandidateEnvelope: candidate_values(),
        LLMDecisionRequest: request_values(candidate),
        LLMDecisionResponse: {
            "schema_version": "llm-decision-response/v1",
            "request_fingerprint": request.request_fingerprint,
            "selections": (selection(),),
        },
        LLMDecisionRecord: {
            "decision_id": decision_id_for(request.request_fingerprint, response_digest),
            "request_fingerprint": request.request_fingerprint,
            "response_digest": response_digest,
            "selections": (selection(),),
            "config_version": "strategy-a-v1",
            "model_identity_policy_id": "exact-model-v1",
            "prompt_template_id": "strategy-a-decision-v1",
            "prompt_template_digest": "prompt-sha256:" + "a" * 64,
            "model_identity": "provider/model",
            "model_revision": "revision-1",
            "started_at": AS_OF,
            "ended_at": AS_OF,
        },
        LLMRunAttestation: {
            "attestation_id": "attestation-1",
            "execution_label": "replay-1",
            "invocation_mode": LLMInvocationMode.REPLAY,
            "decision_ids": (decision_id_for(request.request_fingerprint, response_digest),),
            "occurred_at": AS_OF,
        },
    }
    values = values_by_model[model]
    values[field] = bad_value
    with pytest.raises(ValidationError, match="exact tuple"):
        model(**values)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (StrategyACandidateEnvelope, "reason_codes"),
        (LLMDecisionRequest, "candidates"),
        (LLMDecisionResponse, "selections"),
        (LLMRunAttestation, "decision_ids"),
    ],
)
def test_collection_fields_reject_tuple_subclasses(
    model: type[object], field: str
) -> None:
    candidate = StrategyACandidateEnvelope(**candidate_values())
    values_by_model: dict[type[object], dict[str, object]] = {
        StrategyACandidateEnvelope: candidate_values(),
        LLMDecisionRequest: request_values(candidate),
        LLMDecisionResponse: {
            "schema_version": "llm-decision-response/v1",
            "request_fingerprint": "llm-decision-request-sha256:" + "e" * 64,
            "selections": (selection(),),
        },
        LLMRunAttestation: {
            "attestation_id": "attestation-1",
            "execution_label": "replay-1",
            "invocation_mode": LLMInvocationMode.REPLAY,
            "decision_ids": ("llm-decision-sha256:" + "f" * 64,),
            "occurred_at": AS_OF,
        },
    }
    values = values_by_model[model]
    values[field] = TupleSubclass(values[field])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="exact tuple"):
        model(**values)  # type: ignore[operator]


def test_nested_models_are_exact_and_constructed_pollution_is_revalidated() -> None:
    with pytest.raises(TypeError, match="does not support subclasses"):
        type("MutableTarget", (StrategyAActionTarget,), {"model_config": ConfigDict(frozen=False)})

    polluted_target = StrategyAActionTarget.model_construct(
        action=Side.BUY,
        target_weight=[],
    )
    with pytest.raises(ValidationError):
        StrategyACandidateEnvelope(
            **candidate_values(action_targets=(polluted_target,))
        )

    candidate = StrategyACandidateEnvelope(**candidate_values())
    polluted_candidate = StrategyACandidateEnvelope.model_construct(
        **{**candidate.model_dump(), "action_targets": []}
    )
    with pytest.raises(ValidationError):
        LLMDecisionRequest(**request_values(polluted_candidate))

    polluted_selection = LLMDecisionSelection.model_construct(
        symbol="IBM", action=Side.BUY, confidence=[], thesis="x", invalidation="y"
    )
    with pytest.raises(ValidationError):
        LLMDecisionResponse(
            schema_version="llm-decision-response/v1",
            request_fingerprint="llm-decision-request-sha256:" + "e" * 64,
            selections=(polluted_selection,),
        )


@pytest.mark.parametrize(
    "field",
    [
        "volume_confirmation_threshold",
        "short_sum",
        "long_sum",
        "latest_close",
        "prior_volume_sum",
        "latest_volume",
    ],
)
def test_candidate_rejects_float_decimals(field: str) -> None:
    with pytest.raises(ValidationError):
        StrategyACandidateEnvelope(**candidate_values(**{field: 1.0}))


@pytest.mark.parametrize("as_of", [datetime(2026, 7, 31, 20), "2026-07-31T20:00:00Z"])
def test_contract_timestamps_are_strict_and_timezone_aware(as_of: object) -> None:
    with pytest.raises(ValidationError):
        StrategyACandidateEnvelope(**candidate_values(as_of=as_of))


def test_sorted_unique_collections_are_enforced() -> None:
    evidence_a = "bar-sha256:" + "a" * 64
    evidence_b = "bar-sha256:" + "b" * 64
    with pytest.raises(ValidationError, match="evidence IDs"):
        StrategyACandidateEnvelope(**candidate_values(evidence_ids=(evidence_b, evidence_a)))
    with pytest.raises(ValidationError, match="reason codes"):
        StrategyACandidateEnvelope(**candidate_values(reason_codes=("z", "a")))
    buy = StrategyAActionTarget(action=Side.BUY, target_weight=Decimal("0.1"))
    with pytest.raises(ValidationError, match="action targets"):
        StrategyACandidateEnvelope(**candidate_values(action_targets=(buy, buy)))

    candidate = StrategyACandidateEnvelope(**candidate_values())
    other = StrategyACandidateEnvelope(**candidate_values(symbol="AAPL"))
    with pytest.raises(ValidationError, match="candidates"):
        LLMDecisionRequest(**request_values(candidate, candidates=(candidate, other)))
    with pytest.raises(ValidationError, match="candidates"):
        LLMDecisionRequest(**request_values(candidate, candidates=(candidate, candidate)))
    with pytest.raises(ValidationError, match="selections"):
        LLMDecisionResponse(
            schema_version="llm-decision-response/v1",
            request_fingerprint="llm-decision-request-sha256:" + "e" * 64,
            selections=(selection("IBM"), selection("AAPL")),
        )


def test_attempt_outcomes_and_attestation_ids_are_consistent_and_canonical() -> None:
    request_fingerprint = "llm-decision-request-sha256:" + "e" * 64
    response_digest = "llm-decision-response-sha256:" + "1" * 64
    decision_id = decision_id_for(request_fingerprint, response_digest)
    base_attempt: dict[str, object] = {
        "attempt_id": "attempt-1",
        "request_fingerprint": request_fingerprint,
        "status": LLMDecisionStatus.SUCCESS,
        "response_digest": response_digest,
        "decision_id": decision_id,
        "started_at": AS_OF,
        "ended_at": AS_OF,
    }
    with pytest.raises(ValidationError, match="successful attempts"):
        LLMInvocationAttempt(**{**base_attempt, "response_digest": None})
    with pytest.raises(ValidationError, match="decision_id"):
        LLMInvocationAttempt(
            **{
                **base_attempt,
                "decision_id": "llm-decision-sha256:" + "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="failed attempts"):
        LLMInvocationAttempt(**{**base_attempt, "status": LLMDecisionStatus.TIMEOUT})
    with pytest.raises(ValidationError):
        LLMInvocationAttempt(**{**base_attempt, "selections": (selection(),)})

    attestation: dict[str, object] = {
        "attestation_id": "attestation-1",
        "execution_label": "replay-1",
        "invocation_mode": LLMInvocationMode.REPLAY,
        "decision_ids": (decision_id,),
        "occurred_at": AS_OF,
    }
    with pytest.raises(ValidationError, match="sorted"):
        LLMRunAttestation(
            **{
                **attestation,
                "decision_ids": ("llm-decision-sha256:" + "f" * 64, decision_id),
            }
        )
    with pytest.raises(ValidationError, match="unique"):
        LLMRunAttestation(**{**attestation, "decision_ids": (decision_id, decision_id)})
    with pytest.raises(ValidationError):
        LLMRunAttestation(**{**attestation, "decision_id": decision_id})


def test_request_and_record_link_exact_prompt_config_and_model_identity_fields() -> None:
    assert tuple(LLMDecisionRequest.model_fields) == (
        "schema_version",
        "strategy_id",
        "config_version",
        "market",
        "as_of",
        "decision_phase",
        "model_identity_policy_id",
        "prompt_template_id",
        "prompt_template_digest",
        "candidates",
        "request_fingerprint",
    )
    assert tuple(LLMDecisionRecord.model_fields)[4:11] == (
        "config_version",
        "model_identity_policy_id",
        "prompt_template_id",
        "prompt_template_digest",
        "model_identity",
        "model_revision",
        "provider_response_id",
    )
