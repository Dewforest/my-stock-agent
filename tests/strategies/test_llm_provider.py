from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from stock_agent.domain import Market, Side
from stock_agent.strategies.llm_contract import (
    DecisionPhase,
    LLMDecisionRecord,
    LLMDecisionRequest,
    LLMDecisionResponse,
    LLMDecisionSelection,
    LLMDecisionStatus,
    LLMInvocationAttempt,
    StrategyAActionTarget,
    StrategyACandidateEnvelope,
    StrategyADataQuality,
    StrategyARegime,
    candidate_id_for,
    decision_id_for,
    request_fingerprint_for,
    response_digest_for,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    InvocationStart,
    LLMDecisionProvider,
    LLMProviderError,
    RawLLMResponse,
    RecordedLLMDecisionProvider,
    ReplayLLMDecisionProvider,
)

NOW = datetime(2026, 8, 6, 20, tzinfo=UTC)
PROMPT_DIGEST = "prompt-sha256:" + "a" * 64


def _candidate(symbol: str, actions: tuple[Side, ...]) -> StrategyACandidateEnvelope:
    values: dict[str, Any] = {
        "schema_version": "strategy-a-candidate/v1",
        "strategy_id": "strategy-a",
        "config_version": "strategy-a-v1",
        "market": Market.US,
        "symbol": symbol,
        "as_of": NOW,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "regime": StrategyARegime.OFFENSIVE,
        "data_quality": StrategyADataQuality.COMPLETE,
        "short_window": 2,
        "long_window": 3,
        "volume_window": 2,
        "volume_confirmation_threshold": Decimal("1"),
        "short_sum": Decimal("24"),
        "long_sum": Decimal("34"),
        "latest_close": Decimal("13"),
        "prior_volume_sum": Decimal("200"),
        "latest_volume": Decimal("250"),
        "portfolio_snapshot_id": "portfolio-snapshot-sha256:" + "b" * 64,
        "action_targets": tuple(
            StrategyAActionTarget(action=action, target_weight=Decimal("0.1"))
            for action in sorted(actions, key=list(Side).index)
        ),
        "reason_codes": ("positive-trend",),
        "evidence_ids": ("bar-sha256:" + "c" * 64,),
    }
    provisional = StrategyACandidateEnvelope.model_construct(
        **values,
        candidate_id="strategy-a-candidate-sha256:" + "0" * 64,
    )
    return StrategyACandidateEnvelope(**values, candidate_id=candidate_id_for(provisional))


def _request() -> LLMDecisionRequest:
    values: dict[str, Any] = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": "strategy-a",
        "config_version": "strategy-a-v1",
        "market": Market.US,
        "as_of": NOW,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": PROMPT_DIGEST,
        "candidates": (
            _candidate("AAPL", (Side.BUY, Side.HOLD)),
            _candidate("IBM", (Side.HOLD,)),
        ),
    }
    provisional = LLMDecisionRequest.model_construct(
        **values,
        request_fingerprint="llm-decision-request-sha256:" + "0" * 64,
    )
    return LLMDecisionRequest(
        **values,
        request_fingerprint=request_fingerprint_for(provisional),
    )


def _payload(request: LLMDecisionRequest) -> dict[str, object]:
    return {
        "schema_version": "llm-decision-response/v1",
        "request_fingerprint": request.request_fingerprint,
        "selections": [
            {
                "symbol": "AAPL",
                "action": "BUY",
                "confidence": 82,
                "thesis": "Trend and volume agree",
                "invalidation": "Trend breaks",
            },
            {
                "symbol": "IBM",
                "action": "HOLD",
                "confidence": 61,
                "thesis": "Wait for confirmation",
                "invalidation": "Setup improves",
            },
        ],
        "provider_response_id": "provider-response-1",
    }


