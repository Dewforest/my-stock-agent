from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from typing import Any

import pytest
from pydantic import ValidationError

from stock_agent.account import (
    BookedFill,
    CashInitialized,
    EventReversed,
    OpenExecutionBatchBooked,
    PortfolioLedger,
    PortfolioMarked,
    PositionMark,
)
from stock_agent.domain import Market, Side

BASE = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
SESSION = date(2026, 7, 29)


def common(event_id: str, seconds: int) -> dict[str, object]:
    return {
        "event_id": event_id,
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": BASE + timedelta(seconds=seconds),
    }


def fill(
    fill_id: str,
    symbol: str,
    side: Side,
    quantity: str,
    price: str,
    fees: str = "0",
) -> BookedFill:
    return BookedFill(
        fill_id=fill_id,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal(fees),
    )


def mark(symbol: str, price: str) -> PositionMark:
    return PositionMark(symbol=symbol, price=Decimal(price))


def batch(
    event_id: str,
    seconds: int,
    fills: tuple[BookedFill, ...],
    marks: tuple[PositionMark, ...],
) -> OpenExecutionBatchBooked:
    return OpenExecutionBatchBooked(
        **common(event_id, seconds), session_date=SESSION, fills=fills, marks=marks
    )


def close(
    event_id: str, seconds: int, marks: tuple[PositionMark, ...]
) -> PortfolioMarked:
    return PortfolioMarked(
        **common(event_id, seconds), session_date=SESSION, marks=marks
    )


def ledger(amount: str = "1000") -> PortfolioLedger:
    result = PortfolioLedger("account-1", Market.US)
    result.append(CashInitialized(**common("init", 0), amount=Decimal(amount)))
    return result


def state(value: PortfolioLedger) -> tuple[object, ...]:
    return (
        value.events,
        value.cash,
        value.realized_pnl,
        value.positions,
        value.lots,
        value.snapshot(),
    )


def reverse(target: str, event_id: str, seconds: int) -> EventReversed:
    return EventReversed(
        **common(event_id, seconds), target_event_id=target, reason="correction"
    )


def test_items_are_exact_frozen_canonical_and_copy_safe() -> None:
    position_mark = mark(" aapl ", "100.00")
    booked_fill = fill(" fill-1 ", " msft ", Side.BUY, "1", "20", "0")

    assert position_mark.symbol == "AAPL"
    assert position_mark.price.as_tuple() == Decimal("100.00").as_tuple()
    assert booked_fill.fill_id == "fill-1"
    assert booked_fill.symbol == "MSFT"
    with pytest.raises(ValidationError):
        position_mark.price = Decimal("1")
    with pytest.raises(ValidationError):
        BookedFill(
            fill_id="fill",
            symbol="AAPL",
            side=Side.HOLD,
            quantity=Decimal("1"),
            price=Decimal("1"),
            fees=Decimal("0"),
        )
    with pytest.raises(TypeError):
        position_mark.model_copy(update={"price": Decimal("1")})
    with pytest.raises(TypeError):
        booked_fill.model_copy(update={"fees": Decimal("-1")})


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_booked_fill_accepts_only_executable_exact_side(side: Side) -> None:
    assert fill("fill", "AAPL", side, "0.000000000001", "999", "0").side is side


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (mark, "price", Decimal("0")),
        (mark, "price", Decimal("1.0000000000001")),
        (fill, "quantity", Decimal("0")),
        (fill, "price", Decimal("1E26")),
        (fill, "fees", Decimal("-1")),
    ],
)
def test_items_use_supported_decimal_boundary(
    factory: Any, field: str, value: Decimal
) -> None:
    with pytest.raises(ValidationError):
        if factory is mark:
            PositionMark(symbol="AAPL", **{field: value})
        else:
            values = {
                "fill_id": "fill",
                "symbol": "AAPL",
                "side": Side.BUY,
                "quantity": Decimal("1"),
                "price": Decimal("1"),
                "fees": Decimal("0"),
            }
            values[field] = value
            BookedFill(**values)


class PositionMarkChild(PositionMark):
    pass


class BookedFillChild(BookedFill):
    pass


def test_batch_contracts_require_exact_tuples_items_order_and_unique_ids() -> None:
    good_fill = fill("fill-1", "AAPL", Side.BUY, "1", "10")
    good_mark = mark("AAPL", "10")

    batch("batch", 1, (good_fill,), (good_mark,))
    close("close", 1, ())
    invalid = [
        {"fills": [], "marks": (good_mark,)},
        {"fills": (), "marks": (good_mark,)},
        {"fills": (good_fill, good_fill), "marks": (good_mark,)},
        {
            "fills": (
                BookedFillChild(
                    fill_id="child",
                    symbol="AAPL",
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    price=Decimal("1"),
                    fees=Decimal("0"),
                ),
            ),
            "marks": (good_mark,),
        },
        {"fills": (good_fill,), "marks": [good_mark]},
        {
            "fills": (good_fill,),
            "marks": (mark("MSFT", "10"), mark("AAPL", "10")),
        },
        {"fills": (good_fill,), "marks": (good_mark, good_mark)},
        {
            "fills": (good_fill,),
            "marks": (
                PositionMarkChild(symbol="AAPL", price=Decimal("10")),
            ),
        },
    ]
    for values in invalid:
        with pytest.raises(ValidationError):
            OpenExecutionBatchBooked(
                **common("batch", 1), session_date=SESSION, **values
            )


