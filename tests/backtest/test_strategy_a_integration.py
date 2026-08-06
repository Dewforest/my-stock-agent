from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

import stock_agent.backtest.runner as runner_module
from stock_agent.account import OpenExecutionBatchBooked, PortfolioLedger
from stock_agent.backtest import BacktestSession, BacktestSpec, ChronologicalBacktestRunner
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Currency, Instrument, Market, Side
from stock_agent.execution import ExecutionSimulator, FillStatus
from stock_agent.market import TradingCalendar
from stock_agent.risk import RiskDecisionStatus, RiskEngine
from stock_agent.strategies.llm_contract import (
    LLMDecisionRequest,
    LLMInvocationMode,
    LLMRunAttestation,
    StrategyAConfig,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    InvocationStart,
    RawLLMResponse,
    RecordedLLMDecisionProvider,
    ReplayLLMDecisionProvider,
)
from stock_agent.strategies.strategy_a import BoundedLLMStrategyA, StrategyAAdapterError

DATES = tuple(date(2026, 8, day) for day in (3, 4, 5, 6, 7))
OPEN = (Decimal("8"), Decimal("9"), Decimal("10"), Decimal("10"), Decimal("12"))
CLOSE = (Decimal("8"), Decimal("9"), Decimal("10"), Decimal("10"), Decimal("12"))
VOLUME = (Decimal("100"), Decimal("100"), Decimal("200"), Decimal("300"), Decimal("100"))
PROMPT_DIGEST = "prompt-sha256:" + "a" * 64


def _instant(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _bar(
    day: date,
    open_price: Decimal,
    close_price: Decimal,
    volume: Decimal,
    *,
    available_hour: int = 21,
) -> Bar:
    return Bar(
        symbol="AAPL",
        market=Market.US,
        session_date=day,
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        volume=volume,
        available_at=_instant(day, available_hour),
    )


def _fixture(
    *, changed_third_close: Decimal | None = None, session_count: int = 5
) -> tuple[PointInTimeStore, TradingCalendar, BacktestSpec]:
    store = PointInTimeStore()
    closes = list(CLOSE)
    if changed_third_close is not None:
        closes[2] = changed_third_close
    for day, open_price, close_price, volume in zip(
        DATES, OPEN, closes, VOLUME, strict=True
    ):
        close_at = _instant(day, 21)
        store.append_bar(
            _bar(day, open_price, close_price, volume),
            ingested_at=close_at + timedelta(seconds=1),
            source="strategy-a-fixture",
            source_record_id=f"AAPL-{day.isoformat()}",
        )
    sessions = tuple(
        BacktestSession(
            session_date=day,
            open_at=_instant(day, 14),
            close_at=_instant(day, 21),
            open_bars=(
                _bar(
                    day,
                    open_price,
                    open_price,
                    Decimal(0),
                    available_hour=14,
                ),
            ),
        )
        for day, open_price in zip(DATES[:session_count], OPEN[:session_count], strict=True)
    )
    return (
        store,
        TradingCalendar(Market.US, DATES),
        BacktestSpec(
            run_id="strategy-a-integration",
            account_id="account-1",
            market=Market.US,
            initial_cash=Decimal("1000"),
            instruments=(
                Instrument(
                    symbol="AAPL",
                    market=Market.US,
                    currency=Currency.USD,
                    sector="Technology",
                ),
            ),
            sessions=sessions,
            strategy_config_version="strategy-a-v1",
        ),
    )


def _config(*, offensive_target: str = "0.10") -> StrategyAConfig:
    return StrategyAConfig(
        config_version="strategy-a-v1",
        short_window=2,
        long_window=3,
        volume_window=2,
        volume_confirmation_threshold=Decimal("1"),
        offensive_target_weight=Decimal(offensive_target),
        neutral_target_weight=Decimal("0.05"),
        model_identity_policy_id="exact-model-v1",
        prompt_template_id="strategy-a-decision-v1",
        prompt_template_digest=PROMPT_DIGEST,
    )


def _policy() -> ExactModelIdentityPolicy:
    return ExactModelIdentityPolicy(
        policy_id="exact-model-v1",
        model_identity="fixture/model",
        model_revision="revision-1",
    )


class RequestAwareTransport:
    def __init__(self, *, fail_on_buy: bool = False) -> None:
        self.fail_on_buy = fail_on_buy
        self.calls: list[LLMDecisionRequest] = []

    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse:
        self.calls.append(request)
        selections = []
        for candidate in request.candidates:
            actions = tuple(target.action for target in candidate.action_targets)
            action = (
                Side.BUY
                if Side.BUY in actions
                else Side.HOLD
                if Side.HOLD in actions
                else actions[0]
            )
            if self.fail_on_buy and action is Side.BUY:
                raise TimeoutError("fixture secret must not escape")
            selections.append(
                {
                    "symbol": candidate.symbol,
                    "action": action.value,
                    "confidence": 91,
                    "thesis": f"bounded-{action.value.lower()}",
                    "invalidation": "fixture boundary",
                }
            )
        return RawLLMResponse(
            payload={
                "schema_version": "llm-decision-response/v1",
                "request_fingerprint": request.request_fingerprint,
                "selections": selections,
                "provider_response_id": f"response-{len(self.calls)}",
            },
            model_identity="fixture/model",
            model_revision="revision-1",
        )


class DeterministicBoundary:
    def __init__(self) -> None:
        self.count = 0

    def begin(self) -> InvocationStart:
        self.count += 1
        return InvocationStart(
            attempt_id=f"attempt-{self.count}",
            started_at=datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=2 * self.count),
        )

    def end_at(self, invocation: InvocationStart) -> datetime:
        return invocation.started_at + timedelta(seconds=1)


