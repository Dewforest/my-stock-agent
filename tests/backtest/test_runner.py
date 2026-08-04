from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal, Inexact, getcontext, localcontext
from typing import Any

import pytest

import stock_agent.backtest.runner as runner_module
from stock_agent.account import (
    CashInitialized,
    OpenExecutionBatchBooked,
    PortfolioLedger,
    PortfolioMarked,
)
from stock_agent.backtest import BacktestSession, BacktestSpec, ChronologicalBacktestRunner
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Currency, Instrument, Market, Side, StrategyIntent
from stock_agent.execution import ExecutionSimulator, FillStatus
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.market import TradingCalendar
from stock_agent.risk import RiskEngine
from stock_agent.strategies import StrategyContext

DATES = tuple(date(2026, 7, day) for day in (20, 21, 22, 23, 24))
OPEN_PRICES = (Decimal("100"), Decimal("110"), Decimal("120"), Decimal("125"), Decimal("130"))
CLOSE_PRICES = (Decimal("100"), Decimal("120"), Decimal("130"), Decimal("140"), Decimal("125"))


@dataclass(frozen=True)
class ScriptedStrategy:
    strategy_id: str = "five-day-script"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        session_date = max(bar.session_date for bar in context.market_snapshot.bars)
        index = DATES.index(session_date)
        side, target = (
            (Side.BUY, Decimal("0.15"))
            if index == 0
            else (Side.SELL, Decimal(0))
            if index == 3
            else (Side.HOLD, Decimal(0))
        )
        return (
            StrategyIntent(
                strategy_id=self.strategy_id,
                symbol="AAPL",
                market=Market.US,
                side=side,
                target_weight=target,
                confidence=100,
                as_of=context.market_snapshot.as_of,
                thesis="fixed five-day script",
                invalidation="fixture only",
            ),
        )


class CountingRiskEngine(RiskEngine):
    def __init__(self) -> None:
        self.contexts: list[tuple[str, object]] = []

    def assess_portfolio(self, context):  # type: ignore[no-untyped-def]
        self.contexts.append(("assess", context))
        return super().assess_portfolio(context)

    def evaluate_many(self, intents, context):  # type: ignore[no-untyped-def]
        self.contexts.append(("evaluate", context))
        return super().evaluate_many(intents, context)


class StrategyIntentSubclass(StrategyIntent):
    pass


class TupleSubclass(tuple):
    pass


class CaseStrategy:
    strategy_id = "case-strategy"
    config_version = "v1"

    def __init__(self, case: str) -> None:
        self.case = case
        self.calls = 0

    def evaluate(self, context: StrategyContext) -> Any:
        self.calls += 1
        intent = StrategyIntent(
            strategy_id=self.strategy_id,
            symbol="AAPL",
            market=Market.US,
            side=Side.HOLD,
            target_weight=Decimal(0),
            confidence=100,
            as_of=context.market_snapshot.as_of,
            thesis="validation fixture",
            invalidation="validation fixture",
        )
        if self.case == "list":
            return [intent]
        if self.case == "tuple-subclass-output":
            return TupleSubclass((intent,))
        if self.case == "tuple-subclass":
            return (StrategyIntentSubclass.model_validate(intent.model_dump()),)
        if self.case == "polluted":
            return (
                StrategyIntent.model_construct(
                    strategy_id=self.strategy_id,
                    symbol="AAPL",
                    market=Market.US,
                    side=Side.HOLD,
                    target_weight=Decimal(0),
                    confidence=100,
                    as_of=context.market_snapshot.as_of,
                    thesis="validation fixture",
                ),
            )
        updates: dict[str, object] = {
            "strategy-id": {"strategy_id": "other"},
            "market": {"market": Market.CN},
            "as-of": {"as_of": context.market_snapshot.as_of + timedelta(seconds=1)},
            "symbol": {"symbol": "MSFT"},
        }.get(self.case, {})
        payload = {
            name: getattr(intent, name) for name in StrategyIntent.model_fields
        }
        payload.update(updates)
        candidate = StrategyIntent.model_validate(payload, strict=True)
        if self.case == "duplicate":
            return (candidate, candidate)
        if self.case == "empty":
            return ()
        if self.case == "equivalent-as-of":
            equivalent = context.market_snapshot.as_of.astimezone(
                timezone(timedelta(hours=5))
            )
            payload["as_of"] = equivalent
            return (StrategyIntent.model_validate(payload, strict=True),)
        return (candidate,)


