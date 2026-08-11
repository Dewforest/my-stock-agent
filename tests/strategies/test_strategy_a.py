from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest
from pydantic import ValidationError

from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, Side, StrategyIntent
from stock_agent.strategies.llm_contract import (
    LLMDecisionRecord,
    LLMDecisionRequest,
    LLMDecisionResponse,
    LLMDecisionSelection,
    StrategyAConfig,
    decision_id_for,
    request_fingerprint_for,
    response_digest_for,
)
from stock_agent.strategies.llm_provider import LLMDecisionProvider
from stock_agent.strategies.protocol import MarketSnapshot, Strategy, StrategyContext
from stock_agent.strategies.strategy_a import (
    STRATEGY_A_ID,
    BoundedLLMStrategyA,
    StrategyAAdapterError,
)

AS_OF = datetime(2026, 8, 6, 20, tzinfo=UTC)
STARTED_AT = AS_OF + timedelta(seconds=1)
ENDED_AT = AS_OF + timedelta(seconds=2)
PROMPT_DIGEST = "prompt-sha256:" + "a" * 64


def config(**overrides: object) -> StrategyAConfig:
    values: dict[str, object] = {
        "config_version": "strategy-a-v1",
        "short_window": 2,
        "long_window": 3,
        "volume_window": 2,
        "volume_confirmation_threshold": Decimal("1"),
        "offensive_target_weight": Decimal("0.10"),
        "neutral_target_weight": Decimal("0.05"),
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": PROMPT_DIGEST,
    }
    values.update(overrides)
    return StrategyAConfig(**values)  # type: ignore[arg-type]


def bar(day: int, close: str, volume: str, symbol: str) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        market=Market.US,
        session_date=date(2026, 8, day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
        available_at=datetime(2026, 8, day, 20, tzinfo=UTC),
    )


def context(*bars: Bar, version: str = "strategy-a-v1") -> StrategyContext:
    return StrategyContext(
        market_snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=tuple(bars)),
        portfolio=PortfolioSnapshot(
            account_id="account-1",
            market=Market.US,
            cash=Decimal("1000"),
            nav=Decimal("1000"),
            peak_nav=Decimal("1000"),
            positions=(),
            as_of=AS_OF,
        ),
        strategy_config_version=version,
    )


def two_symbol_context() -> StrategyContext:
    return context(
        bar(3, "20", "100", "AAPL"),
        bar(4, "21", "100", "AAPL"),
        bar(5, "23", "250", "AAPL"),
        bar(3, "10", "100", "IBM"),
        bar(4, "11", "100", "IBM"),
        bar(5, "13", "250", "IBM"),
    )


def selections_for(
    request: LLMDecisionRequest,
    actions: dict[str, Side] | None = None,
) -> tuple[LLMDecisionSelection, ...]:
    selected = actions or {}
    return tuple(
        LLMDecisionSelection(
            symbol=candidate.symbol,
            action=selected.get(candidate.symbol, candidate.action_targets[0].action),
            confidence=87 if candidate.symbol == "AAPL" else 63,
            thesis=f"thesis-{candidate.symbol}",
            invalidation=f"invalidation-{candidate.symbol}",
        )
        for candidate in request.candidates
    )


def record_for(
    request: LLMDecisionRequest,
    *,
    selections: tuple[LLMDecisionSelection, ...] | None = None,
    **overrides: object,
) -> LLMDecisionRecord:
    chosen = selections or selections_for(request)
    response = LLMDecisionResponse(
        schema_version="llm-decision-response/v1",
        request_fingerprint=request.request_fingerprint,
        selections=chosen,
    )
    digest = response_digest_for(response)
    values: dict[str, object] = {
        "decision_id": decision_id_for(request.request_fingerprint, digest),
        "request_fingerprint": request.request_fingerprint,
        "response_digest": digest,
        "selections": chosen,
        "config_version": request.config_version,
        "model_identity_policy_id": request.model_identity_policy_id,
        "prompt_template_id": request.prompt_template_id,
        "prompt_template_digest": request.prompt_template_digest,
        "model_identity": "fixture/model",
        "model_revision": "revision-1",
        "started_at": STARTED_AT,
        "ended_at": ENDED_AT,
    }
    values.update(overrides)
    return LLMDecisionRecord(**values)  # type: ignore[arg-type]


class ProviderSpy:
    def __init__(self, decide: Callable[[LLMDecisionRequest], LLMDecisionRecord]) -> None:
        self._decide = decide
        self.calls: list[LLMDecisionRequest] = []

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord:
        self.calls.append(request)
        return self._decide(request)