def _strategy(config: StrategyAConfig, provider: object) -> BoundedLLMStrategyA:
    return BoundedLLMStrategyA(
        config=config,
        provider=provider,  # type: ignore[arg-type]
        model_identity_policy_id=config.model_identity_policy_id,
        prompt_template_id=config.prompt_template_id,
        prompt_template_digest=config.prompt_template_digest,
    )


def _recorded_strategy(
    journal: LLMDecisionJournal,
    transport: RequestAwareTransport,
    *,
    config: StrategyAConfig | None = None,
) -> BoundedLLMStrategyA:
    selected = config or _config()
    return _strategy(
        selected,
        RecordedLLMDecisionProvider(
            transport=transport,
            journal=journal,
            model_policy=_policy(),
            invocation_boundary=DeterministicBoundary(),
        ),
    )


def _runner(store: PointInTimeStore, calendar: TradingCalendar, strategy: object):
    return ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=strategy,  # type: ignore[arg-type]
        risk_engine=RiskEngine(),
    )


def test_record_then_fresh_replay_is_identical_and_preserves_trade_lifecycle(tmp_path) -> None:
    database = tmp_path / "strategy-a.duckdb"
    store, calendar, spec = _fixture()
    transport = RequestAwareTransport()
    with LLMDecisionJournal(database) as journal:
        recorded = _runner(store, calendar, _recorded_strategy(journal, transport)).run(spec)
        attempts = journal.list_attempts()
        decisions = journal.list_decisions()
        attested_ids = tuple(sorted(item.decision_id for item in decisions))
        journal.append_attestation(
            LLMRunAttestation(
                attestation_id="record-run",
                execution_label="record integration run",
                invocation_mode=LLMInvocationMode.RECORD,
                decision_ids=attested_ids,
                occurred_at=datetime(2031, 1, 1, tzinfo=UTC),
            )
        )

        decision_day = recorded.sessions[2]
        execution_day = recorded.sessions[3]
        assert decision_day.execution_results == ()
        assert decision_day.intents[0].side is Side.BUY
        assert tuple(item.status for item in decision_day.submission_results) == (
            FillStatus.PENDING,
        )
        assert execution_day.execution_results[0].status is FillStatus.FILLED
        assert execution_day.execution_results[0].price == Decimal("10")
        assert execution_day.execution_results[0].filled_quantity == Decimal("10.000000000000")
        assert execution_day.portfolio_snapshot.cash == Decimal("900.000000000000")
        assert execution_day.portfolio_snapshot.positions[0].quantity == Decimal("10.000000000000")
        assert execution_day.portfolio_snapshot.nav == Decimal("1000.000000000000")
        assert recorded.final_snapshot.cash == Decimal("900.000000000000")
        assert recorded.final_snapshot.positions[0].market_value == Decimal("120.000000000000")
        assert recorded.final_snapshot.nav == Decimal("1020.000000000000")
        assert recorded.final_lots[0].quantity == Decimal("10.000000000000")
        trade_events = tuple(
            event
            for event in recorded.ledger_events
            if type(event) is OpenExecutionBatchBooked
        )
        assert len(trade_events) == 1

        decision_ids = tuple(
            evidence_id
            for session in recorded.sessions
            for intent in session.intents
            for evidence_id in intent.evidence_ids
            if evidence_id.startswith("llm-decision-sha256:")
        )
        assert decision_ids
        assert all(journal.decision_by_id(decision_id) is not None for decision_id in decision_ids)
        assert len(transport.calls) == len(attempts) == len(decisions) == 4
        assert len({attempt.attempt_id for attempt in attempts}) == 4

    store.close()
    replay_store, replay_calendar, replay_spec = _fixture()
    with LLMDecisionJournal(database) as reopened:
        replay = ReplayLLMDecisionProvider(journal=reopened, model_policy=_policy())
        replayed = _runner(replay_store, replay_calendar, _strategy(_config(), replay)).run(
            replay_spec
        )
        assert tuple(session.intents for session in replayed.sessions) == tuple(
            session.intents for session in recorded.sessions
        )
        assert replayed.model_dump_json() == recorded.model_dump_json()
        assert replayed == recorded
        assert reopened.list_attempts() == attempts
        assert reopened.list_decisions() == decisions
        reopened.append_attestation(
            LLMRunAttestation(
                attestation_id="replay-run",
                execution_label="replay integration run",
                invocation_mode=LLMInvocationMode.REPLAY,
                decision_ids=attested_ids,
                occurred_at=datetime(2032, 1, 1, tzinfo=UTC),
            )
        )
        attestations = reopened.list_attestations()
        assert tuple(item.invocation_mode for item in attestations) == (
            LLMInvocationMode.RECORD,
            LLMInvocationMode.REPLAY,
        )
        assert attestations[0].occurred_at != attestations[1].occurred_at
        serialized = replayed.model_dump_json()
        assert "started_at" not in serialized
        assert "ended_at" not in serialized
        assert "attestation" not in serialized
    assert len(transport.calls) == 4
    replay_store.close()


