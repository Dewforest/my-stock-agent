from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_agent.backtest import (
    ChronologicalBacktestRunner,
    build_real_data_backtest_spec,
)
from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    BoundedSessionSchedule,
    DailyBarRequest,
    IncrementalBarIngestor,
    MarketDataError,
    MarketDataErrorCode,
    SessionScheduleRow,
)
from stock_agent.data.providers.alpha_vantage import AlphaVantageDailyBarProvider
from stock_agent.domain import Currency, Instrument, Market
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "alpha_vantage_daily.json"
METADATA = FIXTURES / "alpha_vantage_daily.metadata.json"
DATES = tuple(date(2025, 7, day) for day in (1, 2, 3, 7, 8))
HOST = "www.alphavantage.co"
PATH = "/query"
TEST_KEY = "".join(("CAP3_", "TEST_KEY_", "4b8d2f"))
EXPECTED_PROVIDER_RECORD_IDS = (
    "provider-payload-sha256:d6fdc5db3df4bea64809f07d955fcceaa150aef52a70f44a06c757d8acb62bf8",
    "provider-payload-sha256:a57a590df1a79e763ac43f675b7cbac5731a53f271574a33830f79cf65eae561",
    "provider-payload-sha256:93603ad0569c194e4ef2c7b1f7eabeebfe0954661c593ee0d993757b7eee9d97",
    "provider-payload-sha256:cac775f144cb9eac3db75b6690cbad1c4ffe0e36aee24ca5968e13489487eb41",
    "provider-payload-sha256:0f5bf363885101f3a787c93d97058923453a9c0c3089d2451e27783a5f350930",
)
EXPECTED_SOURCE_RECORD_IDS = (
    "market-data-source-record-sha256:1b87daff34fd34a7f4d2ffba82a651d7067ab991cda34f50bcad00315c970bc0",
    "market-data-source-record-sha256:c8643fefa9b93316ae2d44414561da82ab547cacddf7d2c9e91c46b508703adf",
    "market-data-source-record-sha256:015bb04ab7551fe8199c90a1e90c18527821ad96a11594d8c8a2e8580edec68a",
    "market-data-source-record-sha256:f0973f635d0a128bd51124e4dbe5a394799a893c418a8306245cf2a503424dea",
    "market-data-source-record-sha256:e495514fec4121065728a1e1ce4af6eae2c2ab5407879fa27553695326b6f21b",
)


@pytest.fixture(scope="module", autouse=True)
def _block_real_sockets() -> Any:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Capability3 tests must never open sockets")

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket, "create_connection", forbidden)
    patcher.setattr(socket, "create_server", forbidden)
    patcher.setattr(socket, "fromfd", forbidden)
    patcher.setattr(socket, "socketpair", forbidden)
    patcher.setattr(socket, "socket", forbidden)
    yield
    patcher.undo()


class FakeTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, str]] = []

    def get(self, *, host: str, target: str) -> bytes:
        self.calls.append((host, target))
        return self.body


class StaticProvider:
    provider_id = "alpha-vantage"

    def __init__(self, bars: tuple[object, ...]) -> None:
        self._bars = bars

    def fetch_daily_bars(self, request: DailyBarRequest) -> tuple[object, ...]:
        return self._bars


@dataclass(frozen=True)
class EmptyUsStrategy:
    strategy_id: str = "us-empty"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[()]:
        return ()


def _request(
    *,
    symbol: str = "IBM",
    market: Market = Market.US,
    start: date = DATES[0],
    end: date = DATES[-1],
) -> DailyBarRequest:
    return DailyBarRequest(
        market=market,
        symbol=symbol,
        start=start,
        end=end,
        price_mode="RAW",
    )


def _payload(
    *,
    symbol: object = "IBM",
    series: object | None = None,
) -> dict[str, object]:
    if series is None:
        series = {
            "2025-07-01": {
                "1. open": "295.00",
                "2. high": "297.00",
                "3. low": "294.00",
                "4. close": "296.00",
                "5. volume": "3000000",
            }
        }
    return {
        "Meta Data": {"2. Symbol": symbol},
        "Time Series (Daily)": series,
    }


