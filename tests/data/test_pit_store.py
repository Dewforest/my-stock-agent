from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

import stock_agent.data as data
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Market


def make_bar(**overrides: object) -> Bar:
    values = {
        "symbol": "AAPL",
        "market": Market.US,
        "session_date": date(2026, 7, 24),
        "open": Decimal("330.10"),
        "high": Decimal("335.20"),
        "low": Decimal("329.80"),
        "close": Decimal("333.02"),
        "volume": Decimal("1234567"),
        "available_at": datetime(2026, 7, 25, tzinfo=UTC),
    }
    values.update(overrides)
    return Bar(**values)


def test_appends_and_reads_single_bar() -> None:
    store = PointInTimeStore()
    bar = make_bar()

    store.append_bar(
        bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="aapl-2026-07-24-v1",
    )

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) == bar


def test_future_revision_is_hidden_until_available_then_becomes_visible() -> None:
    store = PointInTimeStore()
    old_bar = make_bar()
    revised_bar = make_bar(
        close=Decimal("334.00"),
        available_at=datetime(2026, 7, 27, tzinfo=UTC),
    )
    store.append_bar(
        old_bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )
    store.append_bar(
        revised_bar,
        ingested_at=datetime(2026, 7, 28, tzinfo=UTC),
        source="test-feed",
        source_record_id="v2",
    )

    before_revision = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    )
    at_revision = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 27, tzinfo=UTC),
    )

    assert before_revision == old_bar
    assert at_revision == revised_bar


def test_latest_ingestion_wins_when_available_at_is_equal() -> None:
    store = PointInTimeStore()
    first = make_bar(close=Decimal("333.02"))
    corrected = make_bar(close=Decimal("334.00"))
    store.append_bar(
        first,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )
    store.append_bar(
        corrected,
        ingested_at=datetime(2026, 7, 25, 2, tzinfo=UTC),
        source="test-feed",
        source_record_id="v2",
    )

    result = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    )

    assert result == corrected


def test_equal_revision_timestamps_use_provenance_as_stable_tie_breaker() -> None:
    earlier_provenance = make_bar(close=Decimal("333.02"))
    later_provenance = make_bar(close=Decimal("334.00"))
    ingested_at = datetime(2026, 7, 25, 1, tzinfo=UTC)
    stores = [PointInTimeStore(), PointInTimeStore()]
    revisions = [
        (earlier_provenance, "alpha-feed", "record-z"),
        (later_provenance, "zulu-feed", "record-a"),
    ]

    for store, ordered_revisions in zip(stores, (revisions, reversed(revisions)), strict=True):
        for bar, source, source_record_id in ordered_revisions:
            store.append_bar(
                bar,
                ingested_at=ingested_at,
                source=source,
                source_record_id=source_record_id,
            )

    results = [
        store.latest_bar_as_of(
            market=Market.US,
            symbol="AAPL",
            session_date=date(2026, 7, 24),
            as_of=datetime(2026, 7, 26, tzinfo=UTC),
        )
        for store in stores
    ]

    assert results == [later_provenance, later_provenance]


def test_returns_none_when_no_bar_matches() -> None:
    store = PointInTimeStore()

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) is None


def test_market_is_part_of_bar_identity() -> None:
    store = PointInTimeStore()
    us_bar = make_bar()
    store.append_bar(
        us_bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="us-v1",
    )

    assert store.latest_bar_as_of(
        market=Market.CN,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) is None


def test_query_strips_and_uppercases_symbol() -> None:
    store = PointInTimeStore()
    bar = make_bar(symbol=" aapl ")
    store.append_bar(
        bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="  aapl  ",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) == bar


def test_rejects_naive_as_of() -> None:
    store = PointInTimeStore()

    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        store.latest_bar_as_of(
            market=Market.US,
            symbol="AAPL",
            session_date=date(2026, 7, 24),
            as_of=datetime(2026, 7, 26),
        )


