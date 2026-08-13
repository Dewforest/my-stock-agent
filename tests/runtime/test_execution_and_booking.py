from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from stock_agent.domain import Market, Side
from stock_agent.execution.models import Fill, FillStatus
from stock_agent.runtime.capability_gates import (
    EXECUTION_OPEN_AND_CN_SESSION_STATE,
    AuthorityRecord,
    AuthorityVerdict,
    CapabilityGates,
)
from stock_agent.runtime.execution import ExecutionAdapter, ExecutionOutcome
from stock_agent.runtime.ledger_store import LedgerStore
from stock_agent.runtime.orders import order_set_digest_for
from stock_agent.runtime.state import PendingOrder
from stock_agent.runtime.store import RuntimeStore, StoreError

AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
NEXT_SESSION = date(2026, 8, 14)


def gates(validated: bool) -> CapabilityGates:
    verdict = AuthorityVerdict.VALIDATED if validated else AuthorityVerdict.PARTIAL
    return CapabilityGates(
        (
            AuthorityRecord(
                authority=EXECUTION_OPEN_AND_CN_SESSION_STATE,
                verdict=verdict,
                version="2026-08-12/v1",
                record_digest="verdict-sha256:" + "a" * 64,
            ),
        )
    )


def make_order(store: RuntimeStore, order_id: str = "order-1") -> PendingOrder:
    order = PendingOrder(
        order_id=order_id,
        run_id=RUN_ID,
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        target_weight=Decimal("0.05"),
        intended_session_date=NEXT_SESSION,
        ordinal=1,
    )
    store.persist_orders(
        run_id=RUN_ID, orders=(order,), digest=order_set_digest_for(RUN_ID, (order,)), now=AS_OF
    )
    return order


def filled(order_id: str) -> Fill:
    return Fill(
        status=FillStatus.FILLED,
        order_id=order_id,
        account_id="paper-us-v1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        price=Decimal("200"),
        fees=Decimal("0"),
        session_date=NEXT_SESSION,
        reason=None,
    )


def rejected(order_id: str) -> Fill:
    return Fill(
        status=FillStatus.REJECTED,
        order_id=order_id,
        account_id="paper-us-v1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal("0"),
        price=None,
        fees=Decimal("0"),
        session_date=None,
        reason="unaffordable",
    )


def build(tmp_path: Path, validated: bool, executor) -> tuple[ExecutionAdapter, RuntimeStore]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    ledger_store = LedgerStore(store)
    adapter = ExecutionAdapter(
        store=store, ledger_store=ledger_store, gates=gates(validated), executor=executor
    )
    return adapter, store


# ── fail-closed gate blocks fills ──────────────────────────────────────────


def test_blocked_gate_produces_no_fill(tmp_path: Path) -> None:
    adapter, store = build(tmp_path, validated=False, executor=lambda oid, d: filled(oid))
    order = make_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    result = adapter.execute(order_id=order.order_id, intended_session_date=NEXT_SESSION, now=AS_OF)
    assert result.outcome is ExecutionOutcome.BLOCKED
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[0][0].status.value == "PENDING"


# ── validated gate fills and finalizes ─────────────────────────────────────


def test_validated_fill_finalizes_order(tmp_path: Path) -> None:
    adapter, store = build(tmp_path, validated=True, executor=lambda oid, d: filled(oid))
    order = make_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    result = adapter.execute(order_id=order.order_id, intended_session_date=NEXT_SESSION, now=AS_OF)
    assert result.outcome is ExecutionOutcome.FILLED
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[0][0].status.value == "FINALIZED_FILLED"


def test_rejected_order_is_terminally_rejected(tmp_path: Path) -> None:
    adapter, store = build(tmp_path, validated=True, executor=lambda oid, d: rejected(oid))
    order = make_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    result = adapter.execute(order_id=order.order_id, intended_session_date=NEXT_SESSION, now=AS_OF)
    assert result.outcome is ExecutionOutcome.REJECTED
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[0][0].status.value == "FINALIZED_REJECTED"


# ── same execution cannot fill twice ───────────────────────────────────────


def test_same_order_cannot_fill_twice(tmp_path: Path) -> None:
    adapter, store = build(tmp_path, validated=True, executor=lambda oid, d: filled(oid))
    order = make_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    adapter.execute(order_id=order.order_id, intended_session_date=NEXT_SESSION, now=AS_OF)
    # A second fill attempt is rejected because the order is already finalized.
    with pytest.raises(StoreError):
        adapter.execute(order_id=order.order_id, intended_session_date=NEXT_SESSION, now=AS_OF)