def _body(**kwargs: object) -> bytes:
    return json.dumps(_payload(**kwargs), separators=(",", ":")).encode()


def _provider(
    body: bytes | None = None,
) -> tuple[AlphaVantageDailyBarProvider, FakeTransport]:
    transport = FakeTransport(FIXTURE.read_bytes() if body is None else body)
    return AlphaVantageDailyBarProvider(api_key=TEST_KEY, transport=transport), transport


def _schedule() -> BoundedSessionSchedule:
    zone = ZoneInfo("America/New_York")
    return BoundedSessionSchedule(
        market=Market.US,
        start=DATES[0],
        end=DATES[-1],
        sessions=tuple(
            SessionScheduleRow(
                session_date=session_date,
                open_at=datetime.combine(session_date, time(9, 30), zone),
                close_at=datetime.combine(session_date, time(16), zone),
                timezone="America/New_York",
                provenance="fixture-us-schedule",
                generated_on=date(2026, 8, 4),
            )
            for session_date in DATES
        ),
    )


def test_alpha_vantage_request_fixture_and_canonical_records_are_exact() -> None:
    provider, transport = _provider()

    bars = provider.fetch_daily_bars(_request())

    assert provider.provider_id == "alpha-vantage"
    assert type(bars) is tuple
    assert tuple(bar.session_date for bar in bars) == DATES
    assert transport.calls and transport.calls[0][0] == HOST
    split = urlsplit(transport.calls[0][1])
    assert split.path == PATH
    assert parse_qsl(split.query, keep_blank_values=True) == [
        ("function", "TIME_SERIES_DAILY"),
        ("symbol", "IBM"),
        ("outputsize", "compact"),
        ("datatype", "json"),
        ("apikey", TEST_KEY),
    ]
    first = bars[0]
    assert first.market is Market.US
    assert first.symbol == first.provider_native_symbol == "IBM"
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        Decimal("295.00"),
        Decimal("297.00"),
        Decimal("294.00"),
        Decimal("296.00"),
        Decimal("3000000"),
    )
    assert tuple(item.provider_record_id for item in bars) == EXPECTED_PROVIDER_RECORD_IDS
    assert all(
        type(value) is Decimal
        for bar in bars
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
    )


@pytest.mark.parametrize("symbol", ["A", "A1", "ABCDEFGHIJ"])
def test_exact_uppercase_ascii_us_symbol_subset_is_preserved(symbol: str) -> None:
    provider, transport = _provider(_body(symbol=symbol))
    request = _request(symbol=symbol, start=DATES[0], end=DATES[0])

    bars = provider.fetch_daily_bars(request)

    assert bars[0].symbol == bars[0].provider_native_symbol == symbol
    assert dict(parse_qsl(urlsplit(transport.calls[0][1]).query))["symbol"] == symbol


@pytest.mark.parametrize(
    "symbol",
    ["ibm", "IBM.N", "BRK-B", "1IBM", "ABCDEFGHIJK", "İBM", ""],
)
def test_us_symbol_subset_rejects_punctuation_case_length_and_unicode(symbol: str) -> None:
    with pytest.raises(ValidationError):
        _request(symbol=symbol)