class CountingScriptedStrategy:
    strategy_id = "five-day-script"
    config_version = "v1"

    def __init__(self) -> None:
        self.calls = 0
        self._delegate = ScriptedStrategy()

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        self.calls += 1
        return self._delegate.evaluate(context)


class MutableIdentityStrategy:
    def __init__(self) -> None:
        self.identity_reads = {"strategy_id": 0, "config_version": 0}
        self.raise_on_identity_read = False
        self._delegate = ScriptedStrategy()

    @property
    def strategy_id(self) -> str:
        self.identity_reads["strategy_id"] += 1
        if self.raise_on_identity_read:
            raise AssertionError("strategy_id must be frozen")
        return "five-day-script"

    @property
    def config_version(self) -> str:
        self.identity_reads["config_version"] += 1
        if self.raise_on_identity_read:
            raise AssertionError("config_version must be frozen")
        return "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        return self._delegate.evaluate(context)


def _instant(session_date: date, hour: int) -> datetime:
    return datetime(session_date.year, session_date.month, session_date.day, hour, tzinfo=UTC)


def _bar(session_date: date, price: Decimal, *, at: datetime, volume: Decimal) -> Bar:
    return Bar(
        symbol="AAPL",
        market=Market.US,
        session_date=session_date,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        available_at=at,
    )


def _fixture(
    *, missing_close_dates: tuple[date, ...] = ()
) -> tuple[PointInTimeStore, TradingCalendar, BacktestSpec]:
    calendar = TradingCalendar(Market.US, DATES)
    sessions = tuple(
        BacktestSession(
            session_date=session_date,
            open_at=_instant(session_date, 14),
            close_at=_instant(session_date, 21),
            open_bars=(
                _bar(
                    session_date,
                    open_price,
                    at=_instant(session_date, 14),
                    volume=Decimal(0),
                ),
            ),
        )
        for session_date, open_price in zip(DATES, OPEN_PRICES, strict=True)
    )
    spec = BacktestSpec(
        run_id="five-day-run",
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
        strategy_config_version="v1",
    )
    store = PointInTimeStore()
    for session_date, open_price, close_price in zip(
        DATES, OPEN_PRICES, CLOSE_PRICES, strict=True
    ):
        if session_date in missing_close_dates:
            continue
        close_at = _instant(session_date, 21)
        store.append_bar(
            Bar(
                symbol="AAPL",
                market=Market.US,
                session_date=session_date,
                open=open_price,
                high=max(open_price, close_price),
                low=min(open_price, close_price),
                close=close_price,
                volume=Decimal("1000"),
                available_at=close_at,
            ),
            ingested_at=close_at + timedelta(seconds=1),
            source="fixture",
            source_record_id=f"AAPL-{session_date.isoformat()}",
        )
    return store, calendar, spec


def _spec_with(spec: BacktestSpec, **updates: object) -> BacktestSpec:
    payload = {name: getattr(spec, name) for name in BacktestSpec.model_fields}
    payload.update(updates)
    return BacktestSpec.model_validate(payload, strict=True)


def _session_with(session: BacktestSession, **updates: object) -> BacktestSession:
    payload = {name: getattr(session, name) for name in BacktestSession.model_fields}
    payload.update(updates)
    return BacktestSession.model_validate(payload, strict=True)


def _decimal_context_signature() -> tuple[object, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        tuple(context.flags.items()),
        tuple(context.traps.items()),
    )


