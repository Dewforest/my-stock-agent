from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date
from decimal import Decimal, localcontext
from typing import Annotated, Any, Literal, Self, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_SourceRecordId = Annotated[
    str,
    StringConstraints(
        pattern=r"^market-data-source-record-sha256:[0-9a-f]{64}$",
    ),
]


def _plain_date(value: object) -> object:
    if type(value) is not date:
        raise ValueError("value must be a plain date")
    return value


def _exact_market(value: object) -> object:
    if type(value) is not Market:
        raise ValueError("market must be exactly Market")
    return value


def _supported_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be exactly Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    value_tuple = value.as_tuple()
    exponent = value_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("value must be finite")
    trailing_zeroes = 0
    for digit in reversed(value_tuple.digits):
        if digit:
            break
        trailing_zeroes += 1
    effective_scale = (
        0
        if trailing_zeroes == len(value_tuple.digits)
        else max(0, -exponent - trailing_zeroes)
    )
    if effective_scale > 12:
        raise ValueError("value effective scale must be at most 12")
    if value.copy_abs() >= Decimal("1E26"):
        raise ValueError("value magnitude must be less than 1E26")
    return value


SupportedDecimal = Annotated[Decimal, BeforeValidator(_supported_decimal)]
PositiveSupportedDecimal = Annotated[
    Decimal,
    BeforeValidator(_supported_decimal),
    Field(gt=0),
]
NonNegativeSupportedDecimal = Annotated[
    Decimal,
    BeforeValidator(_supported_decimal),
    Field(ge=0),
]
StrictCount = Annotated[int, Field(strict=True, ge=0)]


def _model_values(value: BaseModel) -> dict[str, object]:
    expected = set(value.__class__.model_fields)
    if set(value.__dict__) != expected:
        raise ValueError("nested model has polluted or missing fields")
    values: dict[str, object] = {}
    for name in value.__class__.model_fields:
        try:
            values[name] = getattr(value, name)
        except AttributeError as error:
            raise ValueError(f"nested model is missing field {name!r}") from error
    return values


def _rebuild_exact(value: object, expected: type[_ModelT], name: str) -> _ModelT:
    if type(value) is not expected:
        raise ValueError(f"{name} must be exactly {expected.__name__}")
    return expected(**_model_values(value))


class _ExactFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @model_validator(mode="wrap")
    @classmethod
    def decimal_context_is_private(cls, value: Any, handler: Any) -> Self:
        with localcontext() as context:
            context.prec = max(context.prec, 256)
            context.Emin = min(context.Emin, -999999)
            context.Emax = max(context.Emax, 999999)
            return handler(value)

    def __init_subclass__(cls, **kwargs: object) -> None:
        if cls.__bases__ != (_ExactFrozenModel,):
            raise TypeError(f"{cls.__name__} does not support subclasses")
        super().__init_subclass__(**kwargs)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError("exact frozen models do not support copy projections or updates")
        return super().copy(deep=deep)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise TypeError("exact frozen models do not support copy updates")
        return super().model_copy(deep=deep)