@pytest.mark.parametrize("session_date", [datetime(2026, 7, 29, tzinfo=UTC), "2026-07-29"])
def test_batch_events_require_plain_session_date(session_date: object) -> None:
    with pytest.raises(ValidationError):
        PortfolioMarked(**common("close", 1), session_date=session_date, marks=())


def test_nested_model_construct_pollution_is_revalidated() -> None:
    corrupted_mark = PositionMark.model_construct(symbol="AAPL", price=Decimal("-1"))
    corrupted_fill = BookedFill.model_construct(
        fill_id="fill",
        symbol="AAPL",
        side=Side.HOLD,
        quantity=Decimal("1"),
        price=Decimal("1"),
        fees=Decimal("0"),
    )
    with pytest.raises(ValidationError):
        close("close", 1, (corrupted_mark,))
    with pytest.raises(ValidationError):
        batch("batch", 1, (corrupted_fill,), (mark("AAPL", "1"),))


def test_open_batch_applies_ordered_buy_sell_fifo_fees_and_complete_marks() -> None:
    value = ledger("1000")
    event = batch(
        "batch",
        1,
        (
            fill("buy-1", "AAPL", Side.BUY, "3", "10", "0.30"),
            fill("buy-2", "AAPL", Side.BUY, "2", "20", "0.20"),
            fill("sell-1", "AAPL", Side.SELL, "4", "30", "0.10"),
            fill("buy-msft", "MSFT", Side.BUY, "2", "50", "1"),
        ),
        (mark("AAPL", "31"), mark("MSFT", "51")),
    )

    value.append(event)

    assert value.cash == Decimal("948.40")
    assert value.realized_pnl == Decimal("69.50")
    assert tuple((lot.symbol, lot.quantity, lot.cost_basis) for lot in value.lots) == (
        ("AAPL", Decimal("1"), Decimal("20.10")),
        ("MSFT", Decimal("2"), Decimal("101")),
    )
    assert tuple((position.symbol, position.market_value) for position in value.positions) == (
        ("AAPL", Decimal("31")),
        ("MSFT", Decimal("102")),
    )
    assert value.snapshot().nav == Decimal("1081.40")
    assert value.snapshot().peak_nav == Decimal("1081.40")


def test_open_batch_does_not_create_mixed_price_false_peak() -> None:
    value = ledger("1000")
    value.append(
        batch(
            "buy-both",
            1,
            (
                fill("a", "AAPL", Side.BUY, "1", "100"),
                fill("m", "MSFT", Side.BUY, "1", "100"),
            ),
            (mark("AAPL", "100"), mark("MSFT", "100")),
        )
    )
    value.append(
        batch(
            "rotate",
            2,
            (
                fill("a-more", "AAPL", Side.BUY, "1", "200"),
                fill("m-part", "MSFT", Side.SELL, "0.5", "50"),
            ),
            (mark("AAPL", "200"), mark("MSFT", "50")),
        )
    )

    assert value.snapshot().nav == Decimal("1050")
    assert value.snapshot().peak_nav == Decimal("1050")


@pytest.mark.parametrize(
    "bad_event",
    [
        batch(
            "missing",
            1,
            (fill("buy", "AAPL", Side.BUY, "1", "10"),),
            (),
        ),
        batch(
            "extra",
            1,
            (fill("buy", "AAPL", Side.BUY, "1", "10"),),
            (mark("AAPL", "10"), mark("MSFT", "20")),
        ),
        batch(
            "cash",
            1,
            (fill("buy", "AAPL", Side.BUY, "1000", "10"),),
            (mark("AAPL", "10"),),
        ),
        batch(
            "oversell",
            1,
            (fill("sell", "AAPL", Side.SELL, "1", "10"),),
            (),
        ),
    ],
)
def test_open_batch_failures_are_atomic(bad_event: OpenExecutionBatchBooked) -> None:
    value = ledger()
    before = state(value)
    with pytest.raises(ValueError):
        value.append(bad_event)
    assert state(value) == before


def test_fill_ids_are_globally_unique_across_batches() -> None:
    value = ledger()
    value.append(
        batch(
            "first", 1, (fill("same", "AAPL", Side.BUY, "1", "10"),), (mark("AAPL", "10"),)
        )
    )
    before = state(value)
    with pytest.raises(ValueError, match="fill_id"):
        value.append(
            batch(
                "second", 2, (fill("same", "MSFT", Side.BUY, "1", "10"),),
                (mark("AAPL", "10"), mark("MSFT", "10")),
            )
        )
    assert state(value) == before


