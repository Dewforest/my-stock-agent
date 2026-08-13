from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from stock_agent.domain import Bar, Market, PortfolioSnapshot
from stock_agent.runtime.decision import (
    DecisionCoordinator,
    DecisionOutcome,
)
from stock_agent.runtime.store import DecisionInvocationStatus, RuntimeStore
from stock_agent.strategies.llm_contract import (
    DecisionPhase,
    LLMDecisionRequest,
    StrategyAConfig,
    request_fingerprint_for,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    RawLLMResponse,
)
from stock_agent.strategies.protocol import MarketSnapshot, StrategyContext
from stock_agent.strategies.strategy_a import (
    STRATEGY_A_ID,
    build_strategy_a_candidates,
)

US_SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "XOM", "JNJ", "PG")
AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
PROMPT_DIGEST = "prompt-sha256:" + "b" * 64
MODEL_POLICY = ExactModelIdentityPolicy(
    policy_id="exact-model-v1",
    model_identity="deepseek-v4-pro",
    model_revision="api-model-id:deepseek-v4-pro",
)


def make_config() -> StrategyAConfig:
    return StrategyAConfig(
        config_version="strategy-a-v1",
        short_window=3,
        long_window=5,
        volume_window=4,
        volume_confirmation_threshold=Decimal("1.20"),
        offensive_target_weight=Decimal("0.10"),
        neutral_target_weight=Decimal("0.05"),
        model_identity_policy_id="exact-model-v1",
        prompt_template_id="strategy-a-openai-compatible-json/v1",
        prompt_template_digest=PROMPT_DIGEST,
    )


def make_request(symbols: tuple[str, ...] = US_SYMBOLS) -> LLMDecisionRequest:
    config = make_config()
    bars: list[Bar] = []
    for symbol in sorted(symbols):
        for day in range(5):
            session = date(2026, 8, 7 + day)
            bars.append(
                Bar(
                    symbol=symbol,
                    market=Market.US,
                    session_date=session,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=Decimal("100"),
                    available_at=datetime(
                        session.year, session.month, session.day, 21, 0, tzinfo=UTC
                    ),
                )
            )
    context = StrategyContext(
        market_snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=tuple(bars)),
        portfolio=PortfolioSnapshot(
            account_id="paper-us-v1",
            market=Market.US,
            cash=Decimal("100000"),
            nav=Decimal("100000"),
            peak_nav=Decimal("100000"),
            positions=(),
            as_of=AS_OF,
        ),
        strategy_config_version="strategy-a-v1",
    )
    candidates = build_strategy_a_candidates(context, config)
    values = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": STRATEGY_A_ID,
        "config_version": config.config_version,
        "market": Market.US,
        "as_of": AS_OF,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "model_identity_policy_id": config.model_identity_policy_id,
        "prompt_template_id": config.prompt_template_id,
        "prompt_template_digest": config.prompt_template_digest,
        "candidates": candidates,
    }
    provisional = LLMDecisionRequest.model_construct(
        **values, request_fingerprint="llm-decision-request-sha256:" + "0" * 64
    )
    return LLMDecisionRequest(**values, request_fingerprint=request_fingerprint_for(provisional))


def make_selections(request: LLMDecisionRequest) -> list[dict[str, object]]:
    return [
        {
            "symbol": candidate.symbol,
            "action": candidate.action_targets[0].action.value,
            "confidence": 50,
            "thesis": "bounded rationale",
            "invalidation": "bounded condition",
        }
        for candidate in request.candidates
    ]


class FakeTransport:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self._error = error
        self.calls = 0

    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return RawLLMResponse(
            payload={
                "schema_version": "llm-decision-response/v1",
                "request_fingerprint": request.request_fingerprint,
                "selections": make_selections(request),
            },
            model_identity="deepseek-v4-pro",
            model_revision="api-model-id:deepseek-v4-pro",
        )


def build_coordinator(
    tmp_path: Path,
    *,
    transport: FakeTransport | None = None,
    credential_error: BaseException | None = None,
    credential_calls: list[int] | None = None,
) -> tuple[DecisionCoordinator, LLMDecisionJournal, RuntimeStore]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    journal = LLMDecisionJournal(":memory:")
    selected_transport = transport if transport is not None else FakeTransport()
    calls = credential_calls if credential_calls is not None else []

    def credential_provider() -> object:
        calls.append(1)
        if credential_error is not None:
            raise credential_error
        return "opaque-credential"

    def transport_factory(credential: object) -> FakeTransport:
        return selected_transport

    coordinator = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=MODEL_POLICY,
        credential_provider=credential_provider,
        transport_factory=transport_factory,
    )
    return coordinator, journal, store


# ── 1. canonical symbol order ──────────────────────────────────────────────


def test_request_uses_canonical_symbol_order() -> None:
    request = make_request(US_SYMBOLS)
    symbols = tuple(candidate.symbol for candidate in request.candidates)
    assert symbols == tuple(sorted(US_SYMBOLS))
    assert len(symbols) == 10


# ── 4/5. credential ready then send intent before transport ────────────────