class TransportSpy:
    def __init__(
        self,
        response: object,
        *,
        model_identity: str = "fixture/model",
        model_revision: str = "revision-1",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.model_identity = model_identity
        self.model_revision = model_revision
        self.error = error
        self.calls: list[LLMDecisionRequest] = []

    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RawLLMResponse(
            payload=self.response,
            model_identity=self.model_identity,
            model_revision=self.model_revision,
        )


class BoundarySpy:
    def __init__(self, *, attempt_id: str = "attempt-1") -> None:
        self.start = InvocationStart(attempt_id=attempt_id, started_at=NOW)
        self.end = NOW + timedelta(seconds=3)
        self.begin_calls = 0
        self.end_calls = 0

    def begin(self) -> InvocationStart:
        self.begin_calls += 1
        return self.start

    def end_at(self, invocation: InvocationStart) -> datetime:
        assert invocation is self.start
        self.end_calls += 1
        return self.end


def _policy() -> ExactModelIdentityPolicy:
    return ExactModelIdentityPolicy(
        policy_id="exact-model-v1",
        model_identity="fixture/model",
        model_revision="revision-1",
    )


def _record_provider(
    journal: object,
    transport: TransportSpy,
    boundary: BoundarySpy | None = None,
) -> RecordedLLMDecisionProvider:
    return RecordedLLMDecisionProvider(
        transport=transport,
        journal=journal,
        model_policy=_policy(),
        invocation_boundary=boundary or BoundarySpy(),
    )


def test_polluted_request_fails_before_boundary_transport_or_journal() -> None:
    valid = _request()
    polluted = LLMDecisionRequest.model_construct(
        **{**valid.model_dump(), "candidates": []}
    )
    transport = TransportSpy(_payload(valid))
    boundary = BoundarySpy()
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            _record_provider(journal, transport, boundary).decide(polluted)

        assert captured.value.code is LLMDecisionStatus.SCHEMA
        assert boundary.begin_calls == 0
        assert transport.calls == []
        assert journal.list_attempts() == ()
        assert journal.list_decisions() == ()


def test_record_mode_validates_and_atomically_journals_canonical_decision() -> None:
    request = _request()
    transport = TransportSpy(json.dumps(_payload(request)))
    boundary = BoundarySpy()
    with LLMDecisionJournal() as journal:
        provider = _record_provider(journal, transport, boundary)

        assert isinstance(provider, LLMDecisionProvider)
        record = provider.decide(request)

        response = LLMDecisionResponse(
            schema_version="llm-decision-response/v1",
            request_fingerprint=request.request_fingerprint,
            selections=record.selections,
            provider_response_id="provider-response-1",
        )
        digest = response_digest_for(response)
        assert record == LLMDecisionRecord(
            decision_id=decision_id_for(request.request_fingerprint, digest),
            request_fingerprint=request.request_fingerprint,
            response_digest=digest,
            selections=record.selections,
            config_version=request.config_version,
            model_identity_policy_id=request.model_identity_policy_id,
            prompt_template_id=request.prompt_template_id,
            prompt_template_digest=request.prompt_template_digest,
            model_identity="fixture/model",
            model_revision="revision-1",
            provider_response_id="provider-response-1",
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=3),
        )
        assert journal.list_decisions() == (record,)
        assert journal.list_attempts() == (
            LLMInvocationAttempt(
                attempt_id="attempt-1",
                request_fingerprint=request.request_fingerprint,
                status=LLMDecisionStatus.SUCCESS,
                response_digest=record.response_digest,
                decision_id=record.decision_id,
                provider_response_id=record.provider_response_id,
                started_at=record.started_at,
                ended_at=record.ended_at,
            ),
        )
    assert transport.calls == [request]
    assert (boundary.begin_calls, boundary.end_calls) == (1, 1)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda raw: raw.update(extra="forbidden"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw.pop("schema_version"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw["selections"][0].update(weight="0.1"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw["selections"][0].update(target="0.1"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw["selections"][0].update(quantity="10"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw["selections"][0].update(order="MARKET"), LLMDecisionStatus.SCHEMA),
        (lambda raw: raw["selections"].pop(), LLMDecisionStatus.ENVELOPE),
        (
            lambda raw: raw["selections"].append(dict(raw["selections"][0])),
            LLMDecisionStatus.ENVELOPE,
        ),
        (lambda raw: raw["selections"].reverse(), LLMDecisionStatus.ENVELOPE),
        (lambda raw: raw["selections"][0].update(action="SELL"), LLMDecisionStatus.ENVELOPE),
        (
            lambda raw: raw.update(request_fingerprint="llm-decision-request-sha256:" + "f" * 64),
            LLMDecisionStatus.ENVELOPE,
        ),
    ],
)
def test_record_mode_rejects_whole_batch_and_journals_only_failure(
    mutation: Any, code: LLMDecisionStatus
) -> None:
    request = _request()
    raw = _payload(request)
    mutation(raw)
    transport = TransportSpy(raw)
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            _record_provider(journal, transport).decide(request)

        assert captured.value.code is code
        assert (
            str(captured.value)
            == {
                LLMDecisionStatus.SCHEMA: "LLM response schema validation failed",
                LLMDecisionStatus.ENVELOPE: "LLM response violates candidate envelope",
            }[code]
        )
        assert captured.value.__cause__ is None
        assert journal.list_decisions() == ()
        assert tuple(item.status for item in journal.list_attempts()) == (code,)


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"schema_version":"x","schema_version":"y"}'])
def test_malformed_or_duplicate_key_json_is_schema_failure(raw: str) -> None:
    request = _request()
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            _record_provider(journal, TransportSpy(raw)).decide(request)
        assert captured.value.code is LLMDecisionStatus.SCHEMA
        assert journal.list_decisions() == ()


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (TimeoutError("credential=secret"), LLMDecisionStatus.TIMEOUT, "LLM transport timed out"),
        (RuntimeError("api_key=secret"), LLMDecisionStatus.TRANSPORT, "LLM transport failed"),
    ],
)
def test_transport_failures_are_stable_secret_free_and_journaled(
    error: Exception, code: LLMDecisionStatus, message: str
) -> None:
    request = _request()
    transport = TransportSpy({}, error=error)
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            _record_provider(journal, transport).decide(request)
        assert captured.value.code is code
        assert str(captured.value) == message
        assert "secret" not in str(captured.value)
        assert captured.value.__cause__ is None
        assert tuple(item.status for item in journal.list_attempts()) == (code,)


