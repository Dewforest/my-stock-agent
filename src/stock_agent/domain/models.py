from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)
]
StrictPositiveDecimal = Annotated[Decimal, Field(strict=True, gt=0)]
StrictNonNegativeDecimal = Annotated[Decimal, Field(strict=True, ge=0)]
StrictUnitDecimal = Annotated[Decimal, Field(strict=True, ge=0, le=1)]
PercentageInt = Annotated[int, Field(ge=0, le=100)]


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("immutable domain models do not support copy updates")
        return super().copy(
            include=include,
            exclude=exclude,
            update=update,
            deep=deep,
        )

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if update:
            raise TypeError("immutable domain models do not support copy updates")
        return super().model_copy(update=update, deep=deep)


class Market(StrEnum):
    CN = "CN"
    US = "US"


class Currency(StrEnum):
    CNY = "CNY"
    USD = "USD"


class Side(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"


class Instrument(_ImmutableModel):

    symbol: Symbol
    market: Market
    currency: Currency
    sector: NonEmptyStr

    @model_validator(mode="after")
    def currency_matches_market(self) -> Self:
        expected_currency = {Market.CN: Currency.CNY, Market.US: Currency.USD}[self.market]
        if self.currency != expected_currency:
            raise ValueError(f"{self.market} instruments must use {expected_currency}")
        return self


class Bar(_ImmutableModel):
    symbol: Symbol
    market: Market
    session_date: date
    open: StrictPositiveDecimal
    high: StrictPositiveDecimal
    low: StrictPositiveDecimal
    close: StrictPositiveDecimal
    volume: StrictNonNegativeDecimal
    available_at: AwareDatetime

    @model_validator(mode="after")
    def bar_is_consistent(self) -> Self:
        if self.available_at.date() < self.session_date:
            raise ValueError("available_at cannot be before session_date")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be at most open, high, and close")
        return self


class Position(_ImmutableModel):
    symbol: Symbol
    quantity: StrictPositiveDecimal
    average_cost: StrictPositiveDecimal
    market_value: StrictNonNegativeDecimal


class PortfolioSnapshot(_ImmutableModel):
    account_id: NonEmptyStr
    market: Market
    cash: StrictNonNegativeDecimal
    nav: StrictNonNegativeDecimal
    peak_nav: StrictNonNegativeDecimal
    positions: tuple[Position, ...] = ()
    as_of: AwareDatetime

    @field_validator("positions", mode="wrap")
    @classmethod
    def preserve_position_subclasses_for_boundary_rejection(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> object:
        if type(value) is tuple and value and all(
            isinstance(item, Position) and type(item) is not Position for item in value
        ):
            return value
        return handler(value)

    @model_validator(mode="after")
    def portfolio_is_consistent(self) -> Self:
        positions_value = sum(
            (position.market_value for position in self.positions), start=Decimal(0)
        )
        if self.nav != self.cash + positions_value:
            raise ValueError("nav must equal cash plus total position market value")
        if self.peak_nav < self.nav:
            raise ValueError("peak_nav must be at least nav")
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("position symbols must be unique")
        return self


class StrategyIntent(_ImmutableModel):
    strategy_id: NonEmptyStr
    symbol: Symbol
    market: Market
    side: Side
    target_weight: StrictUnitDecimal
    confidence: PercentageInt
    as_of: AwareDatetime
    thesis: NonEmptyStr
    invalidation: NonEmptyStr
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def sell_has_zero_target_weight(self) -> Self:
        if self.side is Side.SELL and self.target_weight != Decimal(0):
            raise ValueError("SELL intents must have zero target weight")
        return self
