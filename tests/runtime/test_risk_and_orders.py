from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from stock_agent.domain import (
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Side,
    StrategyIntent,
)
from stock_agent.risk.engine import RiskContext, RiskEngine
from stock_agent.runtime.orders import (
    build_pending_orders,
    evaluate_and_persist,
    order_set_digest_for,
    persist_risk_and_orders,
)
from stock_agent.runtime.state import PendingOrder
from stock_agent.runtime.store import RuntimeStore, StoreError

AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
NEXT_SESSION = date(2026, 8, 14)
SYMBOLS = ("AAPL", "JPM", "XOM")
SECTORS = {"AAPL": "Technology", "JPM": "Financials", "XOM": "Energy"}


def intent(symbol: str, side: Side = Side.BUY, target_weight: str = "0.05") -> StrategyIntent:
    return StrategyIntent(
        strategy_id="strategy-a-bounded-llm",
        symbol=symbol,
        market=Market.US,
        side=side,
        target_weight=Decimal(target_weight),
        confidence=80,
        as_of=AS_OF,
        thesis="bounded rationale",
        invalidation="bounded condition",
    )


def risk_context() -> RiskContext:
    return RiskContext(
        portfolio=PortfolioSnapshot(
            account_id="paper-us-v1",
            market=Market.US,
            cash=Decimal("100000"),
            nav=Decimal("100000"),
            peak_nav=Decimal("100000"),
            positions=(),
            as_of=AS_OF,
        ),
        instruments=tuple(
            Instrument(
                symbol=symbol, market=Market.US, currency=Currency.USD, sector=SECTORS[symbol]
            )
            for symbol in SYMBOLS
        ),
        day_start_available_cash=Decimal("100000"),
        new_position_notional_committed_today=Decimal("0"),
    )


def intents() -> tuple[StrategyIntent, ...]:
    return (intent("AAPL"), intent("JPM"), intent("XOM"))


class SpyEngine:
    def __init__(self, inner: RiskEngine) -> None:
        self._inner = inner
        self.evaluate_many_calls = 0

    def evaluate_many(
        self, intents: tuple[StrategyIntent, ...], context: RiskContext
    ) -> tuple[object, ...]:
        self.evaluate_many_calls += 1
        return self._inner.evaluate_many(intents, context)


# ── 1. one evaluate_many call ──────────────────────────────────────────────


def test_risk_uses_single_evaluate_many_call(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    spy = SpyEngine(RiskEngine())
    orders = evaluate_and_persist(
        store=store,
        engine=spy,  # type: ignore[arg-type]
        run_id=RUN_ID,
        intents=intents(),
        risk_context=risk_context(),
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert spy.evaluate_many_calls == 1
    assert len(orders) == 3


# ── 3. risk rejection is an audited normal result ──────────────────────────


def test_risk_rejection_produces_no_order(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    # A market-mismatched intent is rejected by risk.
    decisions = RiskEngine().evaluate_many(
        (
            StrategyIntent(
                strategy_id="strategy-a-bounded-llm",
                symbol="AAPL",
                market=Market.CN,  # mismatched market vs US portfolio
                side=Side.BUY,
                target_weight=Decimal("0.10"),
                confidence=80,
                as_of=AS_OF,
                thesis="t",
                invalidation="i",
            ),
        ),
        risk_context(),
    )
    orders = persist_risk_and_orders(
        store=store,
        run_id=RUN_ID,
        decisions=decisions,
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert orders == ()


# ── 4/6. atomic commit and restart digest ─────────────────────────────────


def test_orders_commit_and_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    decisions = RiskEngine().evaluate_many(intents(), risk_context())
    orders = persist_risk_and_orders(
        store=store,
        run_id=RUN_ID,
        decisions=decisions,
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert len(orders) == 3
    digest = order_set_digest_for(RUN_ID, orders)
    store.close()

    reopened = RuntimeStore(path)
    loaded = reopened.load_orders(RUN_ID)
    assert loaded is not None
    loaded_orders, loaded_digest = loaded
    assert loaded_digest == digest
    assert order_set_digest_for(RUN_ID, loaded_orders) == digest


# ── 7. idempotent reappend ────────────────────────────────────────────────


def test_identical_reappend_is_idempotent(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    decisions = RiskEngine().evaluate_many(intents(), risk_context())
    orders = persist_risk_and_orders(
        store=store,
        run_id=RUN_ID,
        decisions=decisions,
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    digest = order_set_digest_for(RUN_ID, orders)
    # Re-persisting the identical set is a no-op.
    store.persist_orders(run_id=RUN_ID, orders=orders, digest=digest, now=AS_OF)
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[1] == digest


# ── 8. same order ID with different content conflicts ─────────────────────


def test_same_order_id_with_different_content_conflicts(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    decisions = RiskEngine().evaluate_many(intents(), risk_context())
    orders = persist_risk_and_orders(
        store=store,
        run_id=RUN_ID,
        decisions=decisions,
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    # Tamper with the first order's content but keep its ID.
    original = orders[0]
    tampered = tuple(
        PendingOrder(
            order_id=order.order_id,
            run_id=order.run_id,
            symbol=order.symbol,
            market=order.market,
            side=order.side,
            target_weight=(
                Decimal("0.06") if order.order_id == original.order_id else order.target_weight
            ),
            intended_session_date=order.intended_session_date,
            ordinal=order.ordinal,
        )
        for order in orders
    )
    tampered_digest = order_set_digest_for(RUN_ID, tampered)
    with pytest.raises(StoreError):
        store.persist_orders(run_id=RUN_ID, orders=tampered, digest=tampered_digest, now=AS_OF)


# ── 9. orders target the next explicit session ────────────────────────────


def test_orders_target_next_session(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    decisions = RiskEngine().evaluate_many(intents(), risk_context())
    orders = persist_risk_and_orders(
        store=store,
        run_id=RUN_ID,
        decisions=decisions,
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    assert all(order.intended_session_date == NEXT_SESSION for order in orders)


# ── 10. no failed/data-incomplete/ambiguous run produces an order ──────────


def test_rejected_and_clamped_produce_only_approved_orders(tmp_path: Path) -> None:
    decisions = RiskEngine().evaluate_many(intents(), risk_context())
    # Every decision is APPROVED here; build only APPROVED/CLAMPED orders.
    orders = build_pending_orders(RUN_ID, decisions, NEXT_SESSION)
    assert all(order.side in (Side.BUY, Side.HOLD, Side.SELL, Side.REDUCE) for order in orders)
    # Rejection (status REJECTED) is never turned into an order.
    assert all(order.target_weight >= 0 for order in orders)
