from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from stock_agent.account import (
    CashInitialized,
    OpenExecutionBatchBooked,
    PortfolioMarked,
)
from stock_agent.backtest import BacktestRunner, BacktestSession, BacktestSpec
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Currency, Instrument, Market, Side, StrategyIntent
from stock_agent.execution import FillStatus
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


def _fixture() -> tuple[PointInTimeStore, TradingCalendar, BacktestSpec]:
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


def test_five_session_us_vertical_is_chronological_auditable_and_deterministic() -> None:
    store, calendar, spec = _fixture()
    runner = BacktestRunner(
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
    second = BacktestRunner(
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
