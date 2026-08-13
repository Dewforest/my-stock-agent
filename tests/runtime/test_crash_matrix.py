from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from stock_agent.account.ledger import BuyFilled, CashInitialized
from stock_agent.domain import Market, Side
from stock_agent.runtime.ledger_store import LedgerStore
from stock_agent.runtime.orders import order_set_digest_for
from stock_agent.runtime.state import KillSwitchScope, PendingOrder
from stock_agent.runtime.store import RuntimeStore

AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
NEXT_SESSION = date(2026, 8, 14)
US = "paper-us-v1"


def make_order(order_id: str = "order-1") -> PendingOrder:
    return PendingOrder(
        order_id=order_id,
        run_id=RUN_ID,
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        target_weight=Decimal("0.05"),
        intended_session_date=NEXT_SESSION,
        ordinal=1,
    )


def test_orders_and_ledger_survive_restart_together(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    ledger_store = LedgerStore(store)

    # Commit orders and ledger events before a simulated crash.
    order = make_order()
    store.persist_orders(
        run_id=RUN_ID, orders=(order,), digest=order_set_digest_for(RUN_ID, (order,)), now=AS_OF
    )
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
            BuyFilled(
                event_id="buy-1",
                account_id=US,
                market=Market.US,
                occurred_at=AS_OF,
                symbol="AAPL",
                session_date=NEXT_SESSION,
                quantity=Decimal("100"),
                price=Decimal("200"),
                fees=Decimal("0"),
            ),
        ),
    )
    store.close()

    # Restart and verify both stores rebuild identically.
    reopened = RuntimeStore(path)
    orders, _digest = reopened.load_orders(RUN_ID)
    assert orders[0].order_id == "order-1"
    ledger = LedgerStore(reopened).rebuild_ledger(US, Market.US)
    assert ledger.cash == Decimal("980000")  # 1,000,000 - 100*200


def test_kill_switch_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.set_kill_switch(KillSwitchScope.MARKET, Market.CN.value, set_by="test", now=AS_OF)
    store.close()

    reopened = RuntimeStore(path)
    assert reopened.is_blocked(Market.CN, "paper-cn-v1") is True
    assert reopened.is_blocked(Market.US, "paper-us-v1") is False


def test_obligation_state_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    order = make_order()
    store.persist_orders(
        run_id=RUN_ID, orders=(order,), digest=order_set_digest_for(RUN_ID, (order,)), now=AS_OF
    )
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    store.block_obligation_data(
        order_id=order.order_id,
        missing_authority="execution_open_and_cn_session_state",
        error_digest=None,
        now=AS_OF,
    )
    store.close()

    reopened = RuntimeStore(path)
    assert reopened.get_obligation_status(order.order_id) == "BLOCKED_DATA"
