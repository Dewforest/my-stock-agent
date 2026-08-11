from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from stock_agent.account import OpenExecutionBatchBooked
from stock_agent.backtest import (
    BacktestSession,
    BacktestSpec,
    ChronologicalBacktestRunner,
    OrderPlanSource,
    OrderPlanStatus,
)
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Currency, Instrument, Market, Side, StrategyIntent
from stock_agent.execution import FillStatus
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext

DATES = tuple(date(2026, 7, day) for day in (20, 21, 22, 23, 24))


@dataclass(frozen=True)
class EmptyStrategy:
    strategy_id: str = "empty-pit"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        return ()


def _instant(session_date: date, hour: int) -> datetime:
    return datetime(session_date.year, session_date.month, session_date.day, hour, tzinfo=UTC)


def _bar(session_date: date, close: Decimal, available_at: datetime) -> Bar:
    return Bar(
        symbol="AAPL",
        market=Market.US,
        session_date=session_date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1000"),
        available_at=available_at,
    )


def _spec(run_id: str) -> BacktestSpec:
    return BacktestSpec(
        run_id=run_id,
        account_id="pit-account",
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
        sessions=tuple(
            BacktestSession(
                session_date=session_date,
                open_at=_instant(session_date, 14),
                close_at=_instant(session_date, 21),
                open_bars=(
                    Bar(
                        symbol="AAPL",
                        market=Market.US,
                        session_date=session_date,
                        open=Decimal("100"),
                        high=Decimal("100"),
                        low=Decimal("100"),
                        close=Decimal("100"),
                        volume=Decimal(0),
                        available_at=_instant(session_date, 14),
                    ),
                ),
            )
            for session_date in DATES
        ),
        strategy_config_version="v1",
    )


def _store(*, with_correction: bool) -> PointInTimeStore:
    store = PointInTimeStore()
    for session_date in DATES:
        close_at = _instant(session_date, 21)
        store.append_bar(
            _bar(session_date, Decimal("100"), close_at),
            ingested_at=close_at + timedelta(seconds=1),
            source="official-original",
            source_record_id=f"original-{session_date.isoformat()}",
        )
    if with_correction:
        correction_available = _instant(DATES[3], 20)
        store.append_bar(
            _bar(DATES[0], Decimal("111"), correction_available),
            ingested_at=correction_available + timedelta(seconds=7),
            source="official-correction",
            source_record_id="correction-d1-v2",
        )
    return store


def test_late_pit_correction_changes_only_first_lawful_cumulative_snapshot_and_audit() -> None:
    corrected_store = _store(with_correction=True)
    baseline_store = _store(with_correction=False)
    calendar = TradingCalendar(Market.US, DATES)

    corrected = ChronologicalBacktestRunner(
        store=corrected_store, calendar=calendar, strategy=EmptyStrategy()
    ).run(_spec("corrected-run"))
    baseline = ChronologicalBacktestRunner(
        store=baseline_store, calendar=calendar, strategy=EmptyStrategy()
    ).run(_spec("baseline-run"))

    d1_selected = tuple(session.selected_revisions[0] for session in corrected.sessions)
    assert tuple(item.bar.close for item in d1_selected) == (
        Decimal("100.000000000000"),
        Decimal("100.000000000000"),
        Decimal("100.000000000000"),
        Decimal("111.000000000000"),
        Decimal("111.000000000000"),
    )
    assert tuple(item.source for item in d1_selected) == (
        "official-original",
        "official-original",
        "official-original",
        "official-correction",
        "official-correction",
    )
    assert tuple(item.source_record_id for item in d1_selected) == (
        "original-2026-07-20",
        "original-2026-07-20",
        "original-2026-07-20",
        "correction-d1-v2",
        "correction-d1-v2",
    )
    assert d1_selected[0].bar.available_at == _instant(DATES[0], 21)
    assert d1_selected[0].ingested_at == _instant(DATES[0], 21) + timedelta(seconds=1)
    assert d1_selected[3].bar.available_at == _instant(DATES[3], 20)
    assert d1_selected[3].ingested_at == _instant(DATES[3], 20) + timedelta(seconds=7)
    assert corrected.sessions[2].selected_revisions[0] == d1_selected[0]
    assert corrected.resolved_data_fingerprint != baseline.resolved_data_fingerprint

    corrected_store.close()
    baseline_store.close()


US_DRAWDOWN_DATES = DATES[:4]
US_DRAWDOWN_SYMBOLS = ("AAPL", "MSFT")


