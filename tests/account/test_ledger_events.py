from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal, InvalidOperation, localcontext
from types import UnionType
from typing import get_args

import pytest
from pydantic import ValidationError

import stock_agent.account as account
from stock_agent.account import (
    BuyFilled,
    CashAdjusted,
    CashInitialized,
    EventReversed,
    LedgerEvent,
    PortfolioLedger,
    PositionMarked,
    SellFilled,
)
from stock_agent.domain import Market


def make_cash_initialized(**overrides: object) -> CashInitialized:
    values = {
        "event_id": "event-1",
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": datetime(2026, 7, 28, 12, tzinfo=UTC),
        "amount": Decimal("1000.00"),
    }
    values.update(overrides)
    return CashInitialized(**values)


def test_cash_initialized_preserves_exact_non_negative_decimal() -> None:
    event = make_cash_initialized(amount=Decimal("1000.00"))

    assert event.amount == Decimal("1000.00")
    assert event.amount.as_tuple() == Decimal("1000.00").as_tuple()


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("0.000000000001")])
def test_cash_initialized_accepts_non_negative_boundaries(amount: Decimal) -> None:
    assert make_cash_initialized(amount=amount).amount == amount


def test_cash_initialized_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError):
        make_cash_initialized(amount=Decimal("-0.01"))


def event_fields() -> dict[str, object]:
    return {
        "event_id": "event-2",
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": datetime(2026, 7, 28, 12, tzinfo=UTC),
    }


def make_fill(event_type: type[BuyFilled] | type[SellFilled], **overrides: object):
    values = {
        **event_fields(),
        "symbol": " aapl ",
        "session_date": date(2026, 7, 28),
        "quantity": Decimal("10"),
        "price": Decimal("101.25"),
        "fees": Decimal("0.50"),
    }
    values.update(overrides)
    return event_type(**values)


@pytest.mark.parametrize("event_type", [BuyFilled, SellFilled])
def test_fill_events_capture_canonical_execution(
    event_type: type[BuyFilled] | type[SellFilled],
) -> None:
    event = make_fill(event_type)

    assert event.symbol == "AAPL"
    assert event.quantity == Decimal("10")
    assert event.price == Decimal("101.25")
    assert event.fees == Decimal("0.50")


@pytest.mark.parametrize("event_type", [BuyFilled, SellFilled])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", Decimal("0")),
        ("quantity", Decimal("-1")),
        ("price", Decimal("0")),
        ("fees", Decimal("-0.01")),
    ],
)
def test_fill_events_reject_invalid_execution_values(
    event_type: type[BuyFilled] | type[SellFilled], field: str, value: Decimal
) -> None:
    with pytest.raises(ValidationError):
        make_fill(event_type, **{field: value})


def test_position_marked_captures_canonical_symbol_and_positive_price() -> None:
    event = PositionMarked(
        **event_fields(),
        symbol=" aapl ",
        session_date=date(2026, 7, 28),
        price=Decimal("105.00"),
    )

    assert event.symbol == "AAPL"
    assert event.price.as_tuple() == Decimal("105.00").as_tuple()


def test_position_marked_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        PositionMarked(
            **event_fields(),
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            price=Decimal("0"),
        )


@pytest.mark.parametrize("amount", [Decimal("25.50"), Decimal("-25.50")])
def test_cash_adjusted_accepts_non_zero_signed_amount(amount: Decimal) -> None:
    event = CashAdjusted(**event_fields(), amount=amount, reason=" correction ")

    assert event.amount == amount
    assert event.reason == "correction"


def test_cash_adjusted_rejects_zero_amount() -> None:
    with pytest.raises(ValidationError):
        CashAdjusted(**event_fields(), amount=Decimal("0"), reason="correction")


@pytest.mark.parametrize("reason", ["", "   "])
def test_cash_adjusted_rejects_blank_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        CashAdjusted(**event_fields(), amount=Decimal("1"), reason=reason)


def test_event_reversed_strips_target_and_reason() -> None:
    event = EventReversed(
        **event_fields(), target_event_id=" original-event ", reason=" duplicate fill "
    )

    assert event.target_event_id == "original-event"
    assert event.reason == "duplicate fill"


@pytest.mark.parametrize("field", ["target_event_id", "reason"])
def test_event_reversed_rejects_blank_text(field: str) -> None:
    values = {**event_fields(), "target_event_id": "original-event", "reason": "duplicate"}
    values[field] = "   "

    with pytest.raises(ValidationError):
        EventReversed(**values)


def test_event_reversed_cannot_target_itself() -> None:
    with pytest.raises(ValidationError):
        EventReversed(**event_fields(), target_event_id="event-2", reason="duplicate")


