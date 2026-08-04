from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from typing import Any

import pytest
from pydantic import ConfigDict, ValidationError

from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    AppendedRevisionIdentity,
    BoundedSessionSchedule,
    DailyBarRequest,
    FetchedDailyBar,
    IncrementalBarIngestor,
    IngestionReport,
    MarketDataError,
    MarketDataErrorCode,
    SessionScheduleRow,
)
from stock_agent.domain import Market

SESSION_DATES = (date(2026, 7, 23), date(2026, 7, 24))


def _instant(session_date: date, hour: int) -> datetime:
    return datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        hour,
        tzinfo=UTC,
    )


def _request(**overrides: object) -> DailyBarRequest:
    values = {
        "market": Market.US,
        "symbol": "AAPL",
        "start": SESSION_DATES[0],
        "end": SESSION_DATES[-1],
        "price_mode": "RAW",
    }
    values.update(overrides)
    return DailyBarRequest(**values)


def _fetched(
    session_date: date = SESSION_DATES[0], **overrides: object
) -> FetchedDailyBar:
    values = {
        "market": Market.US,
        "symbol": "AAPL",
        "session_date": session_date,
        "open": Decimal("100.00"),
        "high": Decimal("101.00"),
        "low": Decimal("99.00"),
        "close": Decimal("100.50"),
        "volume": Decimal("1234.000"),
        "provider_id": "alpha-vantage",
        "provider_native_symbol": "AAPL",
        "provider_record_id": f"native-{session_date.isoformat()}",
    }
    values.update(overrides)
    return FetchedDailyBar(**values)


def _schedule(
    dates: tuple[date, ...] = SESSION_DATES,
) -> BoundedSessionSchedule:
    return BoundedSessionSchedule(
        market=Market.US,
        start=dates[0],
        end=dates[-1],
        sessions=tuple(
            SessionScheduleRow(
                session_date=session_date,
                open_at=_instant(session_date, 14),
                close_at=_instant(session_date, 21),
                timezone="America/New_York",
                provenance="audited-test-calendar",
                generated_on=date(2026, 7, 29),
            )
            for session_date in dates
        ),
    )


class FakeProvider:
    def __init__(
        self,
        bars: object,
        *,
        provider_id: str = "alpha-vantage",
        provider_id_after: str | None = None,
    ) -> None:
        self._bars = bars
        self._provider_id = provider_id
        self._provider_id_after = provider_id_after
        self._reads = 0

    @property
    def provider_id(self) -> str:
        self._reads += 1
        if self._reads > 1 and self._provider_id_after is not None:
            return self._provider_id_after
        return self._provider_id

    def fetch_daily_bars(self, request: DailyBarRequest) -> Any:
        return self._bars


class SequencedIdentityProvider:
    def __init__(self, bars: object, identities: tuple[str, ...]) -> None:
        self._bars = bars
        self._identities = iter(identities)
        self.identity_reads = 0
        self.fetch_calls = 0

    @property
    def provider_id(self) -> str:
        self.identity_reads += 1
        return next(self._identities)

    def fetch_daily_bars(self, request: DailyBarRequest) -> Any:
        self.fetch_calls += 1
        return self._bars


class TupleSubclass(tuple):
    pass


class ExplodingProvider:
    provider_id = "alpha-vantage"

    def fetch_daily_bars(self, request: DailyBarRequest) -> Any:
        raise RuntimeError("native payload must not survive")


def _ingest(
    store: PointInTimeStore,
    bars: object,
    *,
    ingested_at: datetime,
    provider_id: str = "alpha-vantage",
    provider_id_after: str | None = None,
    request: DailyBarRequest | None = None,
    schedule: BoundedSessionSchedule | None = None,
) -> IngestionReport:
    return IncrementalBarIngestor(
        provider=FakeProvider(
            bars,
            provider_id=provider_id,
            provider_id_after=provider_id_after,
        ),
        store=store,
    ).ingest(
        request=_request() if request is None else request,
        schedule=_schedule() if schedule is None else schedule,
        ingested_at=ingested_at,
    )