@dataclass(frozen=True)
class DrawdownStrategy:
    lower_targets_on_drawdown: bool = False
    strategy_id: str = "drawdown-script"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        current_date = max(bar.session_date for bar in context.market_snapshot.bars)
        if current_date == US_DRAWDOWN_DATES[0]:
            script = tuple((symbol, Side.BUY, Decimal("0.15")) for symbol in US_DRAWDOWN_SYMBOLS)
        elif current_date == US_DRAWDOWN_DATES[2] and self.lower_targets_on_drawdown:
            script = (
                ("AAPL", Side.SELL, Decimal(0)),
                ("MSFT", Side.REDUCE, Decimal("0.01")),
            )
        else:
            script = ()
        return tuple(
            StrategyIntent(
                strategy_id=self.strategy_id,
                symbol=symbol,
                market=Market.US,
                side=side,
                target_weight=target,
                confidence=100,
                as_of=context.market_snapshot.as_of,
                thesis="drawdown integration fixture",
                invalidation="fixture only",
            )
            for symbol, side, target in script
        )


def _market_bar(
    *,
    symbol: str,
    market: Market,
    session_date: date,
    price: Decimal,
    at: datetime,
    volume: Decimal,
) -> Bar:
    return Bar(
        symbol=symbol,
        market=market,
        session_date=session_date,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        available_at=at,
    )


def _drawdown_fixture(run_id: str) -> tuple[PointInTimeStore, TradingCalendar, BacktestSpec]:
    instruments = tuple(
        Instrument(
            symbol=symbol,
            market=Market.US,
            currency=Currency.USD,
            sector="Technology",
        )
        for symbol in US_DRAWDOWN_SYMBOLS
    )
    sessions = tuple(
        BacktestSession(
            session_date=session_date,
            open_at=_instant(session_date, 14),
            close_at=_instant(session_date, 21),
            open_bars=tuple(
                _market_bar(
                    symbol=symbol,
                    market=Market.US,
                    session_date=session_date,
                    price=Decimal("100") if index < 3 else Decimal("20"),
                    at=_instant(session_date, 14),
                    volume=Decimal(0),
                )
                for symbol in US_DRAWDOWN_SYMBOLS
            ),
        )
        for index, session_date in enumerate(US_DRAWDOWN_DATES)
    )
    spec = BacktestSpec(
        run_id=run_id,
        account_id="drawdown-account",
        market=Market.US,
        initial_cash=Decimal("1000"),
        instruments=instruments,
        sessions=sessions,
        strategy_config_version="v1",
    )
    store = PointInTimeStore()
    closes = (Decimal("100"), Decimal("200"), Decimal("20"), Decimal("20"))
    for session, close in zip(sessions, closes, strict=True):
        for symbol in US_DRAWDOWN_SYMBOLS:
            store.append_bar(
                _market_bar(
                    symbol=symbol,
                    market=Market.US,
                    session_date=session.session_date,
                    price=close,
                    at=session.close_at,
                    volume=Decimal("1000"),
                ),
                ingested_at=session.close_at,
                source="drawdown-fixture",
                source_record_id=f"{symbol}-{session.session_date}",
            )
    return store, TradingCalendar(Market.US, US_DRAWDOWN_DATES), spec


def test_empty_strategy_output_still_executes_drawdown_reduction_for_every_holding() -> None:
    store, calendar, spec = _drawdown_fixture("empty-drawdown-run")

    result = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=DrawdownStrategy(),
        transaction_cost_bps=Decimal(0),
    ).run(spec)

    d2 = result.sessions[1]
    d3 = result.sessions[2]
    assert tuple(fill.filled_quantity for fill in d2.execution_results) == (
        Decimal("1.500000000000"),
        Decimal("1.500000000000"),
    )
    assert d2.portfolio_snapshot.nav == Decimal("1300.000000000000")
    assert d3.portfolio_snapshot.nav == Decimal("760.000000000000")
    assert d3.portfolio_snapshot.peak_nav == Decimal("1300.000000000000")
    assert d3.intents == ()
    assert d3.risk_decisions == ()
    assert d3.portfolio_reduction is not None
    assert d3.portfolio_reduction.current_gross_exposure == Decimal(
        "0.078947368421052631578947368421052631578947368421052631578947368421052631578947368421052631578947368421052631578947368421052631579"
    )
    assert d3.portfolio_reduction.target_gross_exposure == Decimal(
        "0.0394736842105263157894736842105263157894736842105263157894736842105263157894736842105263157894736842105263157894736842105263157895"
    )
    assert tuple(plan.symbol for plan in d3.order_plans) == US_DRAWDOWN_SYMBOLS
    assert all(plan.status is OrderPlanStatus.SUBMITTED for plan in d3.order_plans)
    assert all(plan.source is OrderPlanSource.RISK_REDUCTION for plan in d3.order_plans)
    assert all(plan.order is not None and plan.order.side is Side.SELL for plan in d3.order_plans)
    assert tuple(plan.target_weight for plan in d3.order_plans) == (
        Decimal("0.019736842105"),
        Decimal("0.019736842105"),
    )
    assert tuple(plan.submitted_quantity for plan in d3.order_plans) == (
        Decimal("0.750000000010"),
        Decimal("0.750000000010"),
    )
    assert all(fill.status is FillStatus.PENDING for fill in d3.submission_results)
    assert tuple(lot.quantity for lot in result.final_lots) == (
        Decimal("0.749999999990"),
        Decimal("0.749999999990"),
    )
    assert tuple(position.quantity for position in result.final_snapshot.positions) == (
        Decimal("0.749999999990"),
        Decimal("0.749999999990"),
    )
    store.close()


