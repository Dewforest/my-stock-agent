from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_agent.account import (
    BuyFilled,
    CashAdjusted,
    CashInitialized,
    EventReversed,
    PortfolioLedger,
    PositionMarked,
    SellFilled,
)
from stock_agent.domain import Market

BASE_TIME = datetime(2026, 7, 28, 12, tzinfo=UTC)


def event(kind: type, event_id: str, seconds: int, **values: object):
    common = {
        "event_id": event_id,
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": BASE_TIME + timedelta(seconds=seconds),
    }
    common.update(values)
    return kind(**common)


def ledger_with_cash(amount: str = "1000") -> PortfolioLedger:
    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(event(CashInitialized, "init", 0, amount=Decimal(amount)))
    return ledger


def buy(event_id: str = "buy", seconds: int = 1, **values: object) -> BuyFilled:
    defaults = {
        "symbol": "AAPL",
        "session_date": date(2026, 7, 28),
        "quantity": Decimal("2"),
        "price": Decimal("100"),
        "fees": Decimal("1"),
    }
    defaults.update(values)
    return event(BuyFilled, event_id, seconds, **defaults)


def sell(event_id: str = "sell", seconds: int = 2, **values: object) -> SellFilled:
    defaults = {
        "symbol": "AAPL",
        "session_date": date(2026, 7, 28),
        "quantity": Decimal("1"),
        "price": Decimal("120"),
        "fees": Decimal("2"),
    }
    defaults.update(values)
    return event(SellFilled, event_id, seconds, **defaults)


def reverse(target: str, event_id: str = "reverse", seconds: int = 3) -> EventReversed:
    return event(
        EventReversed,
        event_id,
        seconds,
        target_event_id=target,
        reason="correction",
    )


def state(ledger: PortfolioLedger) -> tuple[object, ...]:
    return (
        ledger.events,
        ledger.cash,
        ledger.positions,
        ledger.lots,
        ledger.realized_pnl,
        ledger.snapshot(),
    )


def test_append_rejects_earlier_fold_instant_atomically() -> None:
    new_york = ZoneInfo("America/New_York")
    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(
        event(
            CashInitialized,
            "init",
            0,
            occurred_at=datetime(2026, 11, 1, 0, 30, tzinfo=new_york),
            amount=Decimal("1000"),
        )
    )
    ledger.append(
        event(
            CashAdjusted,
            "later",
            1,
            occurred_at=datetime(2026, 11, 1, 1, 15, tzinfo=new_york, fold=1),
            amount=Decimal("50"),
            reason="later instant",
        )
    )
    before = state(ledger)
    earlier = event(
        CashAdjusted,
        "earlier",
        2,
        occurred_at=datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0),
        amount=Decimal("25"),
        reason="earlier instant",
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        ledger.append(earlier)

    assert state(ledger) == before


def test_historical_snapshot_excludes_future_fold_instant() -> None:
    new_york = ZoneInfo("America/New_York")
    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(
        event(
            CashInitialized,
            "init",
            0,
            occurred_at=datetime(2026, 11, 1, 0, 30, tzinfo=new_york),
            amount=Decimal("1000"),
        )
    )
    ledger.append(
        event(
            CashAdjusted,
            "adjust",
            1,
            occurred_at=datetime(2026, 11, 1, 1, 15, tzinfo=new_york, fold=1),
            amount=Decimal("50"),
            reason="future adjustment",
        )
    )
    as_of = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0)

    snapshot = ledger.snapshot(as_of)

    assert snapshot.cash == Decimal("1000")
    assert snapshot.as_of == as_of
    assert snapshot.as_of.fold == 0


def test_append_rejects_equal_instant_with_different_offset_atomically() -> None:
    ledger = ledger_with_cash()
    first = event(
        CashAdjusted,
        "first",
        1,
        amount=Decimal("50"),
        reason="first adjustment",
    )
    ledger.append(first)
    before = state(ledger)
    equivalent = first.occurred_at.astimezone(timezone(timedelta(hours=-4)))

    with pytest.raises(ValueError, match="strictly increasing"):
        ledger.append(
            event(
                CashAdjusted,
                "equivalent",
                2,
                occurred_at=equivalent,
                amount=Decimal("25"),
                reason="same instant",
            )
        )

    assert state(ledger) == before


def test_reverse_mark_restores_the_fill_mark_and_restates_peak() -> None:
    ledger = ledger_with_cash()
    ledger.append(buy())
    ledger.append(
        event(
            PositionMarked,
            "mark",
            2,
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            price=Decimal("150"),
        )
    )
    ledger.append(reverse("mark"))

    assert ledger.positions[0].market_value == Decimal("200")
    assert ledger.snapshot().nav == Decimal("999")
    assert ledger.snapshot().peak_nav == Decimal("1000")