def test_five_session_us_vertical_is_chronological_auditable_and_deterministic() -> None:
    store, calendar, spec = _fixture()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=ScriptedStrategy(),
        risk_engine=RiskEngine(),
        transaction_cost_bps=Decimal("10"),
    )

    result = runner.run(spec)

    assert result.final_snapshot.cash == Decimal("1029.640000000000")
    assert result.final_snapshot.nav == Decimal("1029.640000000000")
    assert result.final_snapshot.peak_nav == Decimal("1044.835000000000")
    assert result.realized_pnl == Decimal("29.640000000000")
    assert result.final_lots == ()
    assert tuple(item.session_date for item in result.sessions) == DATES

    events = result.ledger_events
    assert type(events[0]) is CashInitialized
    assert events[0].occurred_at == spec.sessions[0].open_at - timedelta(microseconds=1)
    assert tuple(type(item) for item in events) == (
        CashInitialized,
        PortfolioMarked,
        OpenExecutionBatchBooked,
        PortfolioMarked,
        PortfolioMarked,
        PortfolioMarked,
        OpenExecutionBatchBooked,
        PortfolioMarked,
    )
    close_marks = tuple(item for item in events if type(item) is PortfolioMarked)
    batches = tuple(item for item in events if type(item) is OpenExecutionBatchBooked)
    assert tuple(item.session_date for item in close_marks) == DATES
    assert tuple(item.session_date for item in batches) == (DATES[1], DATES[4])
    assert batches[0].fills[0].quantity == Decimal("1.500000000000")
    assert batches[0].fills[0].fees == Decimal("0.165000000000")
    assert batches[0].marks[0].symbol == "AAPL"
    assert batches[0].marks[0].price == Decimal("110")
    assert batches[1].fills[0].fees == Decimal("0.195000000000")
    assert batches[1].marks == ()

    assert result.sessions[0].execution_results == ()
    for previous, current in zip(result.sessions, result.sessions[1:], strict=False):
        assert tuple(item.order_id for item in current.execution_results) == tuple(
            item.order_id
            for item in previous.submission_results
            if item.status is FillStatus.PENDING
        )
    assert result.sessions[-1].intents == ()
    assert result.sessions[-1].risk_decisions == ()
    assert result.sessions[-1].portfolio_reduction is None
    assert result.sessions[-1].order_plans == ()
    assert result.sessions[-1].submission_results == ()
    assert result.spec_fingerprint.startswith("backtest-spec-sha256:")
    assert result.resolved_data_fingerprint.startswith("resolved-data-sha256:")
    assert len(result.spec_fingerprint) == len("backtest-spec-sha256:") + 64
    assert len(result.resolved_data_fingerprint) == len("resolved-data-sha256:") + 64

    second_store, second_calendar, second_spec = _fixture()
    second = ChronologicalBacktestRunner(
        store=second_store,
        calendar=second_calendar,
        strategy=ScriptedStrategy(),
        risk_engine=RiskEngine(),
        transaction_cost_bps=Decimal("10"),
    ).run(second_spec)
    assert tuple(item.event_id for item in result.ledger_events) == tuple(
        item.event_id for item in second.ledger_events
    )
    assert tuple(fill.fill_id for batch in batches for fill in batch.fills) == tuple(
        fill.fill_id
        for batch in second.ledger_events
        if type(batch) is OpenExecutionBatchBooked
        for fill in batch.fills
    )
    assert tuple(
        plan.order.order_id
        for session in result.sessions
        for plan in session.order_plans
        if plan.order is not None
    ) == tuple(
        plan.order.order_id
        for session in second.sessions
        for plan in session.order_plans
        if plan.order is not None
    )
    assert result.spec_fingerprint == second.spec_fingerprint
    assert result.resolved_data_fingerprint == second.resolved_data_fingerprint

    store.close()
    second_store.close()


def test_five_session_run_is_isolated_from_hostile_decimal_context() -> None:
    expected_store, expected_calendar, expected_spec = _fixture()
    hostile_store, hostile_calendar, hostile_spec = _fixture()
    expected_runner = ChronologicalBacktestRunner(
        store=expected_store,
        calendar=expected_calendar,
        strategy=ScriptedStrategy(),
        risk_engine=RiskEngine(),
        transaction_cost_bps=Decimal("10"),
    )
    hostile_runner = ChronologicalBacktestRunner(
        store=hostile_store,
        calendar=hostile_calendar,
        strategy=ScriptedStrategy(),
        risk_engine=RiskEngine(),
        transaction_cost_bps=Decimal("10"),
    )
    expected = expected_runner.run(expected_spec)

    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_UP
        hostile.Emin = 0
        hostile.Emax = 0
        hostile.traps[Inexact] = True
        signature = _decimal_context_signature()

        actual = hostile_runner.run(hostile_spec)

        assert actual == expected
        assert _decimal_context_signature() == signature

    expected_store.close()
    hostile_store.close()


def test_risk_is_assessed_and_evaluated_once_with_same_context_each_nonfinal_day() -> None:
    store, calendar, spec = _fixture()
    risk = CountingRiskEngine()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=ScriptedStrategy(),
        risk_engine=risk,
        transaction_cost_bps=Decimal("10"),
    )

    runner.run(spec)

    assert tuple(kind for kind, _ in risk.contexts) == (
        "assess",
        "evaluate",
        "assess",
        "evaluate",
        "assess",
        "evaluate",
        "assess",
        "evaluate",
    )
    for assess, evaluate in zip(risk.contexts[::2], risk.contexts[1::2], strict=True):
        assert assess[1] is evaluate[1]
    store.close()