def test_lower_strategy_targets_win_without_duplicate_drawdown_orders() -> None:
    store, calendar, spec = _drawdown_fixture("lower-target-drawdown-run")

    result = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=DrawdownStrategy(lower_targets_on_drawdown=True),
        transaction_cost_bps=Decimal(0),
    ).run(spec)

    d3 = result.sessions[2]
    assert d3.portfolio_reduction is not None
    assert len(d3.risk_decisions) == 2
    assert all(decision.risk_reduction == d3.portfolio_reduction for decision in d3.risk_decisions)
    assert tuple(plan.symbol for plan in d3.order_plans) == US_DRAWDOWN_SYMBOLS
    assert len({plan.symbol for plan in d3.order_plans}) == len(d3.order_plans)
    assert all(plan.source is OrderPlanSource.RISK_REDUCTION for plan in d3.order_plans)
    assert all(plan.order is not None and plan.order.side is Side.SELL for plan in d3.order_plans)
    aapl, msft = d3.order_plans
    assert aapl.target_weight == 0
    assert aapl.submitted_quantity == Decimal("1.500000000000")
    assert msft.target_weight == Decimal("0.01")
    assert msft.submitted_quantity == Decimal("1.120000000000")
    assert msft.submitted_quantity > Decimal("0.75")
    store.close()


CN_DATES = tuple(date(2026, 7, day) for day in (20, 21, 22, 23, 24, 27, 28))
CN_SYMBOL = "600000"


@dataclass(frozen=True)
class CnExecutionStrategy:
    strategy_id: str = "cn-execution-script"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        current_date = max(bar.session_date for bar in context.market_snapshot.bars)
        index = CN_DATES.index(current_date)
        side, target = {
            0: (Side.BUY, Decimal("0.15")),
            1: (Side.SELL, Decimal(0)),
            2: (Side.SELL, Decimal(0)),
            3: (Side.SELL, Decimal(0)),
            4: (Side.BUY, Decimal("0.15")),
            5: (Side.HOLD, Decimal(0)),
        }[index]
        return (
            StrategyIntent(
                strategy_id=self.strategy_id,
                symbol=CN_SYMBOL,
                market=Market.CN,
                side=side,
                target_weight=target,
                confidence=100,
                as_of=context.market_snapshot.as_of,
                thesis="CN execution integration fixture",
                invalidation="fixture only",
            ),
        )


def _cn_fixture() -> tuple[PointInTimeStore, TradingCalendar, BacktestSpec]:
    states = (
        (False, CnPriceLimitState.NONE),
        (False, CnPriceLimitState.NONE),
        (False, CnPriceLimitState.LIMIT_DOWN),
        (True, CnPriceLimitState.NONE),
        (False, CnPriceLimitState.NONE),
        (False, CnPriceLimitState.LIMIT_UP),
        (False, CnPriceLimitState.NONE),
    )
    sessions = tuple(
        BacktestSession(
            session_date=session_date,
            open_at=_instant(session_date, 1),
            close_at=_instant(session_date, 7),
            open_bars=(
                _market_bar(
                    symbol=CN_SYMBOL,
                    market=Market.CN,
                    session_date=session_date,
                    price=Decimal("10"),
                    at=_instant(session_date, 1),
                    volume=Decimal(0),
                ),
            ),
            cn_session_states=(
                CnSessionState(
                    symbol=CN_SYMBOL,
                    session_date=session_date,
                    suspended=suspended,
                    price_limit_state=limit_state,
                ),
            ),
        )
        for session_date, (suspended, limit_state) in zip(CN_DATES, states, strict=True)
    )
    spec = BacktestSpec(
        run_id="cn-seven-session-run",
        account_id="cn-account",
        market=Market.CN,
        initial_cash=Decimal("10000"),
        instruments=(
            Instrument(
                symbol=CN_SYMBOL,
                market=Market.CN,
                currency=Currency.CNY,
                sector="Financials",
            ),
        ),
        sessions=sessions,
        strategy_config_version="v1",
    )
    store = PointInTimeStore()
    for session in sessions:
        store.append_bar(
            _market_bar(
                symbol=CN_SYMBOL,
                market=Market.CN,
                session_date=session.session_date,
                price=Decimal("10"),
                at=session.close_at,
                volume=Decimal("1000"),
            ),
            ingested_at=session.close_at,
            source="cn-integration-fixture",
            source_record_id=f"cn-{session.session_date}",
        )
    return store, TradingCalendar(Market.CN, CN_DATES), spec