def strategy(
    provider: LLMDecisionProvider,
    configured: StrategyAConfig | None = None,
) -> BoundedLLMStrategyA:
    selected_config = configured or config()
    return BoundedLLMStrategyA(
        config=selected_config,
        provider=provider,
        model_identity_policy_id="exact-model-v1",
        prompt_template_id="strategy-a-decision-v1",
        prompt_template_digest=PROMPT_DIGEST,
    )


def test_strategy_has_exact_runtime_contract_and_immutable_identity() -> None:
    provider = ProviderSpy(record_for)
    configured = config()
    subject = strategy(provider, configured)

    assert isinstance(subject, Strategy)
    assert subject.strategy_id == STRATEGY_A_ID
    assert subject.config_version == "strategy-a-v1"
    assert subject.config is configured
    assert subject.provider is provider
    assert not hasattr(subject, "__dict__")
    with pytest.raises(FrozenInstanceError):
        subject.strategy_id = "changed"
    with pytest.raises(FrozenInstanceError):
        subject.config_version = "changed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_identity_policy_id", "other-policy"),
        ("prompt_template_id", "other-template"),
        ("prompt_template_digest", "prompt-sha256:" + "b" * 64),
    ],
)
def test_constructor_requires_exact_config_provenance(field: str, value: str) -> None:
    kwargs = {
        "config": config(),
        "provider": ProviderSpy(record_for),
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": PROMPT_DIGEST,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match="provenance"):
        BoundedLLMStrategyA(**kwargs)  # type: ignore[arg-type]


def test_empty_candidate_batch_returns_exact_empty_tuple_without_provider_call() -> None:
    provider = ProviderSpy(record_for)

    assert strategy(provider).evaluate(context()) == ()
    assert provider.calls == []


def test_builds_canonical_frozen_request_and_maps_only_envelope_owned_targets() -> None:
    def decide(request: LLMDecisionRequest) -> LLMDecisionRecord:
        assert type(request) is LLMDecisionRequest
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
        assert request.model_identity_policy_id == "exact-model-v1"
        assert request.prompt_template_id == "strategy-a-decision-v1"
        assert request.prompt_template_digest == PROMPT_DIGEST
        assert request.request_fingerprint == request_fingerprint_for(request)
        assert tuple(candidate.symbol for candidate in request.candidates) == ("AAPL", "IBM")
        assert all(
            type(candidate) is type(request.candidates[0]) for candidate in request.candidates
        )
        with pytest.raises(ValidationError):
            request.candidates[0].symbol = "MUTATED"
        return record_for(
            request,
            selections=selections_for(request, {"AAPL": Side.HOLD, "IBM": Side.BUY}),
        )

    provider = ProviderSpy(decide)
    intents = strategy(provider).evaluate(two_symbol_context())

    assert tuple(item.symbol for item in intents) == ("AAPL", "IBM")
    assert tuple((item.side, item.target_weight) for item in intents) == (
        (Side.HOLD, Decimal("0")),
        (Side.BUY, Decimal("0.10")),
    )
    assert tuple((item.confidence, item.thesis, item.invalidation) for item in intents) == (
        (87, "thesis-AAPL", "invalidation-AAPL"),
        (63, "thesis-IBM", "invalidation-IBM"),
    )
    request = provider.calls[0]
    record = record_for(
        request,
        selections=selections_for(request, {"AAPL": Side.HOLD, "IBM": Side.BUY}),
    )
    for intent, candidate in zip(intents, request.candidates, strict=True):
        assert intent.evidence_ids == tuple(
            sorted(
                {
                    *candidate.evidence_ids,
                    candidate.portfolio_snapshot_id,
                    candidate.candidate_id,
                    record.decision_id,
                }
            )
        )
        assert type(intent) is StrategyIntent


def test_insufficient_candidate_cannot_be_promoted_to_buy() -> None:
    evaluation = context(bar(5, "10", "100", "IBM"))

    def malicious(request: LLMDecisionRequest) -> LLMDecisionRecord:
        selection = LLMDecisionSelection(
            symbol="IBM",
            action=Side.BUY,
            confidence=100,
            thesis="malicious",
            invalidation="none",
        )
        return record_for(request, selections=(selection,))

    with pytest.raises(StrategyAAdapterError, match="Strategy A bounded decision failed"):
        strategy(ProviderSpy(malicious)).evaluate(evaluation)


@pytest.mark.parametrize(
    "malicious_record",
    [
        lambda request: record_for(request, config_version="other-version"),
        lambda request: record_for(request, model_identity_policy_id="other-policy"),
        lambda request: record_for(request, prompt_template_id="other-template"),
        lambda request: record_for(
            request,
            prompt_template_digest="prompt-sha256:" + "b" * 64,
        ),
        lambda request: LLMDecisionRecord.model_construct(
            **{
                **record_for(request).__dict__,
                "request_fingerprint": "llm-decision-request-sha256:" + "f" * 64,
            }
        ),
        lambda request: LLMDecisionRecord.model_construct(
            **{
                **record_for(request).__dict__,
                "selections": tuple(reversed(record_for(request).selections)),
            }
        ),
    ],
)
def test_malformed_or_mismatched_provider_record_fails_atomically(
    malicious_record: Callable[[LLMDecisionRequest], LLMDecisionRecord],
) -> None:
    provider = ProviderSpy(malicious_record)

    with pytest.raises(StrategyAAdapterError) as captured:
        strategy(provider).evaluate(two_symbol_context())

    assert str(captured.value) == "Strategy A bounded decision failed"
    assert "fixture/model" not in str(captured.value)
    assert len(provider.calls) == 1


def test_provider_or_journal_failure_is_normalized_without_leaking_secrets() -> None:
    secret = "api-key-super-secret"

    def fail(_: LLMDecisionRequest) -> LLMDecisionRecord:
        raise RuntimeError(secret)

    with pytest.raises(StrategyAAdapterError) as captured:
        strategy(ProviderSpy(fail)).evaluate(two_symbol_context())

    assert str(captured.value) == "Strategy A bounded decision failed"
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("pollute", ["context", "config"])
def test_polluted_inputs_fail_before_provider_invocation(pollute: str) -> None:
    provider = ProviderSpy(record_for)
    configured = config()
    evaluation = two_symbol_context()
    if pollute == "context":
        evaluation = StrategyContext.model_construct(
            **{**evaluation.__dict__, "strategy_config_version": []}
        )
    else:
        configured = StrategyAConfig.model_construct(
            **{**configured.__dict__, "short_window": "2"}
        )
    subject = strategy(provider)
    if pollute == "config":
        object.__setattr__(subject, "config", configured)

    with pytest.raises((TypeError, ValueError, ValidationError)):
        subject.evaluate(evaluation)

    assert provider.calls == []


def test_context_config_version_mismatch_fails_before_provider_invocation() -> None:
    provider = ProviderSpy(record_for)

    with pytest.raises(ValueError, match="versions"):
        strategy(provider).evaluate(context(version="other-version"))

    assert provider.calls == []


def test_repeated_and_concurrent_calls_with_fixed_record_are_byte_identical() -> None:
    evaluation = two_symbol_context()
    seed_provider = ProviderSpy(record_for)
    expected = strategy(seed_provider).evaluate(evaluation)
    fixed_record = record_for(seed_provider.calls[0])

    class FixedProvider:
        def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord:
            assert request.request_fingerprint == fixed_record.request_fingerprint
            return fixed_record

    subject = strategy(FixedProvider())
    repeated = subject.evaluate(evaluation)
    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent = tuple(executor.map(subject.evaluate, (evaluation,) * 8))

    def encoded(intents: tuple[StrategyIntent, ...]) -> bytes:
        return b"\n".join(item.model_dump_json().encode() for item in intents)

    expected_bytes = encoded(expected)
    assert encoded(repeated) == expected_bytes
    assert all(encoded(result) == expected_bytes for result in concurrent)


def test_adapter_is_independent_of_hostile_ambient_decimal_context() -> None:
    market = two_symbol_context().market_snapshot
    evaluation = StrategyContext(
        market_snapshot=market,
        portfolio=PortfolioSnapshot(
            account_id="account-1",
            market=Market.US,
            cash=Decimal("1.2345"),
            nav=Decimal("3.5801"),
            peak_nav=Decimal("3.5801"),
            positions=(
                Position(
                    symbol="IBM",
                    quantity=Decimal("1"),
                    average_cost=Decimal("2.3456"),
                    market_value=Decimal("2.3456"),
                ),
            ),
            as_of=AS_OF,
        ),
        strategy_config_version="strategy-a-v1",
    )
    seed = ProviderSpy(record_for)
    expected = strategy(seed).evaluate(evaluation)
    fixed = record_for(seed.calls[0])

    class FixedProvider:
        def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord:
            assert request.request_fingerprint == fixed.request_fingerprint
            return fixed

    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        actual = strategy(FixedProvider()).evaluate(evaluation)

    assert actual == expected