def test_config_version_mismatch_fails_before_strategy_execution() -> None:
    store, calendar, spec = _fixture()
    strategy = CountingScriptedStrategy()
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=strategy
    )

    with pytest.raises(ValueError, match="strategy config version must match exactly"):
        runner.run(_spec_with(spec, strategy_config_version="v2"))

    assert strategy.calls == 0
    store.close()


def test_spec_market_must_match_runner_calendar() -> None:
    store, _, spec = _fixture()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.CN, DATES),
        strategy=ScriptedStrategy(),
    )

    with pytest.raises(ValueError, match="spec market must match runner calendar"):
        runner.run(spec)
    store.close()


def test_spec_sessions_must_be_an_exact_contiguous_calendar_slice() -> None:
    store, calendar, spec = _fixture()
    gapped = _spec_with(spec, sessions=(spec.sessions[0], *spec.sessions[2:]))
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=ScriptedStrategy()
    )

    with pytest.raises(ValueError, match="spec dates must be a contiguous calendar slice"):
        runner.run(gapped)
    store.close()


@pytest.mark.parametrize(
    ("case", "error", "message"),
    (
        ("list", TypeError, "strategy output must be an exact tuple"),
        ("tuple-subclass-output", TypeError, "strategy output must be an exact tuple"),
        ("tuple-subclass", TypeError, "strategy intent must be exactly StrategyIntent"),
        ("polluted", ValueError, "nested model has polluted or missing fields"),
        ("strategy-id", ValueError, "strategy intent strategy_id must match the strategy"),
        ("market", ValueError, "strategy intent market must match the spec"),
        ("as-of", ValueError, "strategy intent as_of must match the session close"),
        ("symbol", ValueError, "strategy intent symbol must belong to the fixed universe"),
        ("duplicate", ValueError, "strategy intent symbols must be unique"),
    ),
)
def test_invalid_strategy_output_fails_before_planning_submission(
    case: str,
    error: type[Exception],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, calendar, spec = _fixture()
    planning_calls: list[object] = []

    def unexpected_planning(*args: object, **kwargs: object) -> None:
        planning_calls.append((args, kwargs))
        raise AssertionError("planning must not be called")

    monkeypatch.setattr(runner_module, "plan_orders", unexpected_planning)
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=CaseStrategy(case)
    )

    with pytest.raises(error, match=message):
        runner.run(spec)

    assert planning_calls == []
    store.close()


@pytest.mark.parametrize("case", ("empty", "equivalent-as-of"))
def test_legal_strategy_outputs_complete(case: str) -> None:
    store, calendar, spec = _fixture()
    result = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=CaseStrategy(case)
    ).run(spec)

    assert len(result.sessions) == len(DATES)
    if case == "empty":
        assert all(session.intents == () for session in result.sessions)
    store.close()


def test_missing_current_close_does_not_append_mark_or_call_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, calendar, spec = _fixture(missing_close_dates=(DATES[0],))
    strategy = CountingScriptedStrategy()
    ledgers: list[PortfolioLedger] = []

    def tracking_ledger(account_id: str, market: Market) -> PortfolioLedger:
        ledger = PortfolioLedger(account_id, market)
        ledgers.append(ledger)
        return ledger

    monkeypatch.setattr(runner_module, "PortfolioLedger", tracking_ledger)
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=strategy
    )

    with pytest.raises(
        ValueError, match=f"missing point-in-time bar for AAPL on {DATES[0].isoformat()}"
    ):
        runner.run(spec)

    assert ledgers == []
    assert strategy.calls == 0
    store.close()


def test_missing_cumulative_close_does_not_append_that_session_mark_or_call_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, calendar, spec = _fixture()
    strategy = CountingScriptedStrategy()
    ledgers: list[PortfolioLedger] = []
    original = PointInTimeStore.latest_bar_revision_as_of

    def intermittently_missing(self: PointInTimeStore, **kwargs: object):  # type: ignore[no-untyped-def]
        if kwargs["session_date"] == DATES[0] and kwargs["as_of"] == spec.sessions[1].close_at:
            return None
        return original(self, **kwargs)  # type: ignore[arg-type]

    def tracking_ledger(account_id: str, market: Market) -> PortfolioLedger:
        ledger = PortfolioLedger(account_id, market)
        ledgers.append(ledger)
        return ledger

    monkeypatch.setattr(
        PointInTimeStore, "latest_bar_revision_as_of", intermittently_missing
    )
    monkeypatch.setattr(runner_module, "PortfolioLedger", tracking_ledger)
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=strategy,
        transaction_cost_bps=Decimal("10"),
    )

    with pytest.raises(
        ValueError, match=f"missing point-in-time bar for AAPL on {DATES[0].isoformat()}"
    ):
        runner.run(spec)

    assert ledgers == []
    assert strategy.calls == 0
    store.close()