class NullOffsetTZ(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


def make_decimal_event(kind: str, field: str, value: object):
    if kind == "cash_initialized":
        return make_cash_initialized(**{field: value})
    if kind == "cash_adjusted":
        return CashAdjusted(**event_fields(), amount=value, reason="correction")
    if kind == "position_marked":
        values = {
            **event_fields(),
            "symbol": "AAPL",
            "session_date": date(2026, 7, 28),
            "price": Decimal("100"),
            field: value,
        }
        return PositionMarked(**values)
    return make_fill(BuyFilled, **{field: value})


DECIMAL_FIELDS = [
    ("cash_initialized", "amount"),
    ("cash_adjusted", "amount"),
    ("buy_filled", "quantity"),
    ("buy_filled", "price"),
    ("buy_filled", "fees"),
    ("position_marked", "price"),
]


@pytest.mark.parametrize(("kind", "field"), DECIMAL_FIELDS)
@pytest.mark.parametrize("value", [1, 1.0, "1"])
def test_decimal_fields_reject_non_decimal_inputs(
    kind: str, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        make_decimal_event(kind, field, value)


@pytest.mark.parametrize(("kind", "field"), DECIMAL_FIELDS)
@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_decimal_fields_reject_non_finite_values_as_validation_errors(
    kind: str, field: str, value: Decimal
) -> None:
    with pytest.raises(ValidationError):
        make_decimal_event(kind, field, value)


@pytest.mark.parametrize("value", [Decimal("1.1234567890121"), Decimal("1E26")])
def test_decimal_fields_reject_unsupported_precision_and_magnitude(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_fill(BuyFilled, price=value)


@pytest.mark.parametrize(
    "value", [Decimal("1.1234567890120000"), Decimal("0E-100")]
)
def test_decimal_fields_accept_trailing_zeroes_without_quantizing(value: Decimal) -> None:
    event = make_cash_initialized(amount=value)

    assert event.amount.as_tuple() == value.as_tuple()


def test_decimal_validation_ignores_ambient_context_and_traps() -> None:
    with localcontext() as context:
        context.prec = 2
        context.traps[InvalidOperation] = True
        accepted = make_fill(BuyFilled, price=Decimal("99999999999999999999999999.999999999999"))
        with pytest.raises(ValidationError):
            make_fill(BuyFilled, price=Decimal("sNaN"))

    assert accepted.price == Decimal("99999999999999999999999999.999999999999")


@pytest.mark.parametrize("event_type", [BuyFilled, SellFilled])
@pytest.mark.parametrize(
    "session_date", [datetime(2026, 7, 28, tzinfo=UTC), "2026-07-28"]
)
def test_fill_events_require_plain_date(
    event_type: type[BuyFilled] | type[SellFilled], session_date: object
) -> None:
    with pytest.raises(ValidationError):
        make_fill(event_type, session_date=session_date)


@pytest.mark.parametrize(
    "session_date", [datetime(2026, 7, 28, tzinfo=UTC), "2026-07-28"]
)
def test_position_marked_requires_plain_date(session_date: object) -> None:
    with pytest.raises(ValidationError):
        PositionMarked(
            **event_fields(), symbol="AAPL", session_date=session_date, price=Decimal("100")
        )


@pytest.mark.parametrize(
    "occurred_at", [datetime(2026, 7, 28, 12), datetime(2026, 7, 28, 12, tzinfo=NullOffsetTZ())]
)
def test_ledger_events_require_aware_occurred_at(occurred_at: datetime) -> None:
    with pytest.raises(ValidationError):
        make_cash_initialized(occurred_at=occurred_at)


def test_common_text_is_stripped_and_non_empty() -> None:
    event = make_cash_initialized(event_id=" event-1 ", account_id=" account-1 ")

    assert event.event_id == "event-1"
    assert event.account_id == "account-1"

    for field in ("event_id", "account_id"):
        with pytest.raises(ValidationError):
            make_cash_initialized(**{field: "   "})


def test_ledger_events_are_frozen_and_forbid_extra_fields() -> None:
    event = make_cash_initialized()

    with pytest.raises(ValidationError):
        event.amount = Decimal("1")
    with pytest.raises(ValidationError):
        make_cash_initialized(source="wire")


def test_ledger_event_is_exact_python_union() -> None:
    assert isinstance(LedgerEvent, UnionType)
    assert get_args(LedgerEvent) == (
        CashInitialized,
        BuyFilled,
        SellFilled,
        CashAdjusted,
        PositionMarked,
        EventReversed,
    )


def test_account_exports_exact_public_contract() -> None:
    assert account.__all__ == [
        "CashInitialized",
        "BuyFilled",
        "SellFilled",
        "CashAdjusted",
        "PositionMarked",
        "EventReversed",
        "LedgerEvent",
        "PortfolioLedger",
    ]
    assert account.PortfolioLedger is PortfolioLedger