def test_reverse_sell_restores_position_cash_cost_and_realized_pnl() -> None:
    ledger = ledger_with_cash()
    ledger.append(buy())
    ledger.append(sell())
    ledger.append(reverse("sell"))

    assert ledger.cash == Decimal("799")
    assert ledger.realized_pnl == Decimal("0")
    assert ledger.positions[0].quantity == Decimal("2")
    assert ledger.positions[0].average_cost == Decimal("100.5")
    assert tuple((lot.quantity, lot.cost_basis) for lot in ledger.lots) == (
        (Decimal("2"), Decimal("201")),
    )
    assert ledger.snapshot().nav == Decimal("999")


def test_reverse_fifo_sell_restores_original_lot_order_and_costs() -> None:
    ledger = ledger_with_cash()
    ledger.append(
        buy(
            event_id="buy-1",
            quantity=Decimal("3"),
            price=Decimal("10"),
            fees=Decimal("0.30"),
            session_date=date(2026, 7, 27),
        )
    )
    ledger.append(
        buy(
            event_id="buy-2",
            seconds=2,
            quantity=Decimal("2"),
            price=Decimal("20"),
            fees=Decimal("0.20"),
            session_date=date(2026, 7, 28),
        )
    )
    ledger.append(
        sell(
            seconds=3,
            quantity=Decimal("4"),
            price=Decimal("30"),
            fees=Decimal("0.10"),
        )
    )

    ledger.append(reverse("sell", seconds=4))

    assert tuple(
        (lot.acquired_session, lot.quantity, lot.cost_basis) for lot in ledger.lots
    ) == (
        (date(2026, 7, 27), Decimal("3"), Decimal("30.30")),
        (date(2026, 7, 28), Decimal("2"), Decimal("40.20")),
    )
    assert ledger.realized_pnl == Decimal("0")


def test_reverse_dependency_free_buy_removes_position_and_restores_cash() -> None:
    ledger = ledger_with_cash()
    ledger.append(buy())
    ledger.append(reverse("buy", seconds=2))

    assert ledger.cash == Decimal("1000")
    assert ledger.positions == ()
    assert ledger.lots == ()
    assert ledger.snapshot().nav == Decimal("1000")


def test_reverse_buy_restores_remaining_lot_order_and_cost() -> None:
    ledger = ledger_with_cash()
    first = buy(
        event_id="buy-1",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fees=Decimal("1"),
        session_date=date(2026, 7, 27),
    )
    second = buy(
        event_id="buy-2",
        seconds=2,
        quantity=Decimal("2"),
        price=Decimal("110"),
        fees=Decimal("2"),
        session_date=date(2026, 7, 28),
    )
    ledger.append(first)
    ledger.append(second)
    third = buy(
        event_id="buy-3",
        seconds=3,
        quantity=Decimal("3"),
        price=Decimal("100"),
        fees=Decimal("3"),
        session_date=date(2026, 7, 29),
    )
    ledger.append(third)

    ledger.append(reverse("buy-2", seconds=4))

    assert tuple(
        (lot.acquired_session, lot.quantity, lot.cost_basis) for lot in ledger.lots
    ) == (
        (date(2026, 7, 27), Decimal("1"), Decimal("101")),
        (date(2026, 7, 29), Decimal("3"), Decimal("303")),
    )
    assert ledger.positions[0].quantity == Decimal("4")
    assert ledger.positions[0].average_cost == Decimal("101")


def test_historical_snapshot_keeps_fifo_aggregate_before_later_sell() -> None:
    ledger = ledger_with_cash()
    first = buy(quantity=Decimal("1"), price=Decimal("100"), fees=Decimal("0"))
    second = buy(
        event_id="buy-2",
        seconds=2,
        quantity=Decimal("1"),
        price=Decimal("200"),
        fees=Decimal("0"),
    )
    sale = sell(
        seconds=3,
        quantity=Decimal("1"),
        price=Decimal("250"),
        fees=Decimal("0"),
    )
    ledger.append(first)
    ledger.append(second)
    ledger.append(sale)

    historical = ledger.snapshot(second.occurred_at)

    assert historical.positions[0].quantity == Decimal("2")
    assert historical.positions[0].average_cost == Decimal("150")
    assert ledger.positions[0].quantity == Decimal("1")
    assert ledger.positions[0].average_cost == Decimal("200")
    assert ledger.realized_pnl == Decimal("150")


def test_historical_snapshot_changes_at_the_inclusive_reversal_boundary() -> None:
    ledger = ledger_with_cash()
    adjustment = event(
        CashAdjusted,
        "deposit",
        1,
        amount=Decimal("50"),
        reason="deposit",
    )
    reversal = reverse("deposit", seconds=2)
    ledger.append(adjustment)
    ledger.append(reversal)

    at_target = ledger.snapshot(adjustment.occurred_at)
    before = ledger.snapshot(BASE_TIME + timedelta(seconds=1, microseconds=999999))
    at_reversal = ledger.snapshot(reversal.occurred_at)
    future_time = BASE_TIME + timedelta(days=1)
    future = ledger.snapshot(future_time)

    assert at_target.cash == Decimal("1050")
    assert at_target.as_of == adjustment.occurred_at
    assert before.cash == Decimal("1050")
    assert before.as_of == BASE_TIME + timedelta(seconds=1, microseconds=999999)
    assert at_reversal.cash == Decimal("1000")
    assert at_reversal.as_of == reversal.occurred_at
    assert future.cash == Decimal("1000")
    assert future.as_of == future_time