def test_day_start_cash_and_new_position_notional_are_session_scoped() -> None:
    store, calendar, spec = _fixture()
    risk = CountingRiskEngine()
    ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=ScriptedStrategy(),
        risk_engine=risk,
        transaction_cost_bps=Decimal("10"),
    ).run(spec)

    contexts = tuple(item[1] for item in risk.contexts[::2])
    assert contexts[1].day_start_available_cash == Decimal("1000")  # type: ignore[attr-defined]
    assert contexts[1].portfolio.cash == Decimal("834.835000000000")  # type: ignore[attr-defined]
    assert contexts[1].new_position_notional_committed_today == Decimal("165.000000000000")  # type: ignore[attr-defined]
    assert contexts[2].day_start_available_cash == Decimal("834.835000000000")  # type: ignore[attr-defined]
    assert contexts[2].new_position_notional_committed_today == 0  # type: ignore[attr-defined]
    store.close()


def test_happy_path_pending_orders_terminate_only_at_the_next_open() -> None:
    store, calendar, spec = _fixture()
    result = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=ScriptedStrategy()
    ).run(spec)

    for previous, current in zip(result.sessions, result.sessions[1:], strict=False):
        pending = tuple(
            item for item in previous.submission_results if item.status is FillStatus.PENDING
        )
        assert tuple(item.order_id for item in current.execution_results) == tuple(
            item.order_id for item in pending
        )
        assert all(
            item.status in (FillStatus.FILLED, FillStatus.REJECTED)
            for item in current.execution_results
        )
        later_ids = {
            item.order_id
            for later in result.sessions[result.sessions.index(current) + 1 :]
            for item in later.execution_results
        }
        assert later_ids.isdisjoint(item.order_id for item in pending)
    store.close()


def test_runner_guard_rejects_nonterminal_next_open_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonterminalSimulator(ExecutionSimulator):
        def process_session(self, **kwargs):  # type: ignore[no-untyped-def]
            results = super().process_session(**kwargs)
            if results:
                return tuple(
                    item.model_copy(update={"status": FillStatus.PENDING})
                    for item in results
                )
            return results

    store, calendar, spec = _fixture()
    monkeypatch.setattr(runner_module, "ExecutionSimulator", NonterminalSimulator)
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=ScriptedStrategy()
    )

    with pytest.raises(
        ValueError, match="all prior pending orders must terminate at the next open"
    ):
        runner.run(spec)
    store.close()


def test_gap_up_cash_rejection_is_audited_without_ledger_booking() -> None:
    store, calendar, spec = _fixture()
    gap_session = _session_with(
        spec.sessions[1],
        open_bars=(
            _bar(
                DATES[1],
                Decimal("1000"),
                at=spec.sessions[1].open_at,
                volume=Decimal(0),
            ),
        ),
    )
    gap_spec = _spec_with(
        spec, sessions=(spec.sessions[0], gap_session, *spec.sessions[2:])
    )
    result = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=ScriptedStrategy(),
        transaction_cost_bps=Decimal("10"),
    ).run(gap_spec)

    rejection = result.sessions[1].execution_results
    assert len(rejection) == 1
    assert rejection[0].status is FillStatus.REJECTED
    assert rejection[0].reason == "insufficient available cash"
    assert not any(
        type(event) is OpenExecutionBatchBooked and event.session_date == DATES[1]
        for event in result.ledger_events
    )
    assert result.sessions[1].portfolio_snapshot.cash == Decimal("1000")
    assert result.final_lots == ()
    assert result.final_snapshot.cash == Decimal("1000")
    store.close()


def test_ledger_append_failure_aborts_with_only_successful_prefix_and_no_later_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledgers: list[PortfolioLedger] = []

    class FailingLedger(PortfolioLedger):
        def append(self, event) -> None:  # type: ignore[no-untyped-def]
            if type(event) is PortfolioMarked and event.session_date == DATES[1]:
                raise ValueError("injected ledger invariant failure")
            super().append(event)

    def failing_ledger(account_id: str, market: Market) -> PortfolioLedger:
        ledger = FailingLedger(account_id, market)
        ledgers.append(ledger)
        return ledger

    store, calendar, spec = _fixture()
    strategy = CountingScriptedStrategy()
    monkeypatch.setattr(runner_module, "PortfolioLedger", failing_ledger)
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=strategy
    )

    with pytest.raises(ValueError, match="injected ledger invariant failure"):
        runner.run(spec)

    assert tuple(type(event) for event in ledgers[0].events) == (
        CashInitialized,
        PortfolioMarked,
        OpenExecutionBatchBooked,
    )
    assert strategy.calls == 1
    store.close()


