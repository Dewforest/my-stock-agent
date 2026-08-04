from __future__ import annotations

from datetime import UTC, datetime
from decimal import localcontext
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from stock_agent.audit import canonical_datetime, canonical_decimal, tagged_sha256
from stock_agent.data import (
    BarRevisionWrite,
    BatchAppendResult,
    PointInTimeStore,
    SelectedBarRevision,
)
from stock_agent.data.policies import (
    CURRENT_VIEW_BASELINE_PIT_POLICY,
    RAW_UNADJUSTED_PRICE_POLICY,
)
from stock_agent.data.providers.errors import MarketDataError, MarketDataErrorCode
from stock_agent.data.providers.models import (
    AppendedRevisionIdentity,
    BoundedSessionSchedule,
    DailyBarRequest,
    FetchedDailyBar,
    IngestionReport,
)
from stock_agent.data.providers.protocol import HistoricalDailyBarProvider
from stock_agent.domain import Bar

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _model_values(value: BaseModel) -> dict[str, object]:
    expected = set(value.__class__.model_fields)
    if set(value.__dict__) != expected:
        raise ValueError("model has polluted or missing fields")
    values: dict[str, object] = {}
    for name in value.__class__.model_fields:
        try:
            values[name] = getattr(value, name)
        except AttributeError as error:
            raise ValueError(f"model is missing field {name!r}") from error
    return values


def _rebuild_exact(value: object, expected: type[_ModelT]) -> _ModelT:
    if type(value) is not expected:
        raise ValueError(f"value must be exactly {expected.__name__}")
    with localcontext() as context:
        context.prec = max(context.prec, 256)
        context.Emin = min(context.Emin, -999999)
        context.Emax = max(context.Emax, 999999)
        return expected.model_validate(_model_values(value), strict=True)


def _provider_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        raise MarketDataError(MarketDataErrorCode.IDENTITY)
    return value


def _read_provider_id(provider: HistoricalDailyBarProvider) -> str:
    read_error: MarketDataError | None = None
    value: object = None
    try:
        value = provider.provider_id
    except BaseException:
        read_error = MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
    if read_error is not None:
        raise read_error from None
    return _provider_id(value)


def _payload(bar: FetchedDailyBar) -> tuple[object, ...]:
    return (
        bar.market,
        bar.symbol,
        bar.session_date,
        canonical_decimal(bar.open),
        canonical_decimal(bar.high),
        canonical_decimal(bar.low),
        canonical_decimal(bar.close),
        canonical_decimal(bar.volume),
    )


def _source_record_id(
    *,
    bar: FetchedDailyBar,
    provider_id: str,
    revision_kind: str,
    available_at: datetime,
) -> str:
    fields: tuple[object, ...] = (
        "source-record/v1",
        provider_id,
        bar.market.value,
        bar.symbol,
        bar.session_date.isoformat(),
        canonical_decimal(bar.open),
        canonical_decimal(bar.high),
        canonical_decimal(bar.low),
        canonical_decimal(bar.close),
        canonical_decimal(bar.volume),
        bar.provider_native_symbol,
        bar.provider_record_id,
        revision_kind,
        canonical_datetime(available_at),
        CURRENT_VIEW_BASELINE_PIT_POLICY,
        RAW_UNADJUSTED_PRICE_POLICY,
    )
    return tagged_sha256("market-data-source-record", fields)


