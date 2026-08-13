from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from stock_agent.domain import Currency, Market

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RuntimeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        strict=True,
    )

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if update:
            raise TypeError("immutable runtime models do not support copy updates")
        return super().model_copy(update=update, deep=deep)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None:
            raise TypeError("immutable runtime models do not support partial copies")
        if update:
            raise TypeError("immutable runtime models do not support copy updates")
        return super().copy(
            include=include,
            exclude=exclude,
            update=None if update is None else dict(update),
            deep=deep,
        )


class MarketAccountProfile(RuntimeModel):
    account_id: NonEmptyStr
    market: Market
    currency: Currency
    initial_cash: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def currency_matches_market(self) -> MarketAccountProfile:
        expected = {Market.CN: Currency.CNY, Market.US: Currency.USD}[self.market]
        if self.currency is not expected:
            raise ValueError(f"{self.market} account must use {expected}")
        return self


class MarketDataProviderProfile(RuntimeModel):
    provider_id: NonEmptyStr
    market: Market
    keychain_service: NonEmptyStr | None = None
    keychain_account: NonEmptyStr | None = None


class ModelRuntimeProfile(RuntimeModel):
    provider_id: NonEmptyStr
    model: NonEmptyStr
    max_tokens: int = Field(gt=0)
    identity_policy_id: NonEmptyStr
    prompt_template_id: NonEmptyStr
    keychain_service: NonEmptyStr
    keychain_account: NonEmptyStr


class StrategyRuntimeProfile(RuntimeModel):
    strategy_id: NonEmptyStr
    config_version: NonEmptyStr
    short_window: int = Field(gt=0)
    long_window: int = Field(gt=0)
    volume_window: int = Field(gt=0)
    volume_confirmation_threshold: Decimal = Field(ge=0)
    offensive_target_weight: Decimal = Field(gt=0, le=1)
    neutral_target_weight: Decimal = Field(ge=0, le=1)
    model_identity_policy_id: NonEmptyStr
    prompt_template_id: NonEmptyStr
    prompt_template_digest: NonEmptyStr

    @model_validator(mode="after")
    def strategy_values_are_consistent(self) -> Self:
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be less than long_window")
        if self.neutral_target_weight > self.offensive_target_weight:
            raise ValueError("neutral target cannot exceed offensive target")
        return self

    @property
    def required_history(self) -> int:
        return max(self.long_window, self.volume_window + 1)


class ScheduleReference(RuntimeModel):
    market: Market
    version: NonEmptyStr
    authority_status: NonEmptyStr


class KeychainItemProfile(RuntimeModel):
    purpose: NonEmptyStr
    service: NonEmptyStr
