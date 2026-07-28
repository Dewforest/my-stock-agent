from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_agent.domain import Market, Side

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)
]
StrictPositiveDecimal = Annotated[Decimal, Field(strict=True, gt=0)]
StrictNonNegativeDecimal = Annotated[Decimal, Field(strict=True, ge=0)]


class FillStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OrderIntent(_ImmutableModel):
    order_id: NonEmptyStr
    account_id: NonEmptyStr
    symbol: Symbol
    market: Market
    side: Side
    quantity: StrictPositiveDecimal


class Fill(_ImmutableModel):
    status: FillStatus
    order_id: NonEmptyStr
    account_id: NonEmptyStr
    symbol: Symbol
    market: Market
    side: Side
    requested_quantity: StrictPositiveDecimal
    filled_quantity: StrictNonNegativeDecimal
    price: StrictPositiveDecimal | None
    fees: StrictNonNegativeDecimal
    session_date: date | None
    reason: NonEmptyStr | None

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if value is not None and type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value

    @model_validator(mode="after")
    def state_is_consistent(self) -> Self:
        zero = Decimal("0")
        if self.status is FillStatus.PENDING:
            if (
                self.filled_quantity != zero
                or self.price is not None
                or self.session_date is not None
                or self.reason is not None
                or self.fees != zero
            ):
                raise ValueError("PENDING fill must not contain execution results")
        elif self.status is FillStatus.REJECTED:
            if (
                self.filled_quantity != zero
                or self.price is not None
                or self.session_date is not None
                or self.fees != zero
                or self.reason is None
            ):
                raise ValueError("REJECTED fill must contain only a reason")
        elif (
            self.filled_quantity != self.requested_quantity
            or self.price is None
            or self.session_date is None
            or self.reason is not None
        ):
            raise ValueError("FILLED fill must contain complete execution results")
        return self
