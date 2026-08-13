from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from stock_agent.domain import Market, Side
from stock_agent.runtime.capability_gates import (
    EXECUTION_OPEN_AND_CN_SESSION_STATE,
    AuthorityRecord,
    AuthorityVerdict,
    CapabilityGates,
    ExecutionAuthorityBlockedError,
)
from stock_agent.runtime.orders import order_set_digest_for
from stock_agent.runtime.state import PendingOrder
from stock_agent.runtime.store import RuntimeStore, StoreError

AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
NEXT_SESSION = date(2026, 8, 14)


def record(authority: str, verdict: AuthorityVerdict) -> AuthorityRecord:
    return AuthorityRecord(
        authority=authority,
        verdict=verdict,
        version="2026-08-12/v1",
        record_digest="verdict-sha256:" + "a" * 64,
    )


def gates(**verdicts: AuthorityVerdict) -> CapabilityGates:
    return CapabilityGates(tuple(record(k, v) for k, v in verdicts.items()))


def make_pending_order(store: RuntimeStore, order_id: str = "order-1") -> PendingOrder:
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
    digest = order_set_digest_for(RUN_ID, (order,))
    store.persist_orders(run_id=RUN_ID, orders=(order,), digest=digest, now=AS_OF)
    return order


# ── capability gate fail-closed ────────────────────────────────────────────


def test_partial_verdict_is_not_validated() -> None:
    gates_ = gates(
        EXECUTION_OPEN_AND_CN_SESSION_STATE=AuthorityVerdict.PARTIAL_WITH_CN_PRICE_LIMIT_INVALIDATED
    )
    assert not gates_.is_validated(EXECUTION_OPEN_AND_CN_SESSION_STATE)
    with pytest.raises(ExecutionAuthorityBlockedError):
        gates_.require_execution_open()


def test_validated_verdict_admits() -> None:
    gates_ = gates(**{EXECUTION_OPEN_AND_CN_SESSION_STATE: AuthorityVerdict.VALIDATED})
    assert gates_.is_validated(EXECUTION_OPEN_AND_CN_SESSION_STATE)
    gates_.require_execution_open()


def test_close_published_ohlcv_cannot_admit_execution_open() -> None:
    # A close-published daily row's open field is not an execution-open authority.
    gates_ = gates(
        EXECUTION_OPEN_AND_CN_SESSION_STATE=AuthorityVerdict.PARTIAL_WITH_CN_PRICE_LIMIT_INVALIDATED
    )
    with pytest.raises(ExecutionAuthorityBlockedError):
        gates_.require_execution_open()


# ── blocked-data obligation, never a fill while gate is closed ─────────────


def test_pending_order_gains_blocked_data_not_fill(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    order = make_pending_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    store.block_obligation_data(
        order_id=order.order_id,
        missing_authority=EXECUTION_OPEN_AND_CN_SESSION_STATE,
        error_digest="digest-1",
        now=AS_OF,
    )
    assert store.get_obligation_status(order.order_id) == "BLOCKED_DATA"
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None
    assert loaded[0][0].status.value == "PENDING"  # order stays pending


def test_authority_amendment_retries_same_obligation(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    order = make_pending_order(store)
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
    store.mark_obligation_ready(order_id=order.order_id, now=AS_OF)
    assert store.get_obligation_status(order.order_id) == "READY"


# ── terminal pairings are atomic and mutually exclusive ───────────────────


def test_fill_finalizes_order_atomically(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    order = make_pending_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    store.finalize_fill(order_id=order.order_id, now=AS_OF)
    assert store.get_obligation_status(order.order_id) == "FINALIZED_FILLED"
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[0][0].status.value == "FINALIZED_FILLED"


def test_expired_and_cancelled_pair_with_order_finalized(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    order = make_pending_order(store, "order-a")
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-a",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    store.terminate_expired(order_id=order.order_id, now=AS_OF)
    assert store.get_obligation_status(order.order_id) == "TERMINATED_EXPIRED"
    loaded = store.load_orders(RUN_ID)
    assert loaded is not None and loaded[0][0].status.value == "FINALIZED_EXPIRED"


def test_terminal_obligation_cannot_be_finalized_again(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    order = make_pending_order(store)
    store.create_execution_obligation(
        order_id=order.order_id,
        obligation_id="obligation-1",
        intended_session_date=NEXT_SESSION,
        now=AS_OF,
    )
    store.finalize_fill(order_id=order.order_id, now=AS_OF)
    with pytest.raises(StoreError):
        store.terminate_expired(order_id=order.order_id, now=AS_OF)


def test_finalize_requires_pending_order(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    with pytest.raises(StoreError):
        store.finalize_fill(order_id="nonexistent", now=AS_OF)