def test_rejects_naive_ingested_at() -> None:
    store = PointInTimeStore()

    with pytest.raises(ValueError, match="ingested_at must be timezone-aware"):
        store.append_bar(
            make_bar(),
            ingested_at=datetime(2026, 7, 25, 1),
            source="test-feed",
            source_record_id="v1",
        )


def test_rejects_ingestion_before_bar_is_available() -> None:
    store = PointInTimeStore()

    with pytest.raises(ValueError, match="ingested_at cannot be before available_at"):
        store.append_bar(
            make_bar(),
            ingested_at=datetime(2026, 7, 24, 23, tzinfo=UTC),
            source="test-feed",
            source_record_id="v1",
        )


@pytest.mark.parametrize(
    ("source", "source_record_id", "message"),
    [
        ("   ", "v1", "source must not be blank"),
        ("test-feed", "   ", "source_record_id must not be blank"),
    ],
)
def test_rejects_blank_source_metadata(
    source: str, source_record_id: str, message: str
) -> None:
    store = PointInTimeStore()

    with pytest.raises(ValueError, match=message):
        store.append_bar(
            make_bar(),
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source=source,
            source_record_id=source_record_id,
        )


def test_decimal_and_aware_datetime_round_trip() -> None:
    store = PointInTimeStore()
    bar = make_bar(
        open=Decimal("330.123456789012"),
        high=Decimal("335.123456789012"),
        low=Decimal("329.123456789012"),
        close=Decimal("333.123456789012"),
        volume=Decimal("1234567.000000000001"),
    )
    store.append_bar(
        bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )

    result = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    )

    assert result == bar
    assert result is not None
    assert all(
        isinstance(value, Decimal)
        for value in (result.open, result.high, result.low, result.close, result.volume)
    )
    assert result.available_at.tzinfo is not None
    assert result.available_at.utcoffset() is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", Decimal("100.0000000000001")),
        ("high", Decimal("101.0000000000001")),
        ("low", Decimal("99.0000000000001")),
        ("close", Decimal("100.0000000000001")),
        ("volume", Decimal("100.0000000000001")),
    ],
)
def test_rejects_nonzero_thirteenth_decimal_place_without_inserting(
    field: str, value: Decimal
) -> None:
    store = PointInTimeStore()
    values = {
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": Decimal("100"),
    }
    values[field] = value
    bar = make_bar(**values)

    with pytest.raises(ValueError, match=rf"{field}.*DECIMAL\(38, 12\)"):
        store.append_bar(
            bar,
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source="test-feed",
            source_record_id=f"invalid-{field}",
        )

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) is None


@pytest.mark.parametrize("value", [Decimal("1E+26"), Decimal("1.1E+26")])
def test_rejects_decimal_integer_part_outside_storage_range_without_inserting(
    value: Decimal,
) -> None:
    store = PointInTimeStore()
    bar = make_bar(volume=value)

    with pytest.raises(ValueError, match=r"volume.*DECIMAL\(38, 12\)"):
        store.append_bar(
            bar,
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source="test-feed",
            source_record_id="oversized-volume",
        )

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) is None


def test_accepts_exact_trailing_zero_quantization_and_round_trips() -> None:
    store = PointInTimeStore()
    bar = make_bar(
        open=Decimal("100.0000000000000"),
        high=Decimal("101.0000000000000"),
        low=Decimal("99.0000000000000"),
        close=Decimal("100.0000000000000"),
        volume=Decimal("100.0000000000000"),
    )
    store.append_bar(
        bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="trailing-zeroes",
    )

    result = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    )

    assert result == bar
    assert result is not None
    assert result.open == Decimal("100.0000000000000")