def test_multiple_successful_fills_share_one_ordered_open_batch_with_complete_marks() -> None:
    two_dates = DATES[:2]
    instruments = (
        Instrument(
            symbol="AAPL",
            market=Market.US,
            currency=Currency.USD,
            sector="Technology",
        ),
        Instrument(
            symbol="MSFT",
            market=Market.US,
            currency=Currency.USD,
            sector="Technology",
        ),
    )

    def frame_bar(symbol: str, session_date: date, price: Decimal) -> Bar:
        return Bar(
            symbol=symbol,
            market=Market.US,
            session_date=session_date,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(0),
            available_at=_instant(session_date, 14),
        )

    sessions = (
        BacktestSession(
            session_date=two_dates[0],
            open_at=_instant(two_dates[0], 14),
            close_at=_instant(two_dates[0], 21),
            open_bars=(
                frame_bar("AAPL", two_dates[0], Decimal("100")),
                frame_bar("MSFT", two_dates[0], Decimal("100")),
            ),
        ),
        BacktestSession(
            session_date=two_dates[1],
            open_at=_instant(two_dates[1], 14),
            close_at=_instant(two_dates[1], 21),
            open_bars=(
                frame_bar("AAPL", two_dates[1], Decimal("110")),
                frame_bar("MSFT", two_dates[1], Decimal("90")),
            ),
        ),
    )
    spec = BacktestSpec(
        run_id="two-fill-run",
        account_id="account-1",
        market=Market.US,
        initial_cash=Decimal("1000"),
        instruments=instruments,
        sessions=sessions,
        strategy_config_version="v1",
    )
    store = PointInTimeStore()
    closes = ((Decimal("100"), Decimal("100")), (Decimal("120"), Decimal("95")))
    for session, prices in zip(sessions, closes, strict=True):
        for instrument, close in zip(instruments, prices, strict=True):
            close_at = session.close_at
            store.append_bar(
                Bar(
                    symbol=instrument.symbol,
                    market=Market.US,
                    session_date=session.session_date,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=Decimal("1000"),
                    available_at=close_at,
                ),
                ingested_at=close_at + timedelta(seconds=1),
                source="fixture",
                source_record_id=f"{instrument.symbol}-{session.session_date}",
            )

    class TwoBuyStrategy:
        strategy_id = "two-buy"
        config_version = "v1"

        def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
            return tuple(
                StrategyIntent(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    market=Market.US,
                    side=Side.BUY,
                    target_weight=Decimal("0.05"),
                    confidence=100,
                    as_of=context.market_snapshot.as_of,
                    thesis="two fill fixture",
                    invalidation="two fill fixture",
                )
                for symbol in ("AAPL", "MSFT")
            )

    result = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.US, two_dates),
        strategy=TwoBuyStrategy(),
    ).run(spec)

    batches = tuple(
        event for event in result.ledger_events if type(event) is OpenExecutionBatchBooked
    )
    assert len(batches) == 1
    assert batches[0].session_date == two_dates[1]
    assert tuple(fill.symbol for fill in batches[0].fills) == ("AAPL", "MSFT")
    assert tuple(mark.symbol for mark in batches[0].marks) == ("AAPL", "MSFT")
    assert tuple(mark.price for mark in batches[0].marks) == (
        Decimal("110"),
        Decimal("90"),
    )
    assert tuple(
        revision.bar.symbol for revision in result.sessions[1].selected_revisions
    ) == ("AAPL", "AAPL", "MSFT", "MSFT")
    store.close()