def test_portfolio_mark_is_atomic_complete_and_supports_all_cash() -> None:
    cash_only = ledger()
    cash_only.append(close("cash-close", 1, ()))
    assert cash_only.snapshot().as_of == BASE + timedelta(seconds=1)

    value = ledger()
    value.append(
        batch(
            "buy", 1,
            (
                fill("a", "AAPL", Side.BUY, "1", "100"),
                fill("m", "MSFT", Side.BUY, "1", "100"),
            ),
            (mark("AAPL", "100"), mark("MSFT", "100")),
        )
    )
    value.append(close("close", 2, (mark("AAPL", "200"), mark("MSFT", "50"))))
    assert value.snapshot().nav == Decimal("1050")
    assert value.snapshot().peak_nav == Decimal("1050")
    assert value.snapshot(BASE + timedelta(seconds=2)) == value.snapshot()


@pytest.mark.parametrize(
    "marks",
    [(), (mark("MSFT", "100"),), (mark("AAPL", "100"), mark("MSFT", "100"))],
)
def test_portfolio_mark_rejects_incomplete_or_extra_symbols_atomically(
    marks: tuple[PositionMark, ...],
) -> None:
    value = ledger()
    value.append(
        batch(
            "buy", 1, (fill("a", "AAPL", Side.BUY, "1", "100"),), (mark("AAPL", "100"),)
        )
    )
    before = state(value)
    with pytest.raises(ValueError):
        value.append(close("bad-close", 2, marks))
    assert state(value) == before


def test_open_batch_is_one_reversible_unit() -> None:
    value = ledger()
    opened = batch(
        "open", 1, (fill("fill", "AAPL", Side.BUY, "1", "100"),), (mark("AAPL", "100"),)
    )
    value.append(opened)
    value.append(reverse("open", "reverse-open", 2))
    assert value.cash == Decimal("1000")
    assert value.positions == ()


def test_lone_upstream_reversal_fails_when_complete_mark_depends_on_it() -> None:
    value = ledger()
    value.append(
        batch(
            "open", 1, (fill("fill", "AAPL", Side.BUY, "1", "100"),), (mark("AAPL", "100"),)
        )
    )
    value.append(close("close", 2, (mark("AAPL", "110"),)))
    before = state(value)
    with pytest.raises(ValueError):
        value.append(reverse("open", "reverse-open", 3))
    assert state(value) == before


def test_append_many_contract_empty_noop_and_atomic_failure() -> None:
    value = ledger()
    before = state(value)
    value.append_many(())
    assert state(value) == before

    with pytest.raises(TypeError):
        value.append_many([])  # type: ignore[arg-type]
    assert state(value) == before

    events = (
        close("close", 1, ()),
        PortfolioMarked(
            **(common("wrong-account", 2) | {"account_id": "other"}),
            session_date=SESSION,
            marks=(),
        ),
    )
    with pytest.raises(ValueError):
        value.append_many(events)
    assert state(value) == before


class PortfolioMarkedChild(PortfolioMarked):
    pass


def test_append_many_rejects_event_subclasses_duplicate_ids_and_time_atomically() -> None:
    cases: tuple[tuple[object, ...], ...] = (
        (PortfolioMarkedChild(**common("child", 1), session_date=SESSION, marks=()),),
        (close("same", 1, ()), close("same", 2, ())),
        (close("later", 2, ()), close("earlier", 1, ())),
    )
    for events in cases:
        value = ledger()
        before = state(value)
        with pytest.raises((TypeError, ValueError)):
            value.append_many(events)  # type: ignore[arg-type]
        assert state(value) == before


def test_append_many_can_atomically_reverse_dependencies_and_replace_stream() -> None:
    value = ledger()
    value.append(
        batch(
            "wrong-open", 1,
            (fill("wrong-fill", "AAPL", Side.BUY, "1", "100"),),
            (mark("AAPL", "100"),),
        )
    )
    value.append(close("wrong-close", 2, (mark("AAPL", "110"),)))

    value.append_many(
        (
            reverse("wrong-close", "reverse-close", 3),
            reverse("wrong-open", "reverse-open", 4),
            batch(
                "correct-open", 5,
                (fill("correct-fill", "MSFT", Side.BUY, "2", "50", "1"),),
                (mark("MSFT", "55"),),
            ),
            close("correct-close", 6, (mark("MSFT", "60"),)),
        )
    )

    assert value.cash == Decimal("899")
    assert value.realized_pnl == Decimal("0")
    assert value.positions[0].symbol == "MSFT"
    assert value.snapshot().nav == Decimal("1019")
    assert value.snapshot().as_of == BASE + timedelta(seconds=6)


def test_hostile_decimal_context_does_not_affect_batch_replay() -> None:
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        value = ledger()
        value.append(
            batch(
                "batch", 1,
                (
                    fill("first", "AAPL", Side.BUY, "3", "10", "0.30"),
                    fill("second", "AAPL", Side.BUY, "2", "20", "0.20"),
                    fill("sell", "AAPL", Side.SELL, "1", "30", "0.10"),
                ),
                (mark("AAPL", "25"),),
            )
        )
    assert value.realized_pnl == Decimal("19.80")
    assert value.snapshot().nav == Decimal("1059.40")
