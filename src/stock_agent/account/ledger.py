from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_agent.domain import Market

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)
]


def _validate_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")

    decimal_tuple = value.as_tuple()
    digits = decimal_tuple.digits
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("value must be finite")

    trailing_zeroes = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    effective_scale = 0 if trailing_zeroes == len(digits) else max(0, -exponent - trailing_zeroes)
    if effective_scale > 12:
        raise ValueError("value must have at most 12 effective decimal places")
    if value.copy_abs() >= Decimal("1E26"):
        raise ValueError("absolute value must be less than 1E26")
    return value


SupportedDecimal = Annotated[Decimal, BeforeValidator(_validate_decimal)]
PositiveDecimal = Annotated[SupportedDecimal, Field(gt=0)]
NonNegativeDecimal = Annotated[SupportedDecimal, Field(ge=0)]


class _LedgerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: NonEmptyStr
    account_id: NonEmptyStr
    market: Market
    occurred_at: AwareDatetime


class CashInitialized(_LedgerEvent):
    amount: NonNegativeDecimal


class _FillEvent(_LedgerEvent):
    symbol: Symbol
    session_date: date
    quantity: PositiveDecimal
    price: PositiveDecimal
    fees: NonNegativeDecimal

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value


class BuyFilled(_FillEvent):
    pass


class SellFilled(_FillEvent):
    pass


class PositionMarked(_LedgerEvent):
    symbol: Symbol
    session_date: date
    price: PositiveDecimal

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value


class CashAdjusted(_LedgerEvent):
    amount: SupportedDecimal
    reason: NonEmptyStr

    @field_validator("amount")
    @classmethod
    def amount_is_non_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value


class EventReversed(_LedgerEvent):
    target_event_id: NonEmptyStr
    reason: NonEmptyStr

    @model_validator(mode="after")
    def target_is_not_self(self) -> Self:
        if self.target_event_id == self.event_id:
            raise ValueError("an event cannot reverse itself")
        return self


LedgerEvent = (
    CashInitialized | BuyFilled | SellFilled | CashAdjusted | PositionMarked | EventReversed
)