def test_public_models_are_exact_final_frozen_and_forbid_copy_pollution() -> None:
    request = _request()
    identity = AppendedRevisionIdentity(
        source="alpha-vantage",
        source_record_id="market-data-source-record-sha256:" + "a" * 64,
    )
    report = IngestionReport(
        requested=2,
        received=1,
        appended=1,
        unchanged=0,
        appended_revision_identities=(identity,),
    )

    assert tuple(DailyBarRequest.model_fields) == (
        "market",
        "symbol",
        "start",
        "end",
        "price_mode",
    )
    assert tuple(FetchedDailyBar.model_fields) == (
        "market",
        "symbol",
        "session_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "provider_id",
        "provider_native_symbol",
        "provider_record_id",
    )
    assert tuple(AppendedRevisionIdentity.model_fields) == (
        "source",
        "source_record_id",
    )
    assert tuple(IngestionReport.model_fields) == (
        "requested",
        "received",
        "appended",
        "unchanged",
        "appended_revision_identities",
    )
    for value in (request, _fetched(), identity, report):
        with pytest.raises(ValidationError):
            value.__setattr__(next(iter(type(value).model_fields)), None)
        with pytest.raises(ValidationError):
            type(value)(**(value.model_dump() | {"unknown": True}))
        for kwargs in ({"include": {}}, {"exclude": set()}, {"update": {}}):
            with pytest.raises(TypeError):
                value.copy(**kwargs)
        with pytest.raises(TypeError):
            value.model_copy(update={})
        with pytest.raises(TypeError):

            class Polluted(type(value)):  # type: ignore[misc, valid-type]
                model_config = ConfigDict(frozen=False)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"market": "US"}, "market"),
        ({"symbol": "aapl"}, "symbol"),
        ({"symbol": "BRK.B"}, "symbol"),
        ({"start": datetime(2026, 7, 23, tzinfo=UTC)}, "plain date"),
        ({"end": date(2026, 7, 22)}, "start"),
        ({"price_mode": "ADJUSTED"}, "price_mode"),
        ({"market": Market.CN, "symbol": "400001"}, "symbol"),
        ({"market": Market.CN, "symbol": "6000000"}, "symbol"),
    ],
)
def test_request_rejects_coercion_unsupported_symbols_and_ranges(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", "100"),
        ("high", 101),
        ("low", 99.0),
        ("close", True),
        ("volume", Decimal("NaN")),
        ("open", Decimal("0")),
        ("volume", Decimal("-1")),
        ("close", Decimal("100.0000000000001")),
        ("volume", Decimal("1E26")),
    ],
)
def test_fetched_bar_rejects_numeric_coercion_and_storage_boundary(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _fetched(**{field: value})


def test_fetched_bar_uses_private_decimal_context_and_canonical_payload_equality() -> None:
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        bar = _fetched(
            open=Decimal("100.0000000000000"),
            high=Decimal("101.0000000000000"),
            low=Decimal("99.0000000000000"),
            close=Decimal("100.5000000000000"),
            volume=Decimal("1234.0000000000000"),
        )
    assert bar.close == Decimal("100.5")


@pytest.mark.parametrize(
    "overrides",
    [
        {"low": Decimal("101.01")},
        {"high": Decimal("99.99")},
        {"market": "US"},
        {"session_date": datetime(2026, 7, 23, tzinfo=UTC)},
        {"provider_id": " "},
        {"provider_native_symbol": " "},
        {"provider_record_id": " "},
    ],
)
def test_fetched_bar_rejects_invalid_identity_and_ohlcv(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _fetched(**overrides)


def test_report_arithmetic_types_and_order_are_exact() -> None:
    one = AppendedRevisionIdentity(
        source="alpha-vantage",
        source_record_id="market-data-source-record-sha256:" + "a" * 64,
    )
    two = AppendedRevisionIdentity(
        source="alpha-vantage",
        source_record_id="market-data-source-record-sha256:" + "b" * 64,
    )
    for overrides in (
        {"requested": True},
        {"received": 0},
        {"received": 3},
        {"appended": 0},
        {"unchanged": 1},
        {"appended_revision_identities": (one, one)},
    ):
        values = {
            "requested": 2,
            "received": 2,
            "appended": 2,
            "unchanged": 0,
            "appended_revision_identities": (one, two),
        }
        values.update(overrides)
        with pytest.raises(ValidationError):
            IngestionReport(**values)


def test_error_codes_and_retryability_are_frozen_and_safe() -> None:
    retryable = {
        MarketDataErrorCode.DNS,
        MarketDataErrorCode.CONNECT_TIMEOUT,
        MarketDataErrorCode.READ_TIMEOUT,
        MarketDataErrorCode.CONNECTION_RESET,
        MarketDataErrorCode.HTTP_5XX,
        MarketDataErrorCode.THROTTLED,
    }
    assert {code for code in MarketDataErrorCode if code.retryable} == retryable
    assert MarketDataErrorCode("persistence_conflict").retryable is False
    error = MarketDataError(
        MarketDataErrorCode.SCHEDULE,
        metadata={"session_date": "2026-07-23"},
    )
    assert error.code is MarketDataErrorCode.SCHEDULE
    assert error.retryable is False
    assert error.metadata == {"session_date": "2026-07-23"}
    with pytest.raises(TypeError):
        error.code = MarketDataErrorCode.AUTH
    with pytest.raises(TypeError):
        error.metadata["body"] = "secret"  # type: ignore[index]


@pytest.mark.parametrize(
    ("bars", "provider_id_after"),
    [
        ([_fetched()], None),
        (TupleSubclass((_fetched(),)), None),
        ((), None),
        ((_fetched(),), "mutated-provider"),
        ((_fetched(provider_id="other"),), None),
        ((_fetched(symbol="MSFT"),), None),
        ((_fetched(session_date=date(2026, 7, 22)),), None),
        ((_fetched(SESSION_DATES[1]), _fetched(SESSION_DATES[0])), None),
        ((_fetched(), _fetched()), None),
    ],
)
def test_provider_postcondition_failures_happen_before_writes(
    bars: object, provider_id_after: str | None
) -> None:
    store = PointInTimeStore()
    with pytest.raises(MarketDataError):
        _ingest(
            store,
            bars,
            ingested_at=_instant(SESSION_DATES[-1], 22),
            provider_id_after=provider_id_after,
        )
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) is None


def test_provider_identity_change_before_fetch_is_rejected_before_fetch_or_write() -> None:
    store = PointInTimeStore()
    provider = SequencedIdentityProvider(
        (_fetched(),),
        ("alpha-vantage", "mutated-provider", "alpha-vantage"),
    )
    ingestor = IncrementalBarIngestor(provider=provider, store=store)

    with pytest.raises(MarketDataError, match="identity"):
        ingestor.ingest(
            request=_request(),
            schedule=_schedule(),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )

    assert provider.identity_reads == 2
    assert provider.fetch_calls == 0
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) is None
    assert store.bar_revision_sources(
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) == ()


