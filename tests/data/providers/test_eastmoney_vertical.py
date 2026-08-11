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
from stock_agent.data.providers.eastmoney import EastmoneyDailyBarProvider
from stock_agent.domain import Currency, Instrument, Market
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext
from tests.data.providers.exception_graph import (
    assert_exception_graph_excludes,
    capture_market_data_error,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "eastmoney_600000_daily.json"
METADATA = FIXTURES / "eastmoney_600000_daily.metadata.json"
DATES = tuple(date(2025, 7, day) for day in (1, 2, 3, 4, 7))
HOST = "push2his.eastmoney.com"
PATH = "/api/qt/stock/kline/get"
EXPECTED_PROVIDER_RECORD_ID = (
    "provider-payload-sha256:"
    "270d47f908c20b4dc352a7cc6add0b652a2f9a5f04617717d8950a4011ab1ebb"
)
EXPECTED_SOURCE_RECORD_IDS = (
    "market-data-source-record-sha256:"
    "9950769199e777806ebd62cbb8b0775a91fd02f1ec765e4f3c8082966d0a34fd",
    "market-data-source-record-sha256:"
    "04721884d66a1375fdc9ce0439bbad360163f5794ef97aa6911ee749ba461d04",
    "market-data-source-record-sha256:"
    "e361d4f264c94635338a2d4f735614d95c98f5672131109220ba231fe8e09a54",
    "market-data-source-record-sha256:"
    "32870b6c5642442190a0d85b9aec17bd1d1ef823439f189aa3641eb7eeba11d3",
    "market-data-source-record-sha256:"
    "976457e6afc83a580005c252c9c15928aa81bc5730461740fab8ac73aa2fb4ed",
)
EXPECTED_SPEC_FINGERPRINT = (
    "backtest-spec-sha256:"
    "1785fe233065178c534dd9bcb1a918913ac725ba2c12726c284f7c930024722d"
)
EXPECTED_RESOLVED_FINGERPRINT = (
    "resolved-data-sha256:"
    "9016924f38e292d163241acc82f5ab54f6a9735d950c0c1e5fe646f80c2d4f7e"
)


@pytest.fixture(scope="module", autouse=True)
def _block_real_sockets() -> Any:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Capability2 tests must never open sockets")

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
    provider_id = "eastmoney"

    def __init__(self, bars: tuple[object, ...]) -> None:
        self._bars = bars

    def fetch_daily_bars(self, request: DailyBarRequest) -> tuple[object, ...]:
        return self._bars


@dataclass(frozen=True)
class EmptyCnStrategy:
    strategy_id: str = "cn-empty"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[()]:
        return ()


def _request(
    *,
    symbol: str = "600000",
    market: Market = Market.CN,
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


def _response(
    *,
    symbol: str = "600000",
    market: object = 1,
    rows: object | None = None,
    rc: object = 0,
) -> bytes:
    if rows is None:
        rows = [
            "2025-07-01,13.36,13.55,13.56,13.32,538889,"
            "724850321.00,1.80,1.42,0.19,0.24"
        ]
    return json.dumps(
        {"rc": rc, "data": {"code": symbol, "market": market, "klines": rows}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _provider(body: bytes | None = None) -> tuple[EastmoneyDailyBarProvider, FakeTransport]:
    transport = FakeTransport(FIXTURE.read_bytes() if body is None else body)
    return EastmoneyDailyBarProvider(transport=transport), transport


def _schedule() -> BoundedSessionSchedule:
    zone = ZoneInfo("Asia/Shanghai")
    return BoundedSessionSchedule(
        market=Market.CN,
        start=DATES[0],
        end=DATES[-1],
        sessions=tuple(
            SessionScheduleRow(
                session_date=session_date,
                open_at=datetime.combine(session_date, time(9, 30), zone),
                close_at=datetime.combine(session_date, time(15), zone),
                timezone="Asia/Shanghai",
                provenance="fixture-cn-schedule",
                generated_on=date(2026, 8, 4),
            )
            for session_date in DATES
        ),
    )


def _states() -> dict[date, tuple[CnSessionState, ...]]:
    return {
        session_date: (
            CnSessionState(
                symbol="600000",
                session_date=session_date,
                suspended=False,
                price_limit_state=CnPriceLimitState.NONE,
            ),
        )
        for session_date in DATES
    }


def test_eastmoney_request_and_fixture_parse_are_exact() -> None:
    provider, transport = _provider()

    bars = provider.fetch_daily_bars(_request())

    assert provider.provider_id == "eastmoney"
    assert type(bars) is tuple
    assert tuple(bar.session_date for bar in bars) == DATES
    assert transport.calls and transport.calls[0][0] == HOST
    split = urlsplit(transport.calls[0][1])
    assert split.path == PATH
    assert dict(parse_qsl(split.query, keep_blank_values=True)) == {
        "secid": "1.600000",
        "beg": "20250701",
        "end": "20250707",
        "klt": "101",
        "fqt": "0",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    assert bars[0].market is Market.CN
    assert bars[0].symbol == "600000"
    assert bars[0].open == Decimal("13.36")
    assert bars[0].close == Decimal("13.55")
    assert bars[0].high == Decimal("13.56")
    assert bars[0].low == Decimal("13.32")
    assert bars[0].volume == Decimal("538889")
    assert bars[0].provider_native_symbol == "1.600000"
    assert bars[0].provider_record_id == EXPECTED_PROVIDER_RECORD_ID
    assert all(
        type(value) is Decimal
        for bar in bars
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
    )


@pytest.mark.parametrize("symbol", ["000001", "300001"])
def test_shenzhen_symbols_map_only_to_market_zero(symbol: str) -> None:
    provider, transport = _provider(
        _response(
            symbol=symbol,
            market=0,
            rows=[
                "2025-07-01,10.00,10.10,10.20,9.90,100,"
                "1000,3.00,1.00,0.10,2.00"
            ],
        )
    )

    bars = provider.fetch_daily_bars(_request(symbol=symbol, start=DATES[0], end=DATES[0]))

    query = dict(parse_qsl(urlsplit(transport.calls[0][1]).query))
    assert query["secid"] == f"0.{symbol}"
    assert bars[0].provider_native_symbol == f"0.{symbol}"


def test_non_cn_request_is_explicitly_unsupported_before_transport() -> None:
    provider, transport = _provider()
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(
            _request(symbol="AAPL", market=Market.US)
        )
    assert caught.value.code is MarketDataErrorCode.UNSUPPORTED_SYMBOL
    assert transport.calls == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_response(rc=7), MarketDataErrorCode.INFORMATION),
        (_response(rc=True), MarketDataErrorCode.SCHEMA),
        (_response(symbol="600001"), MarketDataErrorCode.IDENTITY),
        (_response(market=0), MarketDataErrorCode.IDENTITY),
        (b'{"rc":0,"data":[]}', MarketDataErrorCode.SCHEMA),
        (b'{"rc":0,"data":{"code":"600000","market":1,"klines":{}}}', MarketDataErrorCode.SCHEMA),
        (_response(rows=[]), MarketDataErrorCode.EMPTY),
        (_response(rows=[1]), MarketDataErrorCode.SCHEMA),
        (_response(rows=["2025-07-01,1,1,1,1,1,1,1,1,1"]), MarketDataErrorCode.SCHEMA),
        (_response(rows=["2025-07-01,1,1,1,1,1,1,1,1,1,"]), MarketDataErrorCode.SCHEMA),
        (_response(rows=["2025/07/01,1,1,1,1,1,1,1,1,1,1"]), MarketDataErrorCode.SCHEMA),
        (_response(rows=["2025-07-01,nope,1,1,1,1,1,1,1,1,1"]), MarketDataErrorCode.NUMERIC),
        (_response(rows=["2025-07-01,10,10,9,8,1,1,1,1,1,1"]), MarketDataErrorCode.NUMERIC),
        (
            _response(
                rows=[
                    "2025-07-01,1,1,1,1,1,1,1,1,1,1",
                    "2025-07-01,1,1,1,1,1,1,1,1,1,1",
                ]
            ),
            MarketDataErrorCode.DUPLICATE,
        ),
        (_response(rows=["2025-06-30,1,1,1,1,1,1,1,1,1,1"]), MarketDataErrorCode.OUT_OF_RANGE),
    ],
)
def test_schema_row_numeric_duplicate_range_and_empty_fail_all_or_nothing(
    body: bytes,
    expected: MarketDataErrorCode,
) -> None:
    provider, _ = _provider(body)
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())
    assert caught.value.code is expected


def test_native_date_parse_failure_does_not_survive_in_exception_graph() -> None:
    provider, _ = _provider(
        _response(rows=["2025-02-30,1,1,1,1,1,1,1,1,1,1"])
    )
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())

    assert caught.value.code is MarketDataErrorCode.SCHEMA
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "2025-02-30" not in repr(caught.value)


