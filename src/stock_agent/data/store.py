from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException, localcontext
from typing import Annotated, Any, Self

import duckdb
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_agent.domain import Bar, Market

_DECIMAL_SCALE = Decimal("1E-12")
_DECIMAL_INTEGER_LIMIT = Decimal("1E+26")
_NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_decimal_38_12(value: Decimal, name: str) -> Decimal:
    message = f"{name} must be exactly representable as DECIMAL(38, 12)"
    try:
        with localcontext() as context:
            context.prec = max(50, len(value.as_tuple().digits) + 12)
            if abs(value) >= _DECIMAL_INTEGER_LIMIT:
                raise ValueError(message)
            quantized = value.quantize(_DECIMAL_SCALE)
            if quantized != value:
                raise ValueError(message)
            return quantized
    except DecimalException as error:
        raise ValueError(message) from error


class SelectedBarRevision(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    bar: Bar
    ingested_at: AwareDatetime
    source: _NonBlankText
    source_record_id: _NonBlankText

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("SelectedBarRevision does not support subclasses")

    @field_validator("bar", mode="before")
    @classmethod
    def bar_has_exact_type(cls, value: object) -> object:
        if type(value) is not Bar:
            raise ValueError("bar must be a Bar")
        return value

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError(
                "immutable selected revisions do not support copy projections or updates"
            )
        return super().copy(deep=deep)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise TypeError(
                "immutable selected revisions do not support copy projections or updates"
            )
        return super().model_copy(deep=deep)


class BarRevisionWrite(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    bar: Bar
    ingested_at: AwareDatetime
    source: _NonBlankText
    source_record_id: _NonBlankText

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("BarRevisionWrite does not support subclasses")

    @field_validator("bar", mode="before")
    @classmethod
    def bar_has_exact_type(cls, value: object) -> Bar:
        if type(value) is not Bar:
            raise ValueError("bar must be exactly Bar")
        if set(value.__dict__) != set(Bar.model_fields):
            raise ValueError("bar has polluted or missing fields")
        values: dict[str, object] = {}
        for name in Bar.model_fields:
            try:
                values[name] = getattr(value, name)
            except AttributeError as error:
                raise ValueError(f"bar is missing field {name!r}") from error
        return Bar.model_validate(values, strict=True)

    @field_validator("ingested_at", mode="before")
    @classmethod
    def ingested_at_is_aware(cls, value: object) -> object:
        if type(value) is not datetime:
            raise ValueError("ingested_at must be exactly datetime")
        _require_aware(value, "ingested_at")
        return value

    @model_validator(mode="after")
    def clocks_are_consistent(self) -> Self:
        if self.ingested_at.astimezone(UTC) < self.bar.available_at.astimezone(UTC):
            raise ValueError("ingested_at cannot be before available_at")
        return self

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError("bar revision writes do not support copy projections or updates")
        return super().copy(deep=deep)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise TypeError("bar revision writes do not support copy updates")
        return super().model_copy(deep=deep)


class BatchAppendResult(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    appended: tuple[tuple[str, str], ...]
    unchanged: tuple[tuple[str, str], ...]

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("BatchAppendResult does not support subclasses")

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError("batch append results do not support copy projections or updates")
        return super().copy(deep=deep)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise TypeError("batch append results do not support copy updates")
        return super().model_copy(deep=deep)

    @field_validator("appended", "unchanged", mode="before")
    @classmethod
    def identities_are_exact(
        cls, value: object
    ) -> tuple[tuple[str, str], ...]:
        if type(value) is not tuple:
            raise ValueError("batch identities must be exact tuples")
        cleaned: list[tuple[str, str]] = []
        for identity in value:
            if (
                type(identity) is not tuple
                or len(identity) != 2
                or any(type(item) is not str or not item.strip() for item in identity)
            ):
                raise ValueError("batch identity must contain two nonblank strings")
            cleaned.append((identity[0].strip(), identity[1].strip()))
        return tuple(cleaned)

    @model_validator(mode="after")
    def identities_are_unique(self) -> Self:
        combined = self.appended + self.unchanged
        if len(combined) != len(set(combined)):
            raise ValueError("batch identities must be unique")
        return self


class PointInTimeStore:
    def __init__(self, database: str = ":memory:") -> None:
        self._connection = duckdb.connect(database)
        self._closed = False
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bars (
                market VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                session_date DATE NOT NULL,
                open DECIMAL(38, 12) NOT NULL,
                high DECIMAL(38, 12) NOT NULL,
                low DECIMAL(38, 12) NOT NULL,
                close DECIMAL(38, 12) NOT NULL,
                volume DECIMAL(38, 12) NOT NULL,
                available_at TIMESTAMP NOT NULL,
                ingested_at TIMESTAMP NOT NULL,
                source VARCHAR NOT NULL,
                source_record_id VARCHAR NOT NULL,
                UNIQUE(source, source_record_id)
            )
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("PointInTimeStore is closed")

    def __enter__(self) -> "PointInTimeStore":
        self._ensure_open()
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def append_bar(
        self,
        bar: Bar,
        *,
        ingested_at: datetime,
        source: str,
        source_record_id: str,
    ) -> None:
        self._ensure_open()
        if type(source) is not str or not source.strip():
            raise ValueError("source must not be blank")
        if type(source_record_id) is not str or not source_record_id.strip():
            raise ValueError("source_record_id must not be blank")
        self.append_bar_revisions(
            (
                BarRevisionWrite(
                    bar=bar,
                    ingested_at=ingested_at,
                    source=source,
                    source_record_id=source_record_id,
                ),
            ),
            enforce_single_provider=False,
        )

    @staticmethod
    def _write_payload(write: BarRevisionWrite) -> tuple[object, ...]:
        bar = write.bar
        decimals = [
            _require_decimal_38_12(value, name)
            for name, value in (
                ("open", bar.open),
                ("high", bar.high),
                ("low", bar.low),
                ("close", bar.close),
                ("volume", bar.volume),
            )
        ]
        return (
            str(bar.market),
            bar.symbol.strip().upper(),
            bar.session_date,
            *decimals,
            bar.available_at.astimezone(UTC).replace(tzinfo=None),
            write.ingested_at.astimezone(UTC).replace(tzinfo=None),
            write.source,
            write.source_record_id,
        )

    def append_bar_revisions(
        self,
        revisions: tuple[BarRevisionWrite, ...],
        *,
        enforce_single_provider: bool = True,
        enforce_strict_stream_clock: bool = False,
    ) -> BatchAppendResult:
        self._ensure_open()
        if type(enforce_single_provider) is not bool:
            raise TypeError("enforce_single_provider must be exactly bool")
        if type(enforce_strict_stream_clock) is not bool:
            raise TypeError("enforce_strict_stream_clock must be exactly bool")
        if type(revisions) is not tuple or not revisions:
            raise ValueError("revisions must be a nonempty exact tuple")
        rebuilt: list[BarRevisionWrite] = []
        for revision in revisions:
            if type(revision) is not BarRevisionWrite:
                raise ValueError("revisions must contain exact BarRevisionWrite values")
            if set(revision.__dict__) != set(BarRevisionWrite.model_fields):
                raise ValueError("revision has polluted or missing fields")
            values: dict[str, object] = {}
            for name in BarRevisionWrite.model_fields:
                try:
                    values[name] = getattr(revision, name)
                except AttributeError as error:
                    raise ValueError(
                        f"revision is missing field {name!r}"
                    ) from error
            rebuilt.append(BarRevisionWrite.model_validate(values, strict=True))
        identities = tuple(
            (item.source, item.source_record_id) for item in rebuilt
        )
        provider_events = tuple(
            (
                item.source,
                item.bar.market,
                item.bar.symbol,
                item.bar.session_date,
            )
            for item in rebuilt
        )
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate row identity inside batch")
        if len(provider_events) != len(set(provider_events)):
            raise ValueError("duplicate provider/event identity inside batch")
        event_sources: dict[tuple[Market, str, date], set[str]] = {}
        for write in rebuilt:
            event = (
                write.bar.market,
                write.bar.symbol,
                write.bar.session_date,
            )
            event_sources.setdefault(event, set()).add(write.source)
        if enforce_single_provider and any(
            len(sources) != 1 for sources in event_sources.values()
        ):
            raise ValueError("conflicting providers for event inside batch")
        payloads = tuple(self._write_payload(item) for item in rebuilt)

        appended: list[tuple[str, str]] = []
        unchanged: list[tuple[str, str]] = []
        self._connection.execute("BEGIN TRANSACTION")
        try:
            for write, payload in zip(rebuilt, payloads, strict=True):
                if enforce_single_provider:
                    other_source = self._connection.execute(
                        """
                        SELECT source
                        FROM bars
                        WHERE market = ? AND symbol = ? AND session_date = ?
                              AND source != ?
                        ORDER BY source
                        LIMIT 1
                        """,
                        [
                            str(write.bar.market),
                            write.bar.symbol,
                            write.bar.session_date,
                            write.source,
                        ],
                    ).fetchone()
                    if other_source is not None:
                        raise ValueError(
                            "conflicting provider for event "
                            f"({write.bar.market.value!r}, {write.bar.symbol!r}, "
                            f"{write.bar.session_date.isoformat()!r})"
                        )
                existing = self._connection.execute(
                    """
                    SELECT market, symbol, session_date, open, high, low, close,
                           volume, available_at, ingested_at, source,
                           source_record_id
                    FROM bars
                    WHERE source = ? AND source_record_id = ?
                    """,
                    [write.source, write.source_record_id],
                ).fetchone()
                identity = (write.source, write.source_record_id)
                if existing is not None:
                    if existing == payload:
                        unchanged.append(identity)
                        continue
                    raise ValueError(
                        "conflicting payload for revision identity "
                        f"({write.source!r}, {write.source_record_id!r})"
                    )
                if enforce_strict_stream_clock:
                    latest_ingested_at = self._connection.execute(
                        """
                        SELECT ingested_at
                        FROM bars
                        WHERE source = ? AND market = ? AND symbol = ?
                              AND session_date = ?
                        ORDER BY ingested_at DESC, source_record_id DESC
                        LIMIT 1
                        """,
                        [
                            write.source,
                            str(write.bar.market),
                            write.bar.symbol,
                            write.bar.session_date,
                        ],
                    ).fetchone()
                    if (
                        latest_ingested_at is not None
                        and payload[9] <= latest_ingested_at[0]
                    ):
                        raise ValueError("ingestion clock conflict")
                self._connection.execute(
                    """
                    INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                appended.append(identity)
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return BatchAppendResult(
            appended=tuple(appended),
            unchanged=tuple(unchanged),
        )

    def latest_bar_as_of(
        self,
        *,
        market: Market,
        symbol: str,
        session_date: date,
        as_of: datetime,
    ) -> Bar | None:
        revision = self.latest_bar_revision_as_of(
            market=market,
            symbol=symbol,
            session_date=session_date,
            as_of=as_of,
        )
        return None if revision is None else revision.bar

    def latest_bar_revision_as_of(
        self,
        *,
        market: Market,
        symbol: str,
        session_date: date,
        as_of: datetime,
    ) -> SelectedBarRevision | None:
        self._ensure_open()
        _require_aware(as_of, "as_of")
        row = self._connection.execute(
            """
            SELECT symbol, market, session_date, open, high, low, close, volume,
                   available_at, ingested_at, source, source_record_id
            FROM bars
            WHERE market = ? AND symbol = ? AND session_date = ?
                AND available_at <= ?
            ORDER BY available_at DESC, ingested_at DESC, source DESC, source_record_id DESC
            LIMIT 1
            """,
            [
                str(market),
                symbol.strip().upper(),
                session_date,
                as_of.astimezone(UTC).replace(tzinfo=None),
            ],
        ).fetchone()
        return None if row is None else self._selected_revision_from_row(row)

    def latest_observed_bar_revision(
        self,
        *,
        provider_id: str,
        market: Market,
        symbol: str,
        session_date: date,
    ) -> SelectedBarRevision | None:
        self._ensure_open()
        if type(provider_id) is not str or not provider_id.strip():
            raise ValueError("provider_id must be a nonblank string")
        row = self._connection.execute(
            """
            SELECT symbol, market, session_date, open, high, low, close, volume,
                   available_at, ingested_at, source, source_record_id
            FROM bars
            WHERE source = ? AND market = ? AND symbol = ? AND session_date = ?
            ORDER BY ingested_at DESC, source_record_id DESC
            LIMIT 1
            """,
            [
                provider_id.strip(),
                str(market),
                symbol.strip().upper(),
                session_date,
            ],
        ).fetchone()
        return None if row is None else self._selected_revision_from_row(row)

    def bar_revision_sources(
        self,
        *,
        market: Market,
        symbol: str,
        session_date: date,
    ) -> tuple[str, ...]:
        self._ensure_open()
        rows = self._connection.execute(
            """
            SELECT DISTINCT source
            FROM bars
            WHERE market = ? AND symbol = ? AND session_date = ?
            ORDER BY source
            """,
            [str(market), symbol.strip().upper(), session_date],
        ).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def _selected_revision_from_row(
        row: tuple[object, ...],
    ) -> SelectedBarRevision:
        available_at = row[8]
        ingested_at = row[9]
        if type(available_at) is not datetime or type(ingested_at) is not datetime:
            raise RuntimeError("stored revision timestamps have invalid types")
        return SelectedBarRevision(
            bar=Bar(
                symbol=row[0],
                market=Market(row[1]),
                session_date=row[2],
                open=row[3],
                high=row[4],
                low=row[5],
                close=row[6],
                volume=row[7],
                available_at=available_at.replace(tzinfo=UTC),
            ),
            ingested_at=ingested_at.replace(tzinfo=UTC),
            source=row[10],
            source_record_id=row[11],
        )