def test_non_us_request_is_unsupported_before_transport() -> None:
    provider, transport = _provider()
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request(symbol="600000", market=Market.CN))
    assert caught.value.code is MarketDataErrorCode.UNSUPPORTED_SYMBOL
    assert transport.calls == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, MarketDataErrorCode.SCHEMA),
        ({"Meta Data": [], "Time Series (Daily)": {}}, MarketDataErrorCode.SCHEMA),
        (_payload(symbol=7), MarketDataErrorCode.SCHEMA),
        (_payload(symbol="AAPL"), MarketDataErrorCode.IDENTITY),
        (_payload(series=[]), MarketDataErrorCode.SCHEMA),
        (_payload(series={}), MarketDataErrorCode.EMPTY),
        (_payload(series={"2025-07-01": []}), MarketDataErrorCode.SCHEMA),
        (
            _payload(
                series={
                    "2025-07-01": {
                        "1. open": "1",
                        "2. high": "1",
                        "3. low": "1",
                        "4. close": "1",
                    }
                }
            ),
            MarketDataErrorCode.SCHEMA,
        ),
        (
            _payload(
                series={
                    "2025-07-01": {
                        "1. open": "1",
                        "2. high": "1",
                        "3. low": "1",
                        "4. close": "1",
                        "5. volume": "1",
                        "6. adjusted close": "1",
                    }
                }
            ),
            MarketDataErrorCode.SCHEMA,
        ),
        (
            _payload(
                series={
                    "2025/07/01": {
                        "1. open": "1",
                        "2. high": "1",
                        "3. low": "1",
                        "4. close": "1",
                        "5. volume": "1",
                    }
                }
            ),
            MarketDataErrorCode.SCHEMA,
        ),
        (
            _payload(
                series={
                    "2025-07-01": {
                        "1. open": "not-a-decimal",
                        "2. high": "1",
                        "3. low": "1",
                        "4. close": "1",
                        "5. volume": "1",
                    }
                }
            ),
            MarketDataErrorCode.NUMERIC,
        ),
        (
            _payload(
                series={
                    "2025-07-01": {
                        "1. open": 1,
                        "2. high": "1",
                        "3. low": "1",
                        "4. close": "1",
                        "5. volume": "1",
                    }
                }
            ),
            MarketDataErrorCode.SCHEMA,
        ),
    ],
)
def test_metadata_series_record_date_and_numeric_fail_all_or_nothing(
    payload: dict[str, object],
    expected: MarketDataErrorCode,
) -> None:
    provider, _ = _provider(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request(start=DATES[0], end=DATES[0]))
    assert caught.value.code is expected


def test_complete_series_is_validated_before_inclusive_filtering() -> None:
    payload = json.loads(FIXTURE.read_bytes())
    payload["Time Series (Daily)"]["2025-06-30"]["1. open"] = "NATIVE_BAD_OUTSIDE_RANGE"
    provider, _ = _provider(json.dumps(payload, separators=(",", ":")).encode())

    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())

    assert caught.value.code is MarketDataErrorCode.NUMERIC


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            b'{"Meta Data":{"2. Symbol":"IBM"},"Time Series (Daily)":{},'
            b'"Time Series (Daily)":{}}',
            MarketDataErrorCode.DUPLICATE,
        ),
        (
            b'{"Meta Data":{"2. Symbol":"IBM"},"Time Series (Daily)":{'
            b'"2025-07-01":{"1. open":"1"},'
            b'"2025-07-01":{"1. open":"1"}}}',
            MarketDataErrorCode.DUPLICATE,
        ),
        (
            b'{"Meta Data":{"2. Symbol":"IBM"},"Time Series (Daily)":'
            b'{"2025-07-01":{"1. open":NaN}}}',
            MarketDataErrorCode.MALFORMED_JSON,
        ),
        (b"{} trailing", MarketDataErrorCode.MALFORMED_JSON),
    ],
)
def test_alpha_vantage_uses_shared_strict_json_admission(
    body: bytes,
    expected: MarketDataErrorCode,
) -> None:
    provider, _ = _provider(body)
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())
    assert caught.value.code is expected