def test_eastmoney_public_failure_boundary_hides_body_payload_and_raw_row() -> None:
    raw_row = "2025-07-01,CAP2_RAW_ROW_31ef,1,1,1,1,1,1,1,1,1"
    body = _response(rows=[raw_row])
    provider, _ = _provider(body)

    caught = capture_market_data_error(
        lambda: provider.fetch_daily_bars(_request())
    )

    assert caught.code is MarketDataErrorCode.NUMERIC
    assert_exception_graph_excludes(caught, (body, body.decode(), raw_row, "CAP2_RAW_ROW_31ef"))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"rc":0,"rc":0,"data":{}}', MarketDataErrorCode.DUPLICATE),
        (
            b'{"rc":0,"data":{"code":"600000","code":"600000",'
            b'"market":1,"klines":[]}}',
            MarketDataErrorCode.DUPLICATE,
        ),
        (b'{"rc":NaN,"data":{}}', MarketDataErrorCode.MALFORMED_JSON),
        (b'{} trailing', MarketDataErrorCode.MALFORMED_JSON),
    ],
)
def test_eastmoney_uses_shared_strict_json_admission(
    body: bytes,
    expected: MarketDataErrorCode,
) -> None:
    provider, _ = _provider(body)
    with pytest.raises(MarketDataError) as caught:
        provider.fetch_daily_bars(_request())
    assert caught.value.code is expected