def test_model_identity_and_request_policy_must_match_exactly() -> None:
    request = _request()
    transport = TransportSpy(_payload(request), model_revision="revision-other")
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            _record_provider(journal, transport).decide(request)
        assert captured.value.code is LLMDecisionStatus.IDENTITY_POLICY
        assert str(captured.value) == "LLM model identity policy validation failed"
        assert journal.list_decisions() == ()
        assert journal.list_attempts()[0].status is LLMDecisionStatus.IDENTITY_POLICY


class FailingJournal:
    def __init__(self) -> None:
        self.calls = 0

    def append_attempt(
        self, attempt: LLMInvocationAttempt, *, decision: LLMDecisionRecord | None = None
    ) -> None:
        self.calls += 1
        raise RuntimeError("database password=secret")


def test_journal_failure_prevents_success_and_exposes_only_audit_error() -> None:
    request = _request()
    journal = FailingJournal()
    with pytest.raises(LLMProviderError) as captured:
        _record_provider(journal, TransportSpy(_payload(request))).decide(request)
    assert captured.value.code is LLMDecisionStatus.AUDIT_PERSISTENCE
    assert str(captured.value) == "LLM decision audit persistence failed"
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert journal.calls == 1


def test_replay_returns_original_decision_without_transport_or_new_attempt() -> None:
    request = _request()
    transport = TransportSpy(_payload(request))
    with LLMDecisionJournal() as journal:
        recorded = _record_provider(journal, transport).decide(request)
        attempts_before = journal.list_attempts()

        replayed = ReplayLLMDecisionProvider(
            journal=journal,
            model_policy=_policy(),
        ).decide(request)

        assert replayed is recorded or replayed == recorded
        assert replayed.decision_id == recorded.decision_id
        assert journal.list_decisions() == (recorded,)
        assert journal.list_attempts() == attempts_before
        assert len(transport.calls) == 1


def test_replay_missing_decision_fails_closed() -> None:
    with LLMDecisionJournal() as journal:
        with pytest.raises(LLMProviderError) as captured:
            ReplayLLMDecisionProvider(journal=journal, model_policy=_policy()).decide(_request())
        assert captured.value.code is LLMDecisionStatus.CONFLICT
        assert str(captured.value) == "LLM replay decision unavailable or conflicting"


@pytest.mark.parametrize("conflict", ["config", "prompt", "policy", "model", "selection"])
def test_replay_revalidates_provenance_identity_and_candidate_envelope(conflict: str) -> None:
    request = _request()
    selections = (
        LLMDecisionSelection(
            symbol="AAPL",
            action=Side.BUY if conflict != "selection" else Side.SELL,
            confidence=82,
            thesis="Trend and volume agree",
            invalidation="Trend breaks",
        ),
        LLMDecisionSelection(
            symbol="IBM",
            action=Side.HOLD,
            confidence=61,
            thesis="Wait",
            invalidation="Setup improves",
        ),
    )
    response = LLMDecisionResponse(
        schema_version="llm-decision-response/v1",
        request_fingerprint=request.request_fingerprint,
        selections=selections,
    )
    digest = response_digest_for(response)
    record = LLMDecisionRecord(
        decision_id=decision_id_for(request.request_fingerprint, digest),
        request_fingerprint=request.request_fingerprint,
        response_digest=digest,
        selections=selections,
        config_version="other" if conflict == "config" else request.config_version,
        model_identity_policy_id=(
            "other-policy" if conflict == "policy" else request.model_identity_policy_id
        ),
        prompt_template_id=("other-prompt" if conflict == "prompt" else request.prompt_template_id),
        prompt_template_digest=request.prompt_template_digest,
        model_identity="other/model" if conflict == "model" else "fixture/model",
        model_revision="revision-1",
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
    )
    attempt = LLMInvocationAttempt(
        attempt_id="seed-attempt",
        request_fingerprint=request.request_fingerprint,
        status=LLMDecisionStatus.SUCCESS,
        response_digest=digest,
        decision_id=record.decision_id,
        started_at=record.started_at,
        ended_at=record.ended_at,
    )
    with LLMDecisionJournal() as journal:
        journal.append_attempt(attempt, decision=record)
        with pytest.raises(LLMProviderError) as captured:
            ReplayLLMDecisionProvider(journal=journal, model_policy=_policy()).decide(request)
        expected = (
            LLMDecisionStatus.IDENTITY_POLICY
            if conflict in {"policy", "model"}
            else LLMDecisionStatus.ENVELOPE
            if conflict == "selection"
            else LLMDecisionStatus.CONFLICT
        )
        assert captured.value.code is expected
        assert journal.list_decisions() == (record,)
        assert journal.list_attempts() == (attempt,)