class DailyBarRequest(_ExactFrozenModel):
    market: Market
    symbol: str
    start: date
    end: date
    price_mode: Literal["RAW"] = "RAW"

    _market_is_exact = field_validator("market", mode="before")(_exact_market)
    _start_is_plain = field_validator("start", mode="before")(_plain_date)
    _end_is_plain = field_validator("end", mode="before")(_plain_date)

    @model_validator(mode="after")
    def request_is_supported(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        if self.market is Market.CN:
            valid = (
                len(self.symbol) == 6
                and self.symbol.isascii()
                and self.symbol.isdigit()
                and self.symbol[0] in "603"
                and (
                    self.symbol.startswith("6")
                    or self.symbol.startswith("0")
                    or self.symbol.startswith("3")
                )
            )
        else:
            valid = (
                1 <= len(self.symbol) <= 10
                and self.symbol.isascii()
                and self.symbol[0].isalpha()
                and self.symbol[0].isupper()
                and all(character.isupper() or character.isdigit() for character in self.symbol)
            )
        if not valid:
            raise ValueError("symbol is unsupported for the requested market")
        return self


class FetchedDailyBar(_ExactFrozenModel):
    market: Market
    symbol: str
    session_date: date
    open: PositiveSupportedDecimal
    high: PositiveSupportedDecimal
    low: PositiveSupportedDecimal
    close: PositiveSupportedDecimal
    volume: NonNegativeSupportedDecimal
    provider_id: _NonBlankText
    provider_native_symbol: _NonBlankText
    provider_record_id: _NonBlankText

    _market_is_exact = field_validator("market", mode="before")(_exact_market)
    _session_date_is_plain = field_validator("session_date", mode="before")(_plain_date)

    @model_validator(mode="after")
    def bar_is_consistent(self) -> Self:
        request = DailyBarRequest(
            market=self.market,
            symbol=self.symbol,
            start=self.session_date,
            end=self.session_date,
        )
        if request.symbol != self.symbol:
            raise ValueError("symbol must be canonical")
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be between low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be between low and high")
        return self


class AppendedRevisionIdentity(_ExactFrozenModel):
    source: _NonBlankText
    source_record_id: _SourceRecordId


class IngestionReport(_ExactFrozenModel):
    requested: StrictCount
    received: StrictCount
    appended: StrictCount
    unchanged: StrictCount
    appended_revision_identities: tuple[AppendedRevisionIdentity, ...]

    @field_validator("appended_revision_identities", mode="before")
    @classmethod
    def identities_are_exact(
        cls, value: object
    ) -> tuple[AppendedRevisionIdentity, ...]:
        if type(value) is not tuple:
            raise ValueError("appended_revision_identities must be an exact tuple")
        return tuple(
            _rebuild_exact(item, AppendedRevisionIdentity, "appended identity")
            for item in value
        )

    @model_validator(mode="after")
    def arithmetic_is_consistent(self) -> Self:
        if self.requested <= 0:
            raise ValueError("requested must be positive")
        if not 0 < self.received <= self.requested:
            raise ValueError("received must be positive and not exceed requested")
        if self.appended + self.unchanged != self.received:
            raise ValueError("appended plus unchanged must equal received")
        if len(self.appended_revision_identities) != self.appended:
            raise ValueError("appended identity count must equal appended")
        identities = tuple(
            (item.source, item.source_record_id)
            for item in self.appended_revision_identities
        )
        if len(identities) != len(set(identities)):
            raise ValueError("appended identities must be unique")
        return self


class SessionScheduleRow(_ExactFrozenModel):
    session_date: date
    open_at: AwareDatetime
    close_at: AwareDatetime
    timezone: _NonBlankText
    provenance: _NonBlankText
    generated_on: date

    _session_date_is_plain = field_validator("session_date", mode="before")(_plain_date)
    _generated_on_is_plain = field_validator("generated_on", mode="before")(_plain_date)

    @model_validator(mode="after")
    def clocks_are_authoritative(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA zone name") from error
        if self.open_at.astimezone(UTC) >= self.close_at.astimezone(UTC):
            raise ValueError("open_at must be before close_at")
        if self.open_at.astimezone(zone).date() != self.session_date:
            raise ValueError("open_at local date must equal session_date")
        if self.close_at.astimezone(zone).date() != self.session_date:
            raise ValueError("close_at local date must equal session_date")
        return self


class BoundedSessionSchedule(_ExactFrozenModel):
    market: Market
    start: date
    end: date
    sessions: tuple[SessionScheduleRow, ...]

    _market_is_exact = field_validator("market", mode="before")(_exact_market)
    _start_is_plain = field_validator("start", mode="before")(_plain_date)
    _end_is_plain = field_validator("end", mode="before")(_plain_date)

    @field_validator("sessions", mode="before")
    @classmethod
    def sessions_are_exact(
        cls, value: object
    ) -> tuple[SessionScheduleRow, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("sessions must be a nonempty exact tuple")
        return tuple(
            _rebuild_exact(item, SessionScheduleRow, "schedule row")
            for item in value
        )

    @model_validator(mode="after")
    def bounds_are_exact(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        dates = tuple(item.session_date for item in self.sessions)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise ValueError("schedule dates must be sorted and unique")
        if dates[0] != self.start or dates[-1] != self.end:
            raise ValueError("schedule dates must exactly span the bounded slice")
        if any(
            previous.close_at.astimezone(UTC) >= following.open_at.astimezone(UTC)
            for previous, following in zip(
                self.sessions, self.sessions[1:], strict=False
            )
        ):
            raise ValueError("previous close must be before next open")
        return self