@pytest.mark.parametrize(
    ("payload", "expected", "retryable"),
    [
        (
            {"Error Message": "The API key is invalid or missing."},
            MarketDataErrorCode.AUTH,
            False,
        ),
        (
            {"Error Message": "Invalid API call. Please retry."},
            MarketDataErrorCode.INVALID_REQUEST,
            False,
        ),
        (
            {"Note": "Thank you. Your API call frequency is 5 calls per minute."},
            MarketDataErrorCode.THROTTLED,
            True,
        ),
        (
            {"Information": "The standard API rate limit has been reached."},
            MarketDataErrorCode.THROTTLED,
            True,
        ),
        (
            {"Information": "This endpoint is available to premium members."},
            MarketDataErrorCode.INFORMATION,
            False,
        ),
        (
            {
                "Error Message": "Invalid API call.",
                "Note": "API call frequency exceeded.",
                "Information": "Other information.",
            },
            MarketDataErrorCode.INVALID_REQUEST,
            False,
        ),
    ],
)
def test_semantic_provider_categories_have_stable_precedence_and_retry_policy(
    payload: dict[str, object],
    expected: MarketDataErrorCode,
    retryable: bool,
) -> None:
    provider, _ = _provider(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())
    assert caught.value.code is expected
    assert caught.value.retryable is retryable
    assert caught.value.metadata == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"Error Message": 7}, MarketDataErrorCode.SCHEMA),
        ({"Note": "Provider maintenance"}, MarketDataErrorCode.SCHEMA),
        ({"Information": 7}, MarketDataErrorCode.SCHEMA),
    ],
)
def test_provider_category_values_require_exact_semantic_strings(
    payload: dict[str, object], expected: MarketDataErrorCode
) -> None:
    provider, _ = _provider(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())
    assert caught.value.code is expected


def test_compact_coverage_empty_and_late_end_are_distinct() -> None:
    provider, _ = _provider()
    with pytest.raises(MarketDataError) as too_early:
        provider.fetch_daily_bars(_request(start=date(2025, 6, 29)))
    assert too_early.value.code is MarketDataErrorCode.COMPACT_COVERAGE

    with pytest.raises(MarketDataError) as empty:
        provider.fetch_daily_bars(
            _request(start=date(2025, 7, 5), end=date(2025, 7, 6))
        )
    assert empty.value.code is MarketDataErrorCode.EMPTY

    bars = provider.fetch_daily_bars(_request(start=date(2025, 7, 8), end=date(2025, 7, 12)))
    assert tuple(item.session_date for item in bars) == (date(2025, 7, 8), date(2025, 7, 9))


def test_unsorted_native_series_is_sorted_only_after_complete_admission() -> None:
    series = {
        "2025-07-02": {
            "1. open": "2",
            "2. high": "2",
            "3. low": "2",
            "4. close": "2",
            "5. volume": "2",
        },
        "2025-07-01": {
            "1. open": "1",
            "2. high": "1",
            "3. low": "1",
            "4. close": "1",
            "5. volume": "1",
        },
    }
    provider, _ = _provider(_body(series=series))
    bars = provider.fetch_daily_bars(_request(start=DATES[0], end=DATES[1]))
    assert tuple(item.session_date for item in bars) == DATES[:2]


def test_fixture_metadata_has_exact_secret_free_sha256_provenance() -> None:
    fixture_bytes = FIXTURE.read_bytes()
    metadata = json.loads(METADATA.read_text())

    assert metadata == {
        "provider_id": "alpha-vantage",
        "retrieved_utc": "2026-08-04T00:00:00Z",
        "request_parameters": {
            "function": "TIME_SERIES_DAILY",
            "symbol": "IBM",
            "outputsize": "compact",
            "datatype": "json",
        },
        "source_type": "documentation-derived",
        "transformations_redactions": [
            "Constructed from the documented Alpha Vantage TIME_SERIES_DAILY "
            "compact response shape.",
            "Limited to seven IBM-shaped daily rows spanning the five-session test "
            "schedule plus coverage boundary rows.",
            "The private apikey parameter was omitted; no credentials, cookies, "
            "request headers, or user data were read or added.",
        ],
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
    }
    assert TEST_KEY not in METADATA.read_text()


