from collections.abc import Mapping
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, StrategyIntent


class _ImmutableBoundaryModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
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
            raise TypeError("immutable strategy boundary models do not support copy updates")
        return super().copy(
            include=include,
            exclude=exclude,
            update=update,
            deep=deep,
        )

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update:
            raise TypeError("immutable strategy boundary models do not support copy updates")
        return super().model_copy(update=update, deep=deep)


class MarketSnapshot(_ImmutableBoundaryModel):
    as_of: AwareDatetime
    market: Market
    bars: tuple[Bar, ...]

    @field_validator("market", mode="before")
    @classmethod
    def market_has_exact_type(cls, value: object) -> object:
        if type(value) is not Market:
            raise ValueError("market must be a Market")
        return value

    @field_validator("bars", mode="before")
    @classmethod
    def bars_have_exact_types(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("bars must be a tuple")
        if any(type(item) is not Bar for item in value):
            raise ValueError("bars must contain Bar values")
        return value

    @model_validator(mode="after")
    def bars_are_point_in_time_and_ordered(self) -> Self:
        if any(item.market is not self.market for item in self.bars):
            raise ValueError("bar markets must match the snapshot market")
        if any(item.available_at > self.as_of for item in self.bars):
            raise ValueError("bars cannot be available after the snapshot as_of")

        symbol_sessions = tuple((item.symbol, item.session_date) for item in self.bars)
        if len(symbol_sessions) != len(set(symbol_sessions)):
            raise ValueError("bar symbol and session_date pairs must be unique")

        sort_keys = tuple(
            (item.symbol, item.session_date, item.available_at) for item in self.bars
        )
        if sort_keys != tuple(sorted(sort_keys)):
            raise ValueError("bars must be sorted by symbol, session_date, and available_at")
        return self


class StrategyContext(_ImmutableBoundaryModel):
    market_snapshot: MarketSnapshot
    portfolio: PortfolioSnapshot
    strategy_config_version: str

    @field_validator("market_snapshot", mode="before")
    @classmethod
    def market_snapshot_has_exact_type(cls, value: object) -> object:
        if type(value) is not MarketSnapshot:
            raise ValueError("market_snapshot must be a MarketSnapshot")
        return value

    @field_validator("portfolio", mode="before")
    @classmethod
    def portfolio_has_exact_nested_types(cls, value: object) -> object:
        if type(value) is not PortfolioSnapshot:
            raise ValueError("portfolio must be a PortfolioSnapshot")
        if type(value.positions) is not tuple:
            raise ValueError("portfolio positions must be a tuple")
        if any(type(item) is not Position for item in value.positions):
            raise ValueError("portfolio positions must contain Position values")
        return value

    @field_validator("strategy_config_version", mode="before")
    @classmethod
    def config_version_is_exact_nonblank_text(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("strategy_config_version must be a string")
        stripped = value.strip()
        if not stripped:
            raise ValueError("strategy_config_version must not be blank")
        return stripped

    @model_validator(mode="after")
    def snapshot_and_portfolio_share_valuation_instant(self) -> Self:
        if self.portfolio.market is not self.market_snapshot.market:
            raise ValueError("portfolio and market snapshot markets must match")
        if self.portfolio.as_of != self.market_snapshot.as_of:
            raise ValueError("portfolio and market snapshot as_of values must match")
        return self


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str
    config_version: str

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]: ...