def test_public_risk_engine_clamps_strategy_a_target_before_submission(tmp_path) -> None:
    store, calendar, spec = _fixture(session_count=4)
    transport = RequestAwareTransport()
    with LLMDecisionJournal(tmp_path / "clamp.duckdb") as journal:
        result = _runner(
            store,
            calendar,
            _recorded_strategy(journal, transport, config=_config(offensive_target="0.50")),
        ).run(spec)

    decision = result.sessions[2]
    assert decision.intents[0].target_weight == Decimal("0.50")
    assert decision.risk_decisions[0].status is RiskDecisionStatus.CLAMPED
    assert decision.risk_decisions[0].approved_target_weight == Decimal("0.15")
    assert "SINGLE_STOCK_MAX_15" in decision.risk_decisions[0].rule_ids
    assert decision.submission_results[0].requested_quantity == Decimal("15.000000000000")
    assert result.final_lots[0].quantity == Decimal("15.000000000000")
    store.close()


def test_provider_failure_aborts_without_pending_order_or_trade_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, calendar, spec = _fixture()
    transport = RequestAwareTransport(fail_on_buy=True)
    ledgers: list[PortfolioLedger] = []
    simulators: list[ExecutionSimulator] = []
    real_ledger = PortfolioLedger
    real_simulator = ExecutionSimulator

    def tracking_ledger(account_id: str, market: Market) -> PortfolioLedger:
        value = real_ledger(account_id, market)
        ledgers.append(value)
        return value

    def tracking_simulator(*args: object, **kwargs: object) -> ExecutionSimulator:
        value = real_simulator(*args, **kwargs)  # type: ignore[arg-type]
        simulators.append(value)
        return value

    monkeypatch.setattr(runner_module, "PortfolioLedger", tracking_ledger)
    monkeypatch.setattr(runner_module, "ExecutionSimulator", tracking_simulator)
    with LLMDecisionJournal(tmp_path / "failure.duckdb") as journal:
        with pytest.raises(StrategyAAdapterError, match="Strategy A bounded decision failed"):
            _runner(store, calendar, _recorded_strategy(journal, transport)).run(spec)
        assert tuple(item.status.value for item in journal.list_attempts()) == (
            "SUCCESS",
            "SUCCESS",
            "TIMEOUT",
        )
        assert len(journal.list_decisions()) == 2

    assert simulators[0].pending_order_ids == ()
    assert not any(type(event) is OpenExecutionBatchBooked for event in ledgers[0].events)
    store.close()


def test_changed_frozen_market_data_conflicts_in_replay_before_trade(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "changed-data.duckdb"
    store, calendar, spec = _fixture()
    with LLMDecisionJournal(database) as journal:
        _runner(
            store,
            calendar,
            _recorded_strategy(journal, RequestAwareTransport()),
        ).run(spec)
        attempts = journal.list_attempts()
        decisions = journal.list_decisions()
    store.close()

    changed_store, changed_calendar, changed_spec = _fixture(
        changed_third_close=Decimal("10.5")
    )
    ledgers: list[PortfolioLedger] = []
    real_ledger = PortfolioLedger

    def tracking_ledger(account_id: str, market: Market) -> PortfolioLedger:
        value = real_ledger(account_id, market)
        ledgers.append(value)
        return value

    monkeypatch.setattr(runner_module, "PortfolioLedger", tracking_ledger)
    with LLMDecisionJournal(database) as reopened:
        strategy = _strategy(
            _config(), ReplayLLMDecisionProvider(journal=reopened, model_policy=_policy())
        )
        with pytest.raises(StrategyAAdapterError, match="Strategy A bounded decision failed"):
            _runner(changed_store, changed_calendar, strategy).run(changed_spec)
        assert reopened.list_attempts() == attempts
        assert reopened.list_decisions() == decisions

    assert not any(type(event) is OpenExecutionBatchBooked for event in ledgers[0].events)
    changed_store.close()