def test_unsorted_rows_are_admitted_completely_then_sorted() -> None:
    rows = [
        "2025-07-02,10,11,12,9,100,1,1,1,1,1",
        "2025-07-01,9,10,11,8,90,1,1,1,1,1",
    ]
    provider, _ = _provider(_response(rows=rows))
    bars = provider.fetch_daily_bars(_request(end=DATES[1]))
    assert tuple(bar.session_date for bar in bars) == DATES[:2]


def test_fixture_metadata_has_exact_secret_free_sha256_provenance() -> None:
    fixture_bytes = FIXTURE.read_bytes()
    metadata = json.loads(METADATA.read_text())

    assert metadata == {
        "provider_id": "eastmoney",
        "retrieved_utc": "2026-08-04T09:00:29Z",
        "request_parameters": {
            "secid": "1.600000",
            "beg": "20250701",
            "end": "20250707",
            "klt": "101",
            "fqt": "0",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        "source_type": "documentation-derived",
        "transformations_redactions": [
            "Constructed from the documented Eastmoney response shape after the permitted "
            "live request failed at DNS resolution.",
            "Limited to five 600000 daily rows in the bounded 2025-07-01 through 2025-07-07 range.",
            "No credentials, cookies, request headers, or user data were present or added.",
        ],
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
    }


def _assemble(
    provider: object | None = None,
) -> tuple[Any, ...]:
    if provider is None:
        provider, transport = _provider()
    else:
        transport = None
    store = PointInTimeStore()
    request = _request()
    schedule = _schedule()
    report = IncrementalBarIngestor(provider=provider, store=store).ingest(
        request=request,
        schedule=schedule,
        ingested_at=datetime(2025, 7, 8, 9, tzinfo=UTC),
    )
    spec = build_real_data_backtest_spec(
        store=store,
        schedule=schedule,
        run_id="eastmoney-cn-fixture-run",
        account_id="cn-account",
        instruments=(
            Instrument(
                symbol="600000",
                market=Market.CN,
                currency=Currency.CNY,
                sector="Banking",
            ),
        ),
        initial_cash=Decimal("100000"),
        strategy_config_version="v1",
        cn_session_states_by_date=_states(),
    )
    result = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.CN, DATES),
        strategy=EmptyCnStrategy(),
    ).run(spec)
    return report, spec, result, transport, store


def test_fake_transport_to_eastmoney_to_duckdb_builder_runner_vertical_slice() -> None:
    report, spec, result, transport, store = _assemble()
    try:
        assert transport is not None
        assert len(transport.calls) == 1
        assert report.requested == report.received == report.appended == 5
        assert report.unchanged == 0
        assert tuple(
            item.source_record_id for item in report.appended_revision_identities
        ) == EXPECTED_SOURCE_RECORD_IDS
        assert spec.pit_knowledge_policy == "current-view-baseline/v1"
        assert spec.market_data_price_policy == "raw-unadjusted/no-corporate-actions/v1"
        assert result.manifest.pit_knowledge_policy == "current-view-baseline/v1"
        assert result.manifest.market_data_price_policy == (
            "raw-unadjusted/no-corporate-actions/v1"
        )
        assert result.spec_fingerprint == EXPECTED_SPEC_FINGERPRINT
        assert result.resolved_data_fingerprint == EXPECTED_RESOLVED_FINGERPRINT
        assert tuple(len(session.selected_revisions) for session in result.sessions) == (
            1,
            2,
            3,
            4,
            5,
        )
        expected_opens = tuple(
            Decimal(value) for value in ("13.36", "13.55", "13.46", "13.60", "13.58")
        )
        for index, (expected_open, session, result_session) in enumerate(
            zip(expected_opens, spec.sessions, result.sessions, strict=True)
        ):
            source = session.open_frame_sources[0]
            own_revision = result_session.selected_revisions[index]
            assert session.open_bars[0].open == expected_open
            assert session.open_bars[0].volume == Decimal(0)
            assert source.source == own_revision.source == "eastmoney"
            assert source.source_record_id == own_revision.source_record_id
            assert source.available_at == own_revision.bar.available_at
            assert source.ingested_at == own_revision.ingested_at
    finally:
        store.close()