def _assemble(database: str, provider: object | None = None) -> tuple[str, str, str]:
    if provider is None:
        provider, _ = _provider()
    store = PointInTimeStore(database)
    try:
        report = IncrementalBarIngestor(provider=provider, store=store).ingest(
            request=_request(),
            schedule=_schedule(),
            ingested_at=datetime(2025, 7, 10, 12, tzinfo=UTC),
        )
        spec = build_real_data_backtest_spec(
            store=store,
            schedule=_schedule(),
            run_id="alpha-vantage-us-fixture-run",
            account_id="us-account",
            instruments=(
                Instrument(
                    symbol="IBM",
                    market=Market.US,
                    currency=Currency.USD,
                    sector="Technology",
                ),
            ),
            initial_cash=Decimal("100000"),
            strategy_config_version="v1",
        )
        result = ChronologicalBacktestRunner(
            store=store,
            calendar=TradingCalendar(Market.US, DATES),
            strategy=EmptyUsStrategy(),
        ).run(spec)
        assert report.requested == report.received == report.appended == 5
        assert report.unchanged == 0
        assert tuple(
            item.source_record_id for item in report.appended_revision_identities
        ) == EXPECTED_SOURCE_RECORD_IDS
        assert spec.pit_knowledge_policy == result.manifest.pit_knowledge_policy == (
            "current-view-baseline/v1"
        )
        assert spec.market_data_price_policy == result.manifest.market_data_price_policy == (
            "raw-unadjusted/no-corporate-actions/v1"
        )
        assert tuple(len(item.selected_revisions) for item in result.sessions) == (1, 2, 3, 4, 5)
        assert all(
            session.open_frame_sources[0].source == "alpha-vantage"
            for session in spec.sessions
        )
        assert all(
            session.open_frame_sources[0].source_record_id
            == result.sessions[index].selected_revisions[index].source_record_id
            for index, session in enumerate(spec.sessions)
        )
        return (
            report.model_dump_json(),
            spec.model_dump_json(),
            result.model_dump_json(),
        )
    finally:
        store.close()


def test_fake_transport_to_file_duckdb_builder_runner_is_deterministic(tmp_path: Path) -> None:
    normalized_provider, transport = _provider()
    normalized = normalized_provider.fetch_daily_bars(_request())

    first = _assemble(str(tmp_path / "alpha-first.duckdb"), StaticProvider(normalized))
    second = _assemble(str(tmp_path / "alpha-second.duckdb"), StaticProvider(normalized))

    assert len(transport.calls) == 1
    assert first == second
    assert TEST_KEY not in "".join(first)


def test_fake_transport_alpha_ingestor_file_duckdb_builder_runner_vertical(
    tmp_path: Path,
) -> None:
    provider, transport = _provider()

    output = _assemble(str(tmp_path / "alpha-vertical.duckdb"), provider)

    assert len(transport.calls) == 1
    assert all(output)
    assert TEST_KEY not in "".join(output)


def test_missing_expected_closed_session_reaches_shared_builder_as_data_gap(
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURE.read_bytes())
    del payload["Time Series (Daily)"]["2025-07-03"]
    provider, _ = _provider(json.dumps(payload, separators=(",", ":")).encode())
    store = PointInTimeStore(str(tmp_path / "alpha-gap.duckdb"))
    try:
        report = IncrementalBarIngestor(provider=provider, store=store).ingest(
            request=_request(),
            schedule=_schedule(),
            ingested_at=datetime(2025, 7, 10, 12, tzinfo=UTC),
        )
        assert (report.requested, report.received) == (5, 4)
        with pytest.raises(ValueError, match="missing session data"):
            build_real_data_backtest_spec(
                store=store,
                schedule=_schedule(),
                run_id="alpha-vantage-gap-run",
                account_id="us-account",
                instruments=(
                    Instrument(
                        symbol="IBM",
                        market=Market.US,
                        currency=Currency.USD,
                        sector="Technology",
                    ),
                ),
                initial_cash=Decimal("100000"),
                strategy_config_version="v1",
            )
    finally:
        store.close()
