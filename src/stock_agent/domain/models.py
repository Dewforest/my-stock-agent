from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
UnitDecimal = Annotated[Decimal, Field(ge=0, le=1)]
PercentageInt = Annotated[int, Field(ge=0, le=100)]


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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

    symbol: NonEmptyStr
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
    symbol: NonEmptyStr
    market: Market
    session_date: date
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal
    available_at: AwareDatetime

    @model_validator(mode="after")
    def ohlc_is_consistent(self) -> Self:
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be at most open, high, and close")
        return self


class Position(_ImmutableModel):
    symbol: NonEmptyStr
    quantity: PositiveDecimal
    average_cost: PositiveDecimal
    market_value: NonNegativeDecimal


class PortfolioSnapshot(_ImmutableModel):
    account_id: NonEmptyStr
    market: Market
    cash: NonNegativeDecimal
    nav: NonNegativeDecimal
    peak_nav: NonNegativeDecimal
    positions: tuple[Position, ...] = ()
    as_of: AwareDatetime

    @model_validator(mode="after")
    def portfolio_is_consistent(self) -> Self:
        if self.peak_nav < self.nav:
            raise ValueError("peak_nav must be at least nav")
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("position symbols must be unique")
        return self


class StrategyIntent(_ImmutableModel):
    strategy_id: NonEmptyStr
    symbol: NonEmptyStr
    market: Market
    side: Side
    target_weight: UnitDecimal
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