def test_cn_runner_integrates_lots_t1_limits_suspension_and_complete_frames() -> None:
    store, calendar, spec = _cn_fixture()

    result = ChronologicalBacktestRunner(
        store=store,
        calendar=calendar,
        strategy=CnExecutionStrategy(),
        transaction_cost_bps=Decimal(0),
    ).run(spec)

    d1, d2, d3, d4, d5, d6, d7 = result.sessions
    buy_plan = d1.order_plans[0]
    assert buy_plan.raw_quantity == Decimal("150")
    assert buy_plan.submitted_quantity == Decimal("150.000000000000")
    assert buy_plan.effective_quantity == Decimal("100")
    assert d1.submission_results[0].requested_quantity == Decimal("100")
    assert d1.submission_results[0].status is FillStatus.PENDING
    assert all(
        len(session.cn_session_states) == 1 and type(session.cn_session_states[0]) is CnSessionState
        for session in result.manifest.sessions
    )

    expected_reasons = {
        d3.session_date: "sell blocked at limit down",
        d4.session_date: "instrument is suspended",
        d6.session_date: "buy blocked at limit up",
    }
    for session in (d3, d4, d6):
        assert len(session.execution_results) == 1
        execution = session.execution_results[0]
        assert execution.status is FillStatus.REJECTED
        assert execution.reason == expected_reasons[session.session_date]
        assert "T+1" not in execution.reason

    assert d2.execution_results[0].status is FillStatus.FILLED
    assert d2.execution_results[0].side is Side.BUY
    assert d2.execution_results[0].filled_quantity == Decimal("100")
    assert d2.portfolio_snapshot.positions[0].quantity == Decimal("100")
    assert d3.portfolio_snapshot.positions[0].quantity == Decimal("100")
    assert d4.portfolio_snapshot.positions[0].quantity == Decimal("100")
    assert d5.execution_results[0].status is FillStatus.FILLED
    assert d5.execution_results[0].side is Side.SELL
    assert d5.execution_results[0].filled_quantity == Decimal("100")
    assert d5.portfolio_snapshot.positions == ()
    assert d5.portfolio_snapshot.cash == Decimal("10000.000000000000")
    second_buy_plan = d5.order_plans[0]
    assert second_buy_plan.raw_quantity == Decimal("150")
    assert second_buy_plan.submitted_quantity == Decimal("150.000000000000")
    assert second_buy_plan.effective_quantity == Decimal("100")
    assert d6.intents[0].side is Side.HOLD

    link_fields = ("order_id", "account_id", "symbol", "market", "side", "requested_quantity")
    for previous, current in zip(result.sessions, result.sessions[1:], strict=False):
        pending = tuple(
            fill for fill in previous.submission_results if fill.status is FillStatus.PENDING
        )
        assert len(pending) == len(current.execution_results)
        assert all(
            all(getattr(submission, field) == getattr(execution, field) for field in link_fields)
            for submission, execution in zip(pending, current.execution_results, strict=True)
        )

    batches = tuple(
        event for event in result.ledger_events if type(event) is OpenExecutionBatchBooked
    )
    assert tuple(batch.session_date for batch in batches) == (CN_DATES[1], CN_DATES[4])
    assert tuple(batch.fills[0].side for batch in batches) == (Side.BUY, Side.SELL)
    assert tuple(batch.fills[0].quantity for batch in batches) == (
        Decimal("100"),
        Decimal("100"),
    )
    booked_fills = tuple(fill for batch in batches for fill in batch.fills)
    successful_executions = tuple(
        fill
        for session in result.sessions
        for fill in session.execution_results
        if fill.status is FillStatus.FILLED
    )
    assert len(booked_fills) == len(successful_executions) == 2
    assert tuple(
        (fill.symbol, fill.side, fill.quantity, fill.price) for fill in booked_fills
    ) == tuple(
        (fill.symbol, fill.side, fill.filled_quantity, fill.price) for fill in successful_executions
    )
    assert d7.intents == ()
    assert result.final_lots == ()
    assert result.final_snapshot.cash == Decimal("10000.000000000000")
    assert result.final_snapshot.nav == Decimal("10000.000000000000")
    store.close()
