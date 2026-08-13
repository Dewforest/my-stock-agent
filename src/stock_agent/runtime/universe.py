from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Annotated, Any, Self, cast

from pydantic import StringConstraints, field_validator, model_validator

from stock_agent.audit.canonical import tagged_sha256
from stock_agent.domain import Currency, Market
from stock_agent.runtime.models import (
    MarketAccountProfile,
    MarketDataProviderProfile,
    ModelRuntimeProfile,
    RuntimeModel,
    ScheduleReference,
    StrategyRuntimeProfile,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CanonicalSymbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)
]
EXPECTED_UNIVERSE_V1_DIGEST = (
    "universe-sha256:79619ee5617807e252f9a35ce6c95a4d3a43a97f85f54b300c6493d98b67a177"
)
EXPECTED_RUNTIME_CONFIG_V1_DIGEST = (
    "runtime-config-sha256:a9bcc9ab70e3d818082415f130f2ac5d546153cc992272c677feaf92ec220e14"
)


class RuntimeConfigLoadError(Exception):
    """Stable, secret-free failure for runtime configuration loading."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("paper runtime configuration could not be loaded")


class UniverseMember(RuntimeModel):
    symbol: CanonicalSymbol
    market: Market
    display_name: NonEmptyStr
    currency: Currency
    sector: NonEmptyStr
    inclusion_reason: NonEmptyStr
    effective_date: date
    source: NonEmptyStr

    @model_validator(mode="after")
    def currency_matches_market(self) -> Self:
        expected = {Market.CN: Currency.CNY, Market.US: Currency.USD}[self.market]
        if self.currency is not expected:
            raise ValueError(f"{self.market} universe members must use {expected}")
        return self


class UniverseSnapshot(RuntimeModel):
    universe_id: NonEmptyStr
    digest: NonEmptyStr
    members: tuple[UniverseMember, ...]

    @field_validator("members", mode="after")
    @classmethod
    def sort_members(cls, value: tuple[UniverseMember, ...]) -> tuple[UniverseMember, ...]:
        return tuple(sorted(value, key=lambda item: (item.market.value, item.symbol)))

    @model_validator(mode="after")
    def v1_membership_is_exact(self) -> Self:
        identities = tuple((member.market, member.symbol) for member in self.members)
        if len(identities) != len(set(identities)):
            raise ValueError("universe member market/symbol identities must be unique")
        if self.universe_id == "dual-market-20/v1":
            counts = {
                market: sum(member.market is market for member in self.members) for market in Market
            }
            if len(self.members) != 20 or counts != {Market.CN: 10, Market.US: 10}:
                raise ValueError("dual-market-20/v1 requires exactly 10 CN and 10 US members")
            if self.digest != EXPECTED_UNIVERSE_V1_DIGEST:
                raise ValueError("universe digest is not the approved v1 digest")
        if canonical_universe_digest(self) != self.digest:
            raise ValueError("universe digest does not match canonical content")
        return self


class PaperRuntimeConfig(RuntimeModel):
    schema_version: NonEmptyStr
    profile_id: NonEmptyStr
    config_digest: NonEmptyStr
    universe: UniverseSnapshot
    accounts: tuple[MarketAccountProfile, ...]
    market_data: tuple[MarketDataProviderProfile, ...]
    model: ModelRuntimeProfile
    strategy: StrategyRuntimeProfile
    schedules: tuple[ScheduleReference, ...]

    @field_validator("universe", mode="before")
    @classmethod
    def universe_is_exact(cls, value: object) -> object:
        if isinstance(value, UniverseSnapshot) and type(value) is not UniverseSnapshot:
            raise ValueError("universe must be an exact UniverseSnapshot")
        return value

    @model_validator(mode="after")
    def market_profiles_are_exact(self) -> Self:
        expected_markets = (Market.CN, Market.US)
        collections = {
            "accounts": self.accounts,
            "market_data": self.market_data,
            "schedules": self.schedules,
        }
        for label, values in collections.items():
            markets = tuple(value.market for value in values)
            if markets != expected_markets:
                raise ValueError(f"{label} must contain exact ordered CN and US bindings")
        expected_accounts = (
            ("paper-cn-v1", Market.CN, Currency.CNY, Decimal("1000000")),
            ("paper-us-v1", Market.US, Currency.USD, Decimal("100000")),
        )
        actual_accounts = tuple(
            (item.account_id, item.market, item.currency, item.initial_cash)
            for item in self.accounts
        )
        if actual_accounts != expected_accounts:
            raise ValueError("accounts do not match the approved v1 profile")
        expected_market_data = (
            ("eastmoney-daily/v1", Market.CN, None, None),
            (
                "alpha-vantage-daily/v1",
                Market.US,
                "com.dewforest.my-stock-agent.alpha-vantage",
                "amezf",
            ),
        )
        actual_market_data = tuple(
            (
                item.provider_id,
                item.market,
                item.keychain_service,
                item.keychain_account,
            )
            for item in self.market_data
        )
        if actual_market_data != expected_market_data:
            raise ValueError("market_data does not match the approved v1 profile")
        expected_model = (
            "deepseek-openai-compatible/v1",
            "deepseek-v4-pro",
            1024,
            "exact-model-v1",
            "strategy-a-openai-compatible-json/v1",
            "com.dewforest.my-stock-agent.deepseek",
            "amezf",
        )
        actual_model = (
            self.model.provider_id,
            self.model.model,
            self.model.max_tokens,
            self.model.identity_policy_id,
            self.model.prompt_template_id,
            self.model.keychain_service,
            self.model.keychain_account,
        )
        if actual_model != expected_model:
            raise ValueError("model does not match the approved v1 profile")
        expected_strategy = (
            "strategy-a-bounded-llm",
            "strategy-a-v1",
            3,
            5,
            4,
            Decimal("1.20"),
            Decimal("0.10"),
            Decimal("0.05"),
            "exact-model-v1",
            "strategy-a-openai-compatible-json/v1",
            "prompt-sha256:64cfe18f171b3ff4acdd325d7bbf1f1a4c78f571ac58a2b8af7a27fd97d49045",
        )
        actual_strategy = (
            self.strategy.strategy_id,
            self.strategy.config_version,
            self.strategy.short_window,
            self.strategy.long_window,
            self.strategy.volume_window,
            self.strategy.volume_confirmation_threshold,
            self.strategy.offensive_target_weight,
            self.strategy.neutral_target_weight,
            self.strategy.model_identity_policy_id,
            self.strategy.prompt_template_id,
            self.strategy.prompt_template_digest,
        )
        if actual_strategy != expected_strategy:
            raise ValueError("strategy does not match the approved v1 profile")
        expected_schedules = (
            (Market.CN, "cn-sse-szse-2026/research-v1", "PARTIAL"),
            (Market.US, "us-nyse-nasdaq-2026/research-v1", "PARTIAL"),
        )
        actual_schedules = tuple(
            (item.market, item.version, item.authority_status) for item in self.schedules
        )
        if actual_schedules != expected_schedules:
            raise ValueError("schedules do not match the approved research-only v1 profile")
        if (self.schema_version, self.profile_id) != (
            "paper-runtime-config/v1",
            "dual-market-paper/v1",
        ):
            raise ValueError("runtime identity does not match the approved v1 profile")
        if canonical_runtime_config_digest(self) != self.config_digest:
            raise ValueError("config digest does not match canonical content")
        if self.config_digest != EXPECTED_RUNTIME_CONFIG_V1_DIGEST:
            raise ValueError("config digest is not the approved v1 digest")
        return self


def canonical_universe_digest(snapshot: UniverseSnapshot) -> str:
    return tagged_sha256(
        "universe",
        (
            snapshot.universe_id,
            tuple(
                (
                    member.market.value,
                    member.symbol,
                    member.display_name,
                    member.currency.value,
                    member.sector,
                    member.inclusion_reason,
                    member.effective_date.isoformat(),
                    member.source,
                )
                for member in snapshot.members
            ),
        ),
    )


def canonical_runtime_config_digest(config: PaperRuntimeConfig) -> str:
    return tagged_sha256(
        "runtime-config",
        (
            config.schema_version,
            config.profile_id,
            config.universe.universe_id,
            config.universe.digest,
            tuple(
                (
                    item.account_id,
                    item.market.value,
                    item.currency.value,
                    str(item.initial_cash),
                )
                for item in config.accounts
            ),
            tuple(
                (
                    item.provider_id,
                    item.market.value,
                    item.keychain_service,
                    item.keychain_account,
                )
                for item in config.market_data
            ),
            (
                config.model.provider_id,
                config.model.model,
                config.model.max_tokens,
                config.model.identity_policy_id,
                config.model.prompt_template_id,
                config.model.keychain_service,
                config.model.keychain_account,
            ),
            (
                config.strategy.strategy_id,
                config.strategy.config_version,
                config.strategy.short_window,
                config.strategy.long_window,
                config.strategy.volume_window,
                str(config.strategy.volume_confirmation_threshold),
                str(config.strategy.offensive_target_weight),
                str(config.strategy.neutral_target_weight),
                config.strategy.model_identity_policy_id,
                config.strategy.prompt_template_id,
                config.strategy.prompt_template_digest,
            ),
            tuple(
                (item.market.value, item.version, item.authority_status)
                for item in config.schedules
            ),
        ),
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be TOML string")
    return value


def _decimal_text(value: object, field: str) -> Decimal:
    return Decimal(_text(value, field))


def _build_paper_runtime_config(payload: dict[str, Any]) -> PaperRuntimeConfig:
    universe_payload = cast(dict[str, Any], payload.pop("universe"))
    raw_members = universe_payload.pop("members")
    members = tuple(
        UniverseMember(
            symbol=_text(item["symbol"], "universe.members.symbol"),
            market=Market(_text(item["market"], "universe.members.market")),
            display_name=_text(item["display_name"], "universe.members.display_name"),
            currency=Currency(_text(item["currency"], "universe.members.currency")),
            sector=_text(item["sector"], "universe.members.sector"),
            inclusion_reason=_text(item["inclusion_reason"], "universe.members.inclusion_reason"),
            effective_date=item["effective_date"],
            source=_text(item["source"], "universe.members.source"),
        )
        for item in cast(list[dict[str, Any]], raw_members)
    )
    universe = UniverseSnapshot(
        universe_id=_text(universe_payload.pop("universe_id"), "universe.universe_id"),
        digest=_text(universe_payload.pop("digest"), "universe.digest"),
        members=members,
        **universe_payload,
    )
    accounts = tuple(
        MarketAccountProfile(
            account_id=_text(item["account_id"], "accounts.account_id"),
            market=Market(_text(item["market"], "accounts.market")),
            currency=Currency(_text(item["currency"], "accounts.currency")),
            initial_cash=_decimal_text(item["initial_cash"], "accounts.initial_cash"),
        )
        for item in cast(list[dict[str, Any]], payload.pop("accounts"))
    )
    market_data = tuple(
        MarketDataProviderProfile(
            provider_id=_text(item["provider_id"], "market_data.provider_id"),
            market=Market(_text(item["market"], "market_data.market")),
            keychain_service=(
                _text(item["keychain_service"], "market_data.keychain_service")
                if "keychain_service" in item
                else None
            ),
            keychain_account=(
                _text(item["keychain_account"], "market_data.keychain_account")
                if "keychain_account" in item
                else None
            ),
        )
        for item in cast(list[dict[str, Any]], payload.pop("market_data"))
    )
    raw_model = cast(dict[str, Any], payload.pop("model"))
    model = ModelRuntimeProfile.model_validate(raw_model, strict=True)
    raw_strategy = cast(dict[str, Any], payload.pop("strategy"))
    for field in (
        "volume_confirmation_threshold",
        "offensive_target_weight",
        "neutral_target_weight",
    ):
        raw_strategy[field] = _decimal_text(raw_strategy[field], f"strategy.{field}")
    strategy = StrategyRuntimeProfile.model_validate(raw_strategy, strict=True)
    schedules = tuple(
        ScheduleReference(
            market=Market(_text(item["market"], "schedules.market")),
            version=_text(item["version"], "schedules.version"),
            authority_status=_text(item["authority_status"], "schedules.authority_status"),
        )
        for item in cast(list[dict[str, Any]], payload.pop("schedules"))
    )
    return PaperRuntimeConfig(
        **payload,
        universe=universe,
        accounts=accounts,
        market_data=market_data,
        model=model,
        strategy=strategy,
        schedules=schedules,
    )


def load_paper_runtime_config(path: Path) -> PaperRuntimeConfig:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise RuntimeConfigLoadError("file_error") from None
    try:
        return _build_paper_runtime_config(tomllib.loads(raw))
    except (KeyError, TypeError, ValueError, DecimalException, tomllib.TOMLDecodeError):
        raise RuntimeConfigLoadError("invalid_config") from None