def test_successful_run_resolves_frozen_matrix_before_cache_but_skips_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, calendar, spec = _fixture()
    strategy = CountingScriptedStrategy()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=strategy,
        transaction_cost_bps=Decimal("10.00"),
    )
    first = runner.run(spec)
    original_fingerprint = first.resolved_data_fingerprint
    correction_at = spec.sessions[-1].close_at + timedelta(seconds=1)
    store.append_bar(
        Bar(
            symbol="AAPL",
            market=Market.US,
            session_date=DATES[0],
            open=Decimal("999"),
            high=Decimal("999"),
            low=Decimal("999"),
            close=Decimal("999"),
            volume=Decimal("1000"),
            available_at=correction_at,
        ),
        ingested_at=correction_at,
        source="late-correction",
        source_record_id="late-correction-d1",
    )

    query_count = 0
    original_query = store.latest_bar_revision_as_of

    def count_query(*args: object, **kwargs: object):
        nonlocal query_count
        query_count += 1
        return original_query(*args, **kwargs)

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("cached run must not touch mutable dependencies")

    monkeypatch.setattr(store, "latest_bar_revision_as_of", count_query)
    monkeypatch.setattr(CountingScriptedStrategy, "evaluate", unexpected)
    monkeypatch.setattr(runner_module, "ExecutionSimulator", unexpected)

    second = runner.run(_spec_with(spec, initial_cash=Decimal("1000.000")))

    assert second is first
    assert second.resolved_data_fingerprint == original_fingerprint
    assert second.sessions == first.sessions
    assert query_count == sum(range(1, len(DATES) + 1))
    store.close()


def test_strategy_identity_is_read_once_and_never_touched_after_construction() -> None:
    store, calendar, spec = _fixture()
    strategy = MutableIdentityStrategy()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=strategy,
        transaction_cost_bps=Decimal("10"),
    )

    first = runner.run(spec)

    assert strategy.identity_reads == {"strategy_id": 1, "config_version": 1}
    strategy.raise_on_identity_read = True
    second = runner.run(spec)

    assert second is first
    assert strategy.identity_reads == {"strategy_id": 1, "config_version": 1}
    store.close()


def test_cached_identity_and_conflict_use_frozen_calendar_and_strategy() -> None:
    store, calendar, spec = _fixture()
    strategy = MutableIdentityStrategy()
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=strategy
    )
    first = runner.run(spec)
    strategy.raise_on_identity_read = True
    object.__setattr__(calendar, "market", Market.CN)
    object.__setattr__(calendar, "sessions", tuple(reversed(DATES)))

    assert runner.run(spec) is first
    with pytest.raises(
        ValueError,
        match=r"^run_id conflicts with a different backtest specification$",
    ):
        runner.run(_spec_with(spec, initial_cash=Decimal("1001")))

    assert strategy.identity_reads == {"strategy_id": 1, "config_version": 1}
    store.close()


def test_same_run_id_with_different_canonical_spec_conflicts_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, calendar, spec = _fixture()
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=ScriptedStrategy(),
        transaction_cost_bps=Decimal("10"),
    )
    runner.run(spec)

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("conflict must precede mutable dependencies")

    monkeypatch.setattr(ScriptedStrategy, "evaluate", unexpected)
    monkeypatch.setattr(runner_module, "ExecutionSimulator", unexpected)

    changed_open = _session_with(
        spec.sessions[0],
        open_bars=(
            _bar(
                DATES[0],
                Decimal("101"),
                at=spec.sessions[0].open_at,
                volume=Decimal(0),
            ),
        ),
    )
    conflicting_specs = (
        _spec_with(spec, initial_cash=Decimal("1001")),
        _spec_with(spec, sessions=(changed_open, *spec.sessions[1:])),
    )
    for conflicting in conflicting_specs:
        with pytest.raises(
            ValueError,
            match=r"^run_id conflicts with a different backtest specification$",
        ):
            runner.run(conflicting)

    runner._transaction_cost_bps = Decimal("11")  # type: ignore[attr-defined]
    with pytest.raises(
        ValueError,
        match=r"^run_id conflicts with a different backtest specification$",
    ):
        runner.run(spec)
    store.close()


def test_failed_run_is_not_cached_and_same_id_can_retry_after_dependency_arrives() -> None:
    store, calendar, spec = _fixture(missing_close_dates=(DATES[0],))
    runner = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=CaseStrategy("empty")
    )

    with pytest.raises(
        ValueError, match=f"missing point-in-time bar for AAPL on {DATES[0].isoformat()}"
    ):
        runner.run(spec)

    close_at = spec.sessions[0].close_at
    store.append_bar(
        _bar(DATES[0], CLOSE_PRICES[0], at=close_at, volume=Decimal("1000")),
        ingested_at=close_at + timedelta(seconds=1),
        source="repaired",
        source_record_id="repaired-d1",
    )
    result = runner.run(spec)

    assert result.run_id == spec.run_id
    assert len(result.sessions) == len(DATES)
    store.close()