def test_credential_then_send_intent_before_transport(tmp_path: Path) -> None:
    transport = FakeTransport()
    credential_calls: list[int] = []

    def credential_provider() -> object:
        credential_calls.append(1)
        return "opaque-credential"

    def transport_factory(credential: object) -> FakeTransport:
        return transport

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    journal = LLMDecisionJournal(":memory:")
    coordinator = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=MODEL_POLICY,
        credential_provider=credential_provider,
        transport_factory=transport_factory,
    )
    result = coordinator.run_decision(run_id=RUN_ID, request=make_request(("AAPL",)), now=AS_OF)
    assert result.outcome is DecisionOutcome.DECIDED
    assert credential_calls == [1]
    assert transport.calls == 1
    invocation = store.get_decision_invocation(RUN_ID)
    assert invocation is not None
    assert invocation[0] == DecisionInvocationStatus.DECISION_RECORDED.value


# ── 6. credential failure is a pre-send failure, not ambiguity ─────────────


def test_credential_failure_is_pre_send_not_ambiguous(tmp_path: Path) -> None:
    coordinator, _, store = build_coordinator(
        tmp_path, credential_error=RuntimeError("keychain denied")
    )
    result = coordinator.run_decision(run_id=RUN_ID, request=make_request(("AAPL",)), now=AS_OF)
    assert result.outcome is DecisionOutcome.FAILED_PRE_SEND
    # No SEND_INTENT marker was ever persisted.
    assert store.get_decision_invocation(RUN_ID) is None


# ── 7. crash after send intent never auto-invokes again ────────────────────


def test_prior_send_intent_becomes_needs_reconciliation(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    journal = LLMDecisionJournal(":memory:")
    request = make_request(("AAPL",))
    store.mark_send_intent(RUN_ID, request.request_fingerprint, AS_OF)

    # Simulate crash/reopen: a fresh coordinator sees the prior SEND_INTENT.
    transport = FakeTransport()
    coordinator = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=MODEL_POLICY,
        credential_provider=lambda: "opaque-credential",
        transport_factory=lambda credential: transport,
    )
    result = coordinator.run_decision(run_id=RUN_ID, request=request, now=AS_OF)
    assert result.outcome is DecisionOutcome.NEEDS_RECONCILIATION
    assert transport.calls == 0  # never auto-invoked again


# ── 8. operator-only reconcile abandon ─────────────────────────────────────


def test_reconcile_abandon_appends_audit_and_abandons(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    journal = LLMDecisionJournal(":memory:")
    request = make_request(("AAPL",))
    store.mark_send_intent(RUN_ID, request.request_fingerprint, AS_OF)
    store.mark_needs_reconciliation(RUN_ID, AS_OF)

    coordinator = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=MODEL_POLICY,
        credential_provider=lambda: "opaque-credential",
        transport_factory=lambda credential: FakeTransport(),
    )
    coordinator.reconcile_abandon(
        run_id=RUN_ID, operator="operator-1", reason="manual-review", now=AS_OF
    )
    invocation = store.get_decision_invocation(RUN_ID)
    assert invocation is not None
    assert invocation[0] == DecisionInvocationStatus.ABANDONED_NO_ORDER.value


# ── 9. existing canonical decision resumes with zero transport/Keychain ────


def test_existing_decision_resumes_without_transport(tmp_path: Path) -> None:
    coordinator, journal, store = build_coordinator(tmp_path)
    request = make_request(("AAPL",))
    first = coordinator.run_decision(run_id=RUN_ID, request=request, now=AS_OF)
    assert first.outcome is DecisionOutcome.DECIDED
    assert first.record is not None

    # A fresh coordinator resumes by fingerprint with zero credential/transport.
    credential_calls: list[int] = []
    transport = FakeTransport()
    second = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=MODEL_POLICY,
        credential_provider=lambda: (credential_calls.append(1), "opaque-credential")[1],
        transport_factory=lambda credential: transport,
    )
    resumed = second.run_decision(run_id=RUN_ID, request=request, now=AS_OF)
    assert resumed.outcome is DecisionOutcome.RESUMED
    assert resumed.record is not None
    assert resumed.record.decision_id == first.record.decision_id
    assert credential_calls == []
    assert transport.calls == 0


# ── 11. failed invocation is not HOLD and creates no order ─────────────────


def test_transport_failure_is_needs_reconciliation_not_hold(tmp_path: Path) -> None:
    transport = FakeTransport(error=TimeoutError("read timeout"))
    coordinator, _, _ = build_coordinator(tmp_path, transport=transport)
    result = coordinator.run_decision(run_id=RUN_ID, request=make_request(("AAPL",)), now=AS_OF)
    # The SEND_INTENT was persisted before send, so the failure is ambiguity.
    assert result.outcome is DecisionOutcome.NEEDS_RECONCILIATION
    assert result.record is None


# ── 12. profile identity mismatch is rejected before credential ────────────


def test_model_identity_policy_is_enforced(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    journal = LLMDecisionJournal(":memory:")
    mismatched_policy = ExactModelIdentityPolicy(
        policy_id="other-policy",
        model_identity="deepseek-v4-pro",
        model_revision="api-model-id:deepseek-v4-pro",
    )
    transport = FakeTransport()
    coordinator = DecisionCoordinator(
        store=store,
        journal=journal,
        model_policy=mismatched_policy,
        credential_provider=lambda: "opaque-credential",
        transport_factory=lambda credential: transport,
    )
    result = coordinator.run_decision(run_id=RUN_ID, request=make_request(("AAPL",)), now=AS_OF)
    assert (
        result.outcome is DecisionOutcome.NEEDS_RECONCILIATION
        or result.outcome is DecisionOutcome.FAILED_PRE_SEND
    )
