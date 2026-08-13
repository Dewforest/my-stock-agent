from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from stock_agent.account.ledger import CashInitialized
from stock_agent.domain import Market, Side, StrategyIntent
from stock_agent.risk.engine import RiskContext, RiskDecisionStatus, RiskEngine
from stock_agent.runtime.ledger_store import LedgerStore
from stock_agent.runtime.orders import evaluate_and_persist
from stock_agent.runtime.store import RuntimeStore

AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
NEXT_SESSION = date(2026, 8, 14)
RUN_ID = "paper-run-sha256:" + "a" * 64
US = "paper-us-v1"


def _intent(symbol: str, market: Market, side: Side, target: str) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="strategy-a-bounded-llm",
        symbol=symbol,
        market=market,
        side=side,
        target_weight=Decimal(target),
        confidence=80,
        as_of=AS_OF,
        thesis="thesis",
        invalidation="invalidation",
    )


def _risk_context(market: Market, symbols: tuple[str, ...]) -> RiskContext:
    from stock_agent.domain import Currency, Instrument, PortfolioSnapshot

    instruments = tuple(
        Instrument(
            symbol=s,
            market=market,
            currency=Currency.USD if market is Market.US else Currency.CNY,
            sector="Tech",
        )
        for s in symbols
    )
    portfolio = PortfolioSnapshot(
        account_id=US if market is Market.US else "paper-cn-v1",
        market=market,
        cash=Decimal("100000"),
        nav=Decimal("100000"),
        peak_nav=Decimal("100000"),
        positions=(),
        as_of=AS_OF,
    )
    return RiskContext(
        portfolio=portfolio,
        instruments=instruments,
        day_start_available_cash=Decimal("100000"),
        new_position_notional_committed_today=Decimal("0"),
    )


def test_us_vertical_slice_persists_orders_and_ledger(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    ledger_store = LedgerStore(store)

    # Risk evaluation over intents produces deterministic next-open orders.
    symbols = ("AAPL", "JPM", "XOM")
    intents = (
        _intent("AAPL", Market.US, Side.BUY, "0.05"),
        _intent("JPM", Market.US, Side.BUY, "0.05"),
        _intent("XOM", Market.US, Side.BUY, "0.05"),
    )
    engine = RiskEngine()
    decisions = engine.evaluate_many(intents, _risk_context(Market.US, symbols))  # type: ignore[arg-type]
    assert all(
        d.status in (RiskDecisionStatus.APPROVED, RiskDecisionStatus.CLAMPED) for d in decisions
    )

    orders = evaluate_and_persist(
        store=store,
        engine=engine,
        run_id=RUN_ID,
        intents=intents,
        risk_context=_risk_context(Market.US, symbols),
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert len(orders) == 3

    # Book a durable ledger boundary alongside.
    ledger_store.append_events(
        US,
        Market.US,
        (
            CashInitialized(
                event_id="cash-1",
                account_id=US,
                market=Market.US,
                occurred_at=AS_OF - timedelta(days=1),
                amount=Decimal("1000000"),
            ),
        ),
    )
    assert ledger_store.rebuild_ledger(US, Market.US).cash == Decimal("1000000")


def test_cn_vertical_slice_persists_cny_orders(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    symbols = ("600519", "601318", "600036")
    intents = (
        _intent("600519", Market.CN, Side.BUY, "0.05"),
        _intent("601318", Market.CN, Side.BUY, "0.05"),
        _intent("600036", Market.CN, Side.BUY, "0.05"),
    )
    engine = RiskEngine()
    orders = evaluate_and_persist(
        store=store,
        engine=engine,
        run_id=RUN_ID,
        intents=intents,
        risk_context=_risk_context(Market.CN, symbols),
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert len(orders) == 3
    assert all(o.market is Market.CN for o in orders)