class IncrementalBarIngestor:
    def __init__(
        self,
        *,
        provider: HistoricalDailyBarProvider,
        store: PointInTimeStore,
    ) -> None:
        if type(store) is not PointInTimeStore:
            raise TypeError("store must be exactly PointInTimeStore")
        try:
            fetch = provider.fetch_daily_bars
        except AttributeError as error:
            raise TypeError("provider must implement HistoricalDailyBarProvider") from error
        if not callable(fetch):
            raise TypeError("provider must implement HistoricalDailyBarProvider")
        self._provider = provider
        self._provider_id = _read_provider_id(provider)
        self._store = store

    def ingest(
        self,
        *,
        request: DailyBarRequest,
        schedule: BoundedSessionSchedule,
        ingested_at: datetime,
    ) -> IngestionReport:
        admission_error: MarketDataError | None = None
        try:
            clean_request = _rebuild_exact(request, DailyBarRequest)
            clean_schedule = _rebuild_exact(schedule, BoundedSessionSchedule)
        except (TypeError, ValueError, ValidationError):
            admission_error = MarketDataError(MarketDataErrorCode.INVALID_REQUEST)
        if admission_error is not None:
            raise admission_error from None
        if type(ingested_at) is not datetime or ingested_at.utcoffset() is None:
            raise MarketDataError(MarketDataErrorCode.CLOCK)
        ingested_at = ingested_at.astimezone(UTC)
        if (
            clean_schedule.market is not clean_request.market
            or clean_schedule.start != clean_request.start
            or clean_schedule.end != clean_request.end
        ):
            raise MarketDataError(MarketDataErrorCode.SCHEDULE)
        if any(
            row.close_at.astimezone(UTC) > ingested_at
            for row in clean_schedule.sessions
        ):
            raise MarketDataError(MarketDataErrorCode.SCHEDULE)

        current_provider_id = _read_provider_id(self._provider)
        if current_provider_id != self._provider_id:
            raise MarketDataError(MarketDataErrorCode.IDENTITY)

        fetch_error: MarketDataError | None = None
        try:
            raw_bars = self._provider.fetch_daily_bars(clean_request)
        except MarketDataError as error:
            fetch_error = MarketDataError(error.code, metadata=error.metadata)
        except BaseException:
            fetch_error = MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
        if fetch_error is not None:
            raise fetch_error from None

        current_provider_id = _read_provider_id(self._provider)
        if current_provider_id != self._provider_id:
            raise MarketDataError(MarketDataErrorCode.IDENTITY)
        if type(raw_bars) is not tuple or not raw_bars:
            code = (
                MarketDataErrorCode.EMPTY
                if type(raw_bars) is tuple
                else MarketDataErrorCode.INTERNAL_CONTRACT
            )
            raise MarketDataError(code)

        bars: list[FetchedDailyBar] = []
        rebuild_error: MarketDataError | None = None
        try:
            for raw_bar in raw_bars:
                bars.append(_rebuild_exact(raw_bar, FetchedDailyBar))
        except (TypeError, ValueError, ValidationError):
            rebuild_error = MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
        if rebuild_error is not None:
            raise rebuild_error from None
        event_keys = tuple(
            (bar.market, bar.symbol, bar.session_date) for bar in bars
        )
        if len(event_keys) != len(set(event_keys)):
            raise MarketDataError(MarketDataErrorCode.DUPLICATE)
        dates = tuple(bar.session_date for bar in bars)
        if dates != tuple(sorted(dates)):
            raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
        schedule_by_date = {
            row.session_date: row for row in clean_schedule.sessions
        }
        for bar in bars:
            if bar.provider_id != self._provider_id:
                raise MarketDataError(MarketDataErrorCode.IDENTITY)
            if bar.market is not clean_request.market or bar.symbol != clean_request.symbol:
                raise MarketDataError(MarketDataErrorCode.IDENTITY)
            if not clean_request.start <= bar.session_date <= clean_request.end:
                raise MarketDataError(MarketDataErrorCode.OUT_OF_RANGE)
            if bar.session_date not in schedule_by_date:
                raise MarketDataError(MarketDataErrorCode.SCHEDULE)

        candidates: list[BarRevisionWrite] = []
        unchanged = 0
        for fetched in bars:
            other_sources = tuple(
                source
                for source in self._bar_revision_sources(
                    market=fetched.market,
                    symbol=fetched.symbol,
                    session_date=fetched.session_date,
                )
                if source != self._provider_id
            )
            if other_sources:
                raise MarketDataError(
                    MarketDataErrorCode.PERSISTENCE_CONFLICT,
                    metadata={"session_date": fetched.session_date.isoformat()},
                )
            latest = self._latest_observed_bar_revision(
                provider_id=self._provider_id,
                market=fetched.market,
                symbol=fetched.symbol,
                session_date=fetched.session_date,
            )
            if latest is not None:
                latest_payload = (
                    latest.bar.market,
                    latest.bar.symbol,
                    latest.bar.session_date,
                    canonical_decimal(latest.bar.open),
                    canonical_decimal(latest.bar.high),
                    canonical_decimal(latest.bar.low),
                    canonical_decimal(latest.bar.close),
                    canonical_decimal(latest.bar.volume),
                )
                if latest_payload == _payload(fetched):
                    unchanged += 1
                    continue
                if ingested_at <= latest.ingested_at.astimezone(UTC):
                    raise MarketDataError(MarketDataErrorCode.CLOCK)
                revision_kind = "CORRECTION"
                available_at = ingested_at
            else:
                revision_kind = "BASELINE"
                available_at = schedule_by_date[fetched.session_date].close_at.astimezone(
                    UTC
                )
            if ingested_at < available_at:
                raise MarketDataError(MarketDataErrorCode.CLOCK)
            numeric_error: MarketDataError | None = None
            candidate: BarRevisionWrite | None = None
            try:
                bar = Bar(
                    symbol=fetched.symbol,
                    market=fetched.market,
                    session_date=fetched.session_date,
                    open=fetched.open,
                    high=fetched.high,
                    low=fetched.low,
                    close=fetched.close,
                    volume=fetched.volume,
                    available_at=available_at,
                )
                candidate = BarRevisionWrite(
                    bar=bar,
                    ingested_at=ingested_at,
                    source=self._provider_id,
                    source_record_id=_source_record_id(
                        bar=fetched,
                        provider_id=self._provider_id,
                        revision_kind=revision_kind,
                        available_at=available_at,
                    ),
                )
            except (TypeError, ValueError, ValidationError):
                numeric_error = MarketDataError(MarketDataErrorCode.NUMERIC)
            if numeric_error is not None:
                raise numeric_error from None
            assert candidate is not None
            candidates.append(candidate)

        appended_identities: tuple[AppendedRevisionIdentity, ...] = ()
        if candidates:
            batch_result = self._append_bar_revisions(tuple(candidates))
            if batch_result.unchanged:
                raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
            appended_identities = tuple(
                AppendedRevisionIdentity(
                    source=source,
                    source_record_id=source_record_id,
                )
                for source, source_record_id in batch_result.appended
            )

        return IngestionReport(
            requested=len(clean_schedule.sessions),
            received=len(bars),
            appended=len(appended_identities),
            unchanged=unchanged,
            appended_revision_identities=appended_identities,
        )

    def _bar_revision_sources(
        self, **query: object
    ) -> tuple[str, ...]:
        persistence_error: MarketDataError | None = None
        result: tuple[str, ...] = ()
        try:
            result = self._store.bar_revision_sources(**query)  # type: ignore[arg-type]
        except BaseException:
            persistence_error = MarketDataError(MarketDataErrorCode.PERSISTENCE)
        if persistence_error is not None:
            raise persistence_error from None
        return result

    def _latest_observed_bar_revision(
        self, **query: object
    ) -> SelectedBarRevision | None:
        persistence_error: MarketDataError | None = None
        result = None
        try:
            result = self._store.latest_observed_bar_revision(**query)  # type: ignore[arg-type]
        except BaseException:
            persistence_error = MarketDataError(MarketDataErrorCode.PERSISTENCE)
        if persistence_error is not None:
            raise persistence_error from None
        return result

    def _append_bar_revisions(
        self, candidates: tuple[BarRevisionWrite, ...]
    ) -> BatchAppendResult:
        persistence_error: MarketDataError | None = None
        result = None
        try:
            result = self._store.append_bar_revisions(candidates)
        except BaseException as error:
            code = (
                MarketDataErrorCode.PERSISTENCE_CONFLICT
                if type(error) is ValueError and "conflict" in str(error)
                else MarketDataErrorCode.PERSISTENCE
            )
            persistence_error = MarketDataError(code)
        if persistence_error is not None:
            raise persistence_error from None
        assert result is not None
        return result