def test_historical_snapshot_compares_instants_and_preserves_requested_offset() -> None:
    ledger = ledger_with_cash()
    ledger.append(
        event(
            CashAdjusted,
            "deposit",
            1,
            amount=Decimal("50"),
            reason="deposit",
        )
    )
    equivalent = datetime(
        2026,
        7,
        28,
        8,
        0,
        1,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    snapshot = ledger.snapshot(equivalent)

    assert snapshot.cash == Decimal("1050")
    assert snapshot.as_of == equivalent
    assert snapshot.as_of.tzinfo is equivalent.tzinfo


class _NoOffsetTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    ("as_of", "error"),
    [
        ("2026-07-28T12:00:00Z", TypeError),
        (datetime(2026, 7, 28, 12), ValueError),
        (datetime(2026, 7, 28, 12, tzinfo=_NoOffsetTimezone()), ValueError),
    ],
)
def test_historical_snapshot_rejects_invalid_as_of(
    as_of: object, error: type[Exception]
) -> None:
    ledger = ledger_with_cash()

    with pytest.raises(error):
        ledger.snapshot(as_of)  # type: ignore[arg-type]


def test_historical_snapshot_before_initialization_raises_runtime_error() -> None:
    ledger = ledger_with_cash()

    with pytest.raises(RuntimeError, match="cash has not been initialized"):
        ledger.snapshot(BASE_TIME - timedelta(microseconds=1))


def test_illegal_reversal_targets_and_duplicates_are_atomic() -> None:
    cases: list[tuple[PortfolioLedger, EventReversed]] = []

    missing = ledger_with_cash()
    cases.append((missing, reverse("missing", seconds=1)))

    initialized = ledger_with_cash()
    cases.append((initialized, reverse("init", seconds=1)))

    reversal_target = ledger_with_cash()
    reversal_target.append(
        event(
            CashAdjusted,
            "deposit",
            1,
            amount=Decimal("50"),
            reason="deposit",
        )
    )
    reversal_target.append(reverse("deposit", "reverse-1", 2))
    cases.append((reversal_target, reverse("reverse-1", "reverse-2", 3)))

    duplicate = ledger_with_cash()
    duplicate.append(
        event(
            CashAdjusted,
            "deposit",
            1,
            amount=Decimal("50"),
            reason="deposit",
        )
    )
    duplicate.append(reverse("deposit", "reverse-1", 2))
    cases.append((duplicate, reverse("deposit", "reverse-2", 3)))

    for ledger, invalid_reversal in cases:
        before = state(ledger)
        with pytest.raises(ValueError):
            ledger.append(invalid_reversal)
        assert state(ledger) == before


def test_reversing_buy_that_a_later_sell_depends_on_is_atomic() -> None:
    ledger = ledger_with_cash()
    ledger.append(buy())
    ledger.append(sell())
    before = state(ledger)

    with pytest.raises(ValueError, match="cannot sell more"):
        ledger.append(reverse("buy"))

    assert state(ledger) == before


def test_reversing_cash_needed_by_a_later_buy_is_atomic() -> None:
    ledger = ledger_with_cash("100")
    ledger.append(
        event(
            CashAdjusted,
            "deposit",
            1,
            amount=Decimal("100"),
            reason="deposit",
        )
    )
    ledger.append(
        buy(
            seconds=2,
            quantity=Decimal("1"),
            price=Decimal("150"),
            fees=Decimal("0"),
        )
    )
    before = state(ledger)

    with pytest.raises(ValueError, match="insufficient cash"):
        ledger.append(reverse("deposit"))

    assert state(ledger) == before


def test_reversal_can_be_followed_by_a_corrected_event() -> None:
    ledger = ledger_with_cash()
    original = event(
        CashAdjusted,
        "deposit-wrong",
        1,
        amount=Decimal("500"),
        reason="deposit",
    )
    reversal = reverse("deposit-wrong", seconds=2)
    corrected = event(
        CashAdjusted,
        "deposit-corrected",
        3,
        amount=Decimal("200"),
        reason="corrected deposit",
    )
    ledger.append(original)
    ledger.append(reversal)

    assert ledger.snapshot().as_of == reversal.occurred_at
    assert ledger.snapshot().peak_nav == Decimal("1000")
    assert ledger.snapshot(BASE_TIME + timedelta(seconds=1)).peak_nav == Decimal("1500")

    ledger.append(corrected)

    assert ledger.events == (ledger.events[0], original, reversal, corrected)
    assert ledger.cash == Decimal("1200")
    assert ledger.snapshot().peak_nav == Decimal("1200")
    assert ledger.snapshot().as_of == corrected.occurred_at
