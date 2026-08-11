from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints, field_validator

from stock_agent.domain import Market, Side
from stock_agent.market import NoFutureSession, TradingCalendar

_MAX_DECIMAL = Decimal("1E26")
_MAX_FRACTIONAL_PLACES = 12

__all__ = [
    "CnPriceLimitState",
    "CnSessionState",
    "apply_lot_size",
    "can_sell_t1",
    "execution_block_reason",
]

Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1),
]


class CnPriceLimitState(StrEnum):
    NONE = "NONE"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"


class CnSessionState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: Symbol
    session_date: date
    suspended: StrictBool
    price_limit_state: CnPriceLimitState

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value


def _validate_decimal(value: object, *, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{name} must be a Decimal")

    decimal_value = value
    if not decimal_value.is_finite():
        raise ValueError(f"{name} must be finite")
    if decimal_value <= 0:
        raise ValueError(f"{name} must be positive")
    if decimal_value.copy_abs() >= _MAX_DECIMAL:
        raise ValueError(f"{name} must have absolute value below 1E26")

    _, digits, exponent = decimal_value.as_tuple()
    assert isinstance(exponent, int)
    trailing_zeroes = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    fractional_places = max(0, -exponent - trailing_zeroes)
    if fractional_places > _MAX_FRACTIONAL_PLACES:
        raise ValueError(f"{name} must be exactly representable to 12 decimal places")
    return decimal_value


def _positive_decimal_as_ratio(value: Decimal) -> tuple[int, int]:
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if exponent >= 0:
        return coefficient * (10**exponent), 1
    return coefficient, 10 ** (-exponent)


def apply_lot_size(
    *,
    side: Side,
    quantity: Decimal,
    lot_size: Decimal = Decimal("100"),
) -> Decimal:
    quantity = _validate_decimal(quantity, name="quantity")
    lot_size = _validate_decimal(lot_size, name="lot_size")

    lot_numerator, lot_denominator = _positive_decimal_as_ratio(lot_size)
    if lot_numerator % lot_denominator:
        raise ValueError("lot_size must be an integer")

    if side is Side.SELL:
        return quantity
    if side is not Side.BUY:
        raise ValueError("side must be BUY or SELL")

    quantity_numerator, quantity_denominator = _positive_decimal_as_ratio(quantity)
    integer_lot = lot_numerator // lot_denominator
    lots = quantity_numerator // (quantity_denominator * integer_lot)
    return Decimal(lots * integer_lot)


def can_sell_t1(
    *,
    acquired_session: date,
    sell_session: date,
    calendar: TradingCalendar,
) -> bool:
    if type(acquired_session) is not date or type(sell_session) is not date:
        raise TypeError("acquired_session and sell_session must be plain dates")
    if calendar.market is not Market.CN:
        raise ValueError("T+1 rule requires a China market calendar")
    if not calendar.is_session(acquired_session) or not calendar.is_session(sell_session):
        raise ValueError("both dates must be explicit calendar sessions")

    try:
        first_sell_session = calendar.next_session(acquired_session)
    except NoFutureSession:
        return False
    return sell_session >= first_sell_session


def execution_block_reason(*, side: Side, state: CnSessionState) -> str | None:
    if side is not Side.BUY and side is not Side.SELL:
        raise ValueError("side must be BUY or SELL")
    if state.suspended:
        return "instrument is suspended"
    if side is Side.BUY and state.price_limit_state is CnPriceLimitState.LIMIT_UP:
        return "buy blocked at limit up"
    if side is Side.SELL and state.price_limit_state is CnPriceLimitState.LIMIT_DOWN:
        return "sell blocked at limit down"
    return None