def test_same_normalized_revision_is_an_idempotent_no_op(tmp_path: Path) -> None:
    database = str(tmp_path / "bars.duckdb")
    store = PointInTimeStore(database)
    ingested_at = datetime(2026, 7, 25, 1, tzinfo=UTC)
    store.append_bar(
        make_bar(),
        ingested_at=ingested_at,
        source=" test-feed ",
        source_record_id=" revision-1 ",
    )
    store.append_bar(
        make_bar(symbol=" aapl ", close=Decimal("333.0200000000000")),
        ingested_at=ingested_at,
        source="test-feed",
        source_record_id="revision-1",
    )
    store.close()

    connection = duckdb.connect(database, read_only=True)
    try:
        assert connection.execute("SELECT count(*) FROM bars").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("bar_overrides", "ingested_at"),
    [
        ({"symbol": "MSFT"}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"market": Market.CN}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"session_date": date(2026, 7, 23)}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"open": Decimal("331")}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"high": Decimal("336")}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"low": Decimal("328")}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"close": Decimal("334")}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        ({"volume": Decimal("2")}, datetime(2026, 7, 25, 1, tzinfo=UTC)),
        (
            {"available_at": datetime(2026, 7, 26, tzinfo=UTC)},
            datetime(2026, 7, 26, 1, tzinfo=UTC),
        ),
        ({}, datetime(2026, 7, 25, 2, tzinfo=UTC)),
    ],
)
def test_same_revision_identity_rejects_any_conflicting_payload(
    bar_overrides: dict[str, object], ingested_at: datetime
) -> None:
    store = PointInTimeStore()
    original = make_bar()
    store.append_bar(
        original,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source=" test-feed ",
        source_record_id=" revision-1 ",
    )

    with pytest.raises(ValueError, match=r"conflicting payload.*test-feed.*revision-1"):
        store.append_bar(
            make_bar(**bar_overrides),
            ingested_at=ingested_at,
            source="test-feed",
            source_record_id="revision-1",
        )

    assert store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) == original


class NoneOffsetTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize("operation", ["append", "query"])
def test_rejects_datetime_whose_timezone_has_no_utc_offset(operation: str) -> None:
    store = PointInTimeStore()
    invalid_datetime = datetime(2026, 7, 26, tzinfo=NoneOffsetTimezone())

    if operation == "append":
        with pytest.raises(ValueError, match="ingested_at must be timezone-aware"):
            store.append_bar(
                make_bar(),
                ingested_at=invalid_datetime,
                source="test-feed",
                source_record_id="v1",
            )
    else:
        with pytest.raises(ValueError, match="as_of must be timezone-aware"):
            store.latest_bar_as_of(
                market=Market.US,
                symbol="AAPL",
                session_date=date(2026, 7, 24),
                as_of=invalid_datetime,
            )


def test_file_store_persists_after_close_and_reopen(tmp_path: Path) -> None:
    database = str(tmp_path / "persistent.duckdb")
    bar = make_bar()
    store = PointInTimeStore(database)
    store.append_bar(
        bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )
    store.close()

    reopened = PointInTimeStore(database)
    assert reopened.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) == bar
    reopened.close()


def test_context_manager_closes_store_on_exit() -> None:
    with PointInTimeStore() as store:
        store.append_bar(
            make_bar(),
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source="test-feed",
            source_record_id="v1",
        )

    with pytest.raises(RuntimeError, match="PointInTimeStore is closed"):
        store.latest_bar_as_of(
            market=Market.US,
            symbol="AAPL",
            session_date=date(2026, 7, 24),
            as_of=datetime(2026, 7, 26, tzinfo=UTC),
        )


def test_close_is_idempotent_and_closed_store_rejects_operations() -> None:
    store = PointInTimeStore()
    store.close()
    store.close()

    with pytest.raises(RuntimeError, match="PointInTimeStore is closed"):
        store.append_bar(
            make_bar(),
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source="test-feed",
            source_record_id="v1",
        )
    with pytest.raises(RuntimeError, match="PointInTimeStore is closed"):
        store.latest_bar_as_of(
            market=Market.US,
            symbol="AAPL",
            session_date=date(2026, 7, 24),
            as_of=datetime(2026, 7, 26, tzinfo=UTC),
        )


def test_data_public_api_exports_only_point_in_time_store() -> None:
    assert data.__all__ == ["PointInTimeStore"]