def test_provider_identity_change_after_fetch_is_rejected_after_three_reads_without_write() -> None:
    store = PointInTimeStore()
    provider = SequencedIdentityProvider(
        (_fetched(),),
        ("alpha-vantage", "alpha-vantage", "mutated-provider"),
    )
    ingestor = IncrementalBarIngestor(provider=provider, store=store)

    with pytest.raises(MarketDataError, match="identity"):
        ingestor.ingest(
            request=_request(),
            schedule=_schedule(),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )

    assert provider.identity_reads == 3
    assert provider.fetch_calls == 1
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) is None
    assert store.bar_revision_sources(
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) == ()


def test_provider_constructed_pollution_fails_before_writes() -> None:
    values = _fetched().model_dump()
    values.pop("close")
    polluted = FetchedDailyBar.model_construct(**values)
    store = PointInTimeStore()
    with pytest.raises(MarketDataError):
        _ingest(
            store,
            (polluted,),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) is None

    polluted_extra = _fetched()
    polluted_extra.__dict__["unknown"] = "pollution"
    with pytest.raises(MarketDataError):
        _ingest(
            store,
            (polluted_extra,),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )


def test_provider_exception_is_reconstructed_without_original_exception_graph() -> None:
    ingestor = IncrementalBarIngestor(
        provider=ExplodingProvider(),
        store=PointInTimeStore(),
    )
    with pytest.raises(MarketDataError) as captured:
        ingestor.ingest(
            request=_request(),
            schedule=_schedule(),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )
    error = captured.value
    assert error.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert error.__cause__ is None
    assert error.__context__ is None