def test_file_duckdb_rerun_and_single_correction_flow_into_builder_and_runner(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "capability2.duckdb")
    baseline_body = FIXTURE.read_bytes()
    corrected_open = "13.50"
    payload = json.loads(baseline_body)
    corrected_rows = list(payload["data"]["klines"])
    corrected_fields = corrected_rows[2].split(",")
    corrected_fields[1] = corrected_open
    corrected_rows[2] = ",".join(corrected_fields)
    payload["data"]["klines"] = corrected_rows
    corrected_body = json.dumps(payload, separators=(",", ":")).encode()
    transport = FakeTransport(baseline_body)
    provider = EastmoneyDailyBarProvider(transport=transport)
    store = PointInTimeStore(database)
    request = _request()
    schedule = _schedule()
    ingestor = IncrementalBarIngestor(provider=provider, store=store)
    baseline_clock = datetime(2025, 7, 8, 9, tzinfo=UTC)
    corrected_identity = None
    try:
        baseline = ingestor.ingest(
            request=request, schedule=schedule, ingested_at=baseline_clock
        )
        rerun = ingestor.ingest(
            request=request, schedule=schedule, ingested_at=baseline_clock
        )
        transport.body = corrected_body
        correction_clock = datetime(2025, 7, 8, 9, 0, 1, tzinfo=UTC)
        correction = ingestor.ingest(
            request=request, schedule=schedule, ingested_at=correction_clock
        )

        assert (baseline.appended, baseline.unchanged) == (5, 0)
        assert (rerun.appended, rerun.unchanged) == (0, 5)
        assert (correction.appended, correction.unchanged) == (1, 4)
        corrected_identity = correction.appended_revision_identities[0]
        assert corrected_identity.source == "eastmoney"

        spec = build_real_data_backtest_spec(
            store=store,
            schedule=schedule,
            run_id="eastmoney-cn-correction-run",
            account_id="cn-account",
            instruments=(
                Instrument(
                    symbol="600000",
                    market=Market.CN,
                    currency=Currency.CNY,
                    sector="Banking",
                ),
            ),
            initial_cash=Decimal("100000"),
            strategy_config_version="v1",
            cn_session_states_by_date=_states(),
        )
        result = ChronologicalBacktestRunner(
            store=store,
            calendar=TradingCalendar(Market.CN, DATES),
            strategy=EmptyCnStrategy(),
        ).run(spec)
        selected_source = spec.sessions[2].open_frame_sources[0]
        selected_revision = result.sessions[2].selected_revisions[2]
        assert spec.sessions[2].open_bars[0].open == Decimal("13.46")
        assert selected_revision.bar.open == Decimal("13.46")
        assert selected_source.source == selected_revision.source == "eastmoney"
        assert selected_source.source_record_id == EXPECTED_SOURCE_RECORD_IDS[2]
        assert selected_revision.source_record_id == EXPECTED_SOURCE_RECORD_IDS[2]
        assert selected_source.source_record_id != corrected_identity.source_record_id
        assert selected_source.available_at == schedule.sessions[2].close_at
        assert selected_revision.bar.available_at == schedule.sessions[2].close_at
        assert selected_source.ingested_at == baseline_clock
        assert selected_revision.ingested_at == baseline_clock
    finally:
        store.close()

    assert corrected_identity is not None
    reopened = PointInTimeStore(database)
    try:
        persisted = reopened.latest_observed_bar_revision(
            provider_id="eastmoney",
            market=Market.CN,
            symbol="600000",
            session_date=DATES[2],
        )
        assert persisted is not None
        assert persisted.bar.open == Decimal(corrected_open)
        assert persisted.bar.available_at == correction_clock
        assert persisted.ingested_at == correction_clock
        assert persisted.source == "eastmoney"
        assert persisted.source_record_id == corrected_identity.source_record_id
    finally:
        reopened.close()


def test_two_independent_assemblies_from_fixture_tuple_are_byte_deterministic() -> None:
    eastmoney, transport = _provider()
    normalized = eastmoney.fetch_daily_bars(_request())
    first = _assemble(StaticProvider(normalized))
    second = _assemble(StaticProvider(normalized))
    try:
        first_report, first_spec, first_result, _, _ = first
        second_report, second_spec, second_result, _, _ = second
        assert len(transport.calls) == 1
        assert first_report.model_dump_json() == second_report.model_dump_json()
        assert first_spec.model_dump_json() == second_spec.model_dump_json()
        assert first_result.model_dump_json() == second_result.model_dump_json()
        assert first_result.spec_fingerprint == second_result.spec_fingerprint
        assert (
            first_result.resolved_data_fingerprint
            == second_result.resolved_data_fingerprint
        )
    finally:
        first[-1].close()
        second[-1].close()
