from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from stock_agent.account.ledger import (
    BuyFilled,
    CashInitialized,
    PortfolioLedger,
    SellFilled,
)
from stock_agent.domain import Market
from stock_agent.runtime.ledger_store import LedgerStore, LedgerStoreError
from stock_agent.runtime.store import RuntimeStore

US = "paper-us-v1"
CN = "paper-cn-v1"


def cash_init(account_id: str, market: Market, amount: str = "1000000") -> CashInitialized:
    return CashInitialized(
        event_id=f"cash-{account_id}",
        account_id=account_id,
        market=market,
        occurred_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
        amount=Decimal(amount),
    )


def buy(
    account_id: str,
    market: Market,
    symbol: str,
    quantity: str,
    price: str,
    session: date,
    seq: int,
) -> BuyFilled:
    return BuyFilled(
        event_id=f"buy-{account_id}-{seq}",
        account_id=account_id,
        market=market,
        occurred_at=datetime(2026, 8, 2, 9, 30, tzinfo=UTC) + timedelta(minutes=seq),
        symbol=symbol,
        session_date=session,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal("0"),
    )


def sell(
    account_id: str,
    market: Market,
    symbol: str,
    quantity: str,
    price: str,
    session: date,
    seq: int,
) -> SellFilled:
    return SellFilled(
        event_id=f"sell-{account_id}-{seq}",
        account_id=account_id,
        market=market,
        occurred_at=datetime(2026, 8, 3, 9, 30, tzinfo=UTC) + timedelta(minutes=seq),
        symbol=symbol,
        session_date=session,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal("0"),
    )


def make_store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "runtime.sqlite")


# ── 1. cash initializes exactly once ───────────────────────────────────────


def test_account_initializes_cash_exactly_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    ledger_store.append_events(US, Market.US, (cash_init(US, Market.US),))
    ledger = ledger_store.rebuild_ledger(US, Market.US)
    assert ledger.cash == Decimal("1000000")
    # A second CashInitialized is rejected by the ledger replay.
    with pytest.raises(ValueError):
        PortfolioLedger.rebuild(
            US, Market.US, (cash_init(US, Market.US), cash_init(US, Market.US, amount="2000000"))
        )


# ── 2/4. cross-validate and byte-identical rebuild across restart ──────────


def test_rebuild_survives_restart_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    ledger_store = LedgerStore(store)
    events = (
        cash_init(US, Market.US),
        buy(US, Market.US, "AAPL", "100", "200", date(2026, 8, 2), 1),
        buy(US, Market.US, "MSFT", "50", "300", date(2026, 8, 2), 2),
    )
    ledger_store.append_events(US, Market.US, events)
    original = ledger_store.rebuild_ledger(US, Market.US)
    store.close()

    reopened = RuntimeStore(path)
    rebuilt = LedgerStore(reopened).rebuild_ledger(US, Market.US)
    assert rebuilt.events == original.events
    assert rebuilt.cash == original.cash
    assert rebuilt.positions == original.positions
    assert rebuilt.lots == original.lots
    assert rebuilt.snapshot().nav == original.snapshot().nav
    assert rebuilt.snapshot().peak_nav == original.snapshot().peak_nav
    assert rebuilt.realized_pnl == original.realized_pnl


# ── 3. idempotent reappend vs conflict ─────────────────────────────────────


def test_idempotent_reappend_and_conflict(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    events = (
        cash_init(US, Market.US),
        buy(US, Market.US, "AAPL", "10", "200", date(2026, 8, 2), 1),
    )
    ledger_store.append_events(US, Market.US, events)
    # Idempotent: same events reappended are a no-op.
    ledger_store.append_events(US, Market.US, events)
    assert len(ledger_store.load_events(US)) == 2

    # Conflicting content under the same event_id is rejected.
    tampered = (
        CashInitialized(
            event_id="cash-paper-us-v1",
            account_id=US,
            market=Market.US,
            occurred_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
            amount=Decimal("999999"),
        ),
    )
    with pytest.raises(LedgerStoreError):
        ledger_store.append_events(US, Market.US, tampered)


# ── 5. A-share acquisition session survives restart for T+1 ───────────────


def test_a_share_acquisition_session_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    ledger_store = LedgerStore(store)
    session = date(2026, 8, 3)
    ledger_store.append_events(
        CN,
        Market.CN,
        (cash_init(CN, Market.CN), buy(CN, Market.CN, "600519", "100", "1500", session, 1)),
    )
    store.close()

    rebuilt = LedgerStore(RuntimeStore(path)).rebuild_ledger(CN, Market.CN)
    assert len(rebuilt.lots) == 1
    assert rebuilt.lots[0].acquired_session == session


# ── 6. append batch is atomic ─────────────────────────────────────────────


def test_append_batch_is_atomic(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    # The second event has an out-of-order occurred_at, so the whole batch fails.
    bad = (
        cash_init(US, Market.US),
        buy(US, Market.US, "AAPL", "10", "200", date(2026, 8, 2), 1),
        CashInitialized(
            event_id="cash-again",
            account_id=US,
            market=Market.US,
            occurred_at=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
            amount=Decimal("1"),
        ),
    )
    with pytest.raises(LedgerStoreError):
        ledger_store.append_events(US, Market.US, bad)
    # Nothing was persisted.
    assert ledger_store.load_events(US) == ()


# ── 7. corrupted payload fails closed ─────────────────────────────────────


def test_corrupted_payload_fails_closed(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    ledger_store.append_events(US, Market.US, (cash_init(US, Market.US),))
    # Corrupt the persisted payload directly.
    store.connection.execute("UPDATE ledger_events SET payload = '{broken'")
    with pytest.raises(LedgerStoreError):
        ledger_store.load_events(US)


# ── 8. CN and US streams cannot cross ─────────────────────────────────────


def test_cn_and_us_streams_cannot_cross(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    ledger_store.append_events(US, Market.US, (cash_init(US, Market.US),))
    ledger_store.append_events(CN, Market.CN, (cash_init(CN, Market.CN),))
    assert len(ledger_store.load_events(US)) == 1
    assert len(ledger_store.load_events(CN)) == 1
    us_events = ledger_store.load_events(US)
    assert all(event.account_id == US and event.market is Market.US for event in us_events)


# ── 9. hostile Decimal context does not alter replay ──────────────────────


def test_hostile_decimal_context_does_not_alter_replay(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ledger_store = LedgerStore(store)
    events = (
        cash_init(US, Market.US, amount="1000000"),
        buy(US, Market.US, "AAPL", "100", "200.123456789", date(2026, 8, 2), 1),
    )
    ledger_store.append_events(US, Market.US, events)

    original = ledger_store.rebuild_ledger(US, Market.US)
    with localcontext() as ctx:
        ctx.prec = 2  # hostile low precision
        rebuilt = LedgerStore(store).rebuild_ledger(US, Market.US)
    assert rebuilt.cash == original.cash
    assert rebuilt.positions == original.positions
    assert rebuilt.realized_pnl == original.realized_pnl