def test_semantically_equivalent_specs_are_canonicalized_for_hashes_ids_and_json() -> None:
    first_store, first_calendar, first_spec = _fixture()
    second_store, second_calendar, second_spec = _fixture()
    offset = timezone(timedelta(hours=5, minutes=30))

    equivalent_sessions = tuple(
        _session_with(
            session,
            open_at=session.open_at.astimezone(offset),
            close_at=session.close_at.astimezone(offset),
            open_bars=tuple(
                Bar(
                    symbol=bar.symbol,
                    market=bar.market,
                    session_date=bar.session_date,
                    open=Decimal(f"{bar.open}.000"),
                    high=Decimal(f"{bar.high}.000"),
                    low=Decimal(f"{bar.low}.000"),
                    close=Decimal(f"{bar.close}.000"),
                    volume=Decimal("-0.000"),
                    available_at=bar.available_at.astimezone(offset),
                )
                for bar in session.open_bars
            ),
        )
        for session in second_spec.sessions
    )
    equivalent_spec = _spec_with(
        second_spec,
        initial_cash=Decimal("1000.0000"),
        sessions=equivalent_sessions,
    )
    first = ChronologicalBacktestRunner(
        store=first_store,
        calendar=first_calendar,
        strategy=ScriptedStrategy(),
        transaction_cost_bps=Decimal("10"),
    ).run(first_spec)
    equivalent = ChronologicalBacktestRunner(
        store=second_store,
        calendar=second_calendar,
        strategy=ScriptedStrategy(),
        transaction_cost_bps=Decimal("10.000"),
    ).run(equivalent_spec)

    assert equivalent.spec_fingerprint == first.spec_fingerprint
    assert equivalent.model_dump_json() == first.model_dump_json()

    third_store, third_calendar, third_spec = _fixture()
    different_run_id = ChronologicalBacktestRunner(
        store=third_store,
        calendar=third_calendar,
        strategy=ScriptedStrategy(),
        transaction_cost_bps=Decimal("10"),
    ).run(_spec_with(third_spec, run_id="different-run-id"))
    assert different_run_id.spec_fingerprint == first.spec_fingerprint

    first_store.close()
    second_store.close()
    third_store.close()


def test_same_run_id_with_changed_cn_state_conflicts_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = DATES[:2]
    instrument = Instrument(
        symbol="600000",
        market=Market.CN,
        currency=Currency.CNY,
        sector="Financials",
    )

    def cn_session(session_date: date, *, suspended: bool) -> BacktestSession:
        open_at = _instant(session_date, 1)
        return BacktestSession(
            session_date=session_date,
            open_at=open_at,
            close_at=_instant(session_date, 7),
            open_bars=(
                Bar(
                    symbol=instrument.symbol,
                    market=Market.CN,
                    session_date=session_date,
                    open=Decimal("10"),
                    high=Decimal("10"),
                    low=Decimal("10"),
                    close=Decimal("10"),
                    volume=Decimal(0),
                    available_at=open_at,
                ),
            ),
            cn_session_states=(
                CnSessionState(
                    symbol=instrument.symbol,
                    session_date=session_date,
                    suspended=suspended,
                    price_limit_state=CnPriceLimitState.NONE,
                ),
            ),
        )

    sessions = tuple(cn_session(item, suspended=False) for item in dates)
    spec = BacktestSpec(
        run_id="cn-conflict-run",
        account_id="cn-account",
        market=Market.CN,
        initial_cash=Decimal("1000"),
        instruments=(instrument,),
        sessions=sessions,
        strategy_config_version="v1",
    )
    store = PointInTimeStore()
    for session in sessions:
        store.append_bar(
            Bar(
                symbol=instrument.symbol,
                market=Market.CN,
                session_date=session.session_date,
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                volume=Decimal("1000"),
                available_at=session.close_at,
            ),
            ingested_at=session.close_at,
            source="cn-fixture",
            source_record_id=f"cn-{session.session_date}",
        )
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.CN, dates),
        strategy=CaseStrategy("empty"),
    )
    runner.run(spec)

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("CN conflict must precede mutable dependencies")

    monkeypatch.setattr(CaseStrategy, "evaluate", unexpected)
    monkeypatch.setattr(runner_module, "ExecutionSimulator", unexpected)
    changed_first = cn_session(dates[0], suspended=True)

    with pytest.raises(
        ValueError,
        match=r"^run_id conflicts with a different backtest specification$",
    ):
        runner.run(_spec_with(spec, sessions=(changed_first, sessions[1])))
    store.close()