def test_baseline_repeat_correction_repeat_and_rollback_state_machine() -> None:
    store = PointInTimeStore()
    session_date = SESSION_DATES[1]
    base = {"provider_record_id": "native-1"}
    times = tuple(
        _instant(SESSION_DATES[-1], 22) + timedelta(minutes=index)
        for index in range(5)
    )
    reports = (
        _ingest(store, (_fetched(session_date, **base),), ingested_at=times[0]),
        _ingest(
            store,
            (_fetched(session_date, close=Decimal("100.5000"), **base),),
            ingested_at=times[1],
        ),
        _ingest(
            store,
            (_fetched(session_date, close=Decimal("100.75"), **base),),
            ingested_at=times[2],
        ),
        _ingest(
            store,
            (_fetched(session_date, close=Decimal("100.75"), **base),),
            ingested_at=times[3],
        ),
        _ingest(
            store,
            (
                _fetched(
                    session_date,
                    close=Decimal("100.5000000000000"),
                    **base,
                ),
            ),
            ingested_at=times[4],
        ),
    )

    assert tuple((item.appended, item.unchanged) for item in reports) == (
        (1, 0),
        (0, 1),
        (1, 0),
        (0, 1),
        (1, 0),
    )
    assert reports[0].appended_revision_identities[0].source_record_id == (
        "market-data-source-record-sha256:"
        "efdfb4c72b80457003997ec9960779b3160cf258dcf5ebd3e2f84628660f8d22"
    )
    baseline = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=session_date,
        as_of=_instant(session_date, 21),
    )
    correction = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=session_date,
        as_of=times[2],
    )
    rollback = store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=session_date,
    )
    assert baseline is not None and baseline.bar.close == Decimal("100.5")
    assert baseline.bar.available_at == _instant(session_date, 21)
    assert correction is not None and correction.bar.close == Decimal("100.75")
    assert correction.bar.available_at == times[2]
    assert rollback is not None and rollback.bar.close == Decimal("100.5")
    assert rollback.bar.available_at == times[4]
    assert rollback.source_record_id != baseline.source_record_id


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_changed_observation_requires_strict_per_stream_clock(
    delta: timedelta,
) -> None:
    store = PointInTimeStore()
    ingested_at = _instant(SESSION_DATES[-1], 22)
    _ingest(store, (_fetched(),), ingested_at=ingested_at)
    with pytest.raises(MarketDataError, match="clock"):
        _ingest(
            store,
            (_fetched(close=Decimal("100.75")),),
            ingested_at=ingested_at + delta,
        )
    observed = store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    )
    assert observed is not None and observed.bar.close == Decimal("100.5")


def test_second_provider_for_same_event_is_a_persistence_conflict() -> None:
    store = PointInTimeStore()
    _ingest(
        store,
        (_fetched(),),
        ingested_at=_instant(SESSION_DATES[-1], 22),
    )
    with pytest.raises(MarketDataError, match="persistence_conflict"):
        _ingest(
            store,
            (_fetched(provider_id="other-provider"),),
            provider_id="other-provider",
            ingested_at=_instant(SESSION_DATES[-1], 23),
        )


def test_complete_batch_is_session_sorted_in_report() -> None:
    store = PointInTimeStore()
    report = _ingest(
        store,
        (_fetched(SESSION_DATES[0]), _fetched(SESSION_DATES[1])),
        ingested_at=_instant(SESSION_DATES[-1], 22),
    )
    assert (report.requested, report.received, report.appended, report.unchanged) == (
        2,
        2,
        2,
        0,
    )
    selected = tuple(
        store.latest_observed_bar_revision(
            provider_id="alpha-vantage",
            market=Market.US,
            symbol="AAPL",
            session_date=session_date,
        )
        for session_date in SESSION_DATES
    )
    assert tuple(
        item.source_record_id for item in selected if item is not None
    ) == tuple(item.source_record_id for item in report.appended_revision_identities)


def test_schedule_gap_and_unclosed_session_abort_before_any_write() -> None:
    store = PointInTimeStore()
    with pytest.raises(MarketDataError, match="schedule"):
        _ingest(
            store,
            (_fetched(SESSION_DATES[0]), _fetched(SESSION_DATES[1])),
            schedule=_schedule((SESSION_DATES[0],)),
            ingested_at=_instant(SESSION_DATES[-1], 22),
        )
    with pytest.raises(MarketDataError, match="schedule"):
        _ingest(
            store,
            (_fetched(SESSION_DATES[0]), _fetched(SESSION_DATES[1])),
            ingested_at=_instant(SESSION_DATES[-1], 20),
        )
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=SESSION_DATES[0],
    ) is None
