from datetime import UTC, date, datetime
from decimal import Decimal

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
