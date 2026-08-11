from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from pydantic import ConfigDict, PydanticDeprecatedSince20, ValidationError

import stock_agent.data as data
from stock_agent.data import BarRevisionWrite, PointInTimeStore, SelectedBarRevision
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


def test_selected_bar_revision_is_exact_frozen_and_normalized() -> None:
    bar = make_bar()
    revision = SelectedBarRevision(
        bar=bar,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source=" test-feed ",
        source_record_id=" revision-1 ",
    )

    assert tuple(SelectedBarRevision.model_fields) == (
        "bar",
        "ingested_at",
        "source",
        "source_record_id",
    )
    assert revision.model_dump() == {
        "bar": bar.model_dump(),
        "ingested_at": datetime(2026, 7, 25, 1, tzinfo=UTC),
        "source": "test-feed",
        "source_record_id": "revision-1",
    }
    assert not any(type(value) is tuple for value in revision.__dict__.values())
    with pytest.raises(ValidationError):
        revision.source = "other-feed"
    with pytest.raises(ValidationError):
        SelectedBarRevision(
            bar=bar,
            ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
            source="test-feed",
            source_record_id="revision-1",
            unknown=True,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ingested_at": datetime(2026, 7, 25, 1)}, "timezone"),
        ({"source": "   "}, "source"),
        ({"source_record_id": "   "}, "source_record_id"),
    ],
)
def test_selected_bar_revision_rejects_invalid_metadata(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "bar": make_bar(),
        "ingested_at": datetime(2026, 7, 25, 1, tzinfo=UTC),
        "source": "test-feed",
        "source_record_id": "revision-1",
    }
    values.update(overrides)

    with pytest.raises(ValidationError, match=message):
        SelectedBarRevision(**values)


def test_selected_bar_revision_rejects_subclasses_and_copy_pollution() -> None:
    revision = SelectedBarRevision(
        bar=make_bar(),
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="revision-1",
    )

    with pytest.raises(TypeError, match="does not support subclasses"):

        class MutableSelectedBarRevision(SelectedBarRevision):
            model_config = ConfigDict(frozen=False)

    message = "immutable selected revisions do not support copy projections or updates"
    for kwargs in ({"include": {}}, {"exclude": set()}, {"update": {}}):
        with pytest.raises(TypeError, match=rf"^{message}$"):
            revision.copy(**kwargs)
    with pytest.raises(TypeError, match=rf"^{message}$"):
        revision.model_copy(update={})

    with pytest.warns(PydanticDeprecatedSince20):
        shallow = revision.copy()
    with pytest.warns(PydanticDeprecatedSince20):
        deep = revision.copy(deep=True)
    assert shallow == revision
    assert deep == revision
    assert shallow.model_fields_set == revision.model_fields_set
    assert deep.model_fields_set == revision.model_fields_set


@pytest.mark.parametrize("field", ["bar", "ingested_at", "source", "source_record_id"])
def test_selected_bar_revision_revalidates_constructed_pollution(field: str) -> None:
    values: dict[str, object] = {
        "bar": make_bar(),
        "ingested_at": datetime(2026, 7, 25, 1, tzinfo=UTC),
        "source": "test-feed",
        "source_record_id": "revision-1",
    }
    values[field] = []
    polluted = SelectedBarRevision.model_construct(**values)

    with pytest.raises(ValidationError):
        SelectedBarRevision.model_validate(polluted)


def test_selected_bar_revision_rejects_bar_subclasses_and_constructed_bar_pollution() -> None:
    class MutableBar(Bar):
        model_config = ConfigDict(frozen=False)

    mutable_bar = MutableBar(**make_bar().model_dump())
    values = {name: getattr(make_bar(), name) for name in Bar.model_fields}
    values["close"] = []
    polluted_bar = Bar.model_construct(**values)
    metadata = {
        "ingested_at": datetime(2026, 7, 25, 1, tzinfo=UTC),
        "source": "test-feed",
        "source_record_id": "revision-1",
    }

    with pytest.raises(ValidationError):
        SelectedBarRevision(bar=mutable_bar, **metadata)
    with pytest.raises(ValidationError):
        SelectedBarRevision(bar=polluted_bar, **metadata)


def test_latest_revision_returns_selected_bar_and_metadata() -> None:
    store = PointInTimeStore()
    bar = make_bar()
    ingested_at = datetime(2026, 7, 25, 1, tzinfo=UTC)
    store.append_bar(
        bar,
        ingested_at=ingested_at,
        source="test-feed",
        source_record_id="revision-1",
    )

    assert store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    ) == SelectedBarRevision(
        bar=bar,
        ingested_at=ingested_at,
        source="test-feed",
        source_record_id="revision-1",
    )


@pytest.mark.parametrize(
    ("winner_metadata", "conflicting_metadata"),
    [
        pytest.param(
            (
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "alpha-feed",
                "record-a",
            ),
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 4, tzinfo=UTC),
                "zulu-feed",
                "record-z",
            ),
            id="available-at-before-later-ingestion-source-and-id",
        ),
        pytest.param(
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 2, tzinfo=UTC),
                "alpha-feed",
                "record-a",
            ),
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "zulu-feed",
                "record-z",
            ),
            id="ingested-at-before-larger-source-and-id",
        ),
        pytest.param(
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "zulu-feed",
                "record-a",
            ),
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "alpha-feed",
                "record-z",
            ),
            id="source-before-larger-id",
        ),
        pytest.param(
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "test-feed",
                "record-z",
            ),
            (
                datetime(2026, 7, 25, tzinfo=UTC),
                datetime(2026, 7, 25, 1, tzinfo=UTC),
                "test-feed",
                "record-a",
            ),
            id="source-record-id-final-tie-breaker",
        ),
    ],
)
def test_latest_revision_uses_complete_precedence_order(
    winner_metadata: tuple[datetime, datetime, str, str],
    conflicting_metadata: tuple[datetime, datetime, str, str],
) -> None:
    store = PointInTimeStore()
    winner_available_at, winner_ingested_at, winner_source, winner_record_id = (
        winner_metadata
    )
    (
        conflicting_available_at,
        conflicting_ingested_at,
        conflicting_source,
        conflicting_record_id,
    ) = conflicting_metadata
    winner = make_bar(close=Decimal("334.00"), available_at=winner_available_at)
    conflicting = make_bar(
        close=Decimal("333.00"), available_at=conflicting_available_at
    )
    store.append_bar(
        conflicting,
        ingested_at=conflicting_ingested_at,
        source=conflicting_source,
        source_record_id=conflicting_record_id,
    )
    store.append_bar(
        winner,
        ingested_at=winner_ingested_at,
        source=winner_source,
        source_record_id=winner_record_id,
    )
    query = {
        "market": Market.US,
        "symbol": "AAPL",
        "session_date": date(2026, 7, 24),
        "as_of": datetime(2026, 7, 26, tzinfo=UTC),
    }

    revision = store.latest_bar_revision_as_of(**query)

    assert revision == SelectedBarRevision(
        bar=winner,
        ingested_at=winner_ingested_at,
        source=winner_source,
        source_record_id=winner_record_id,
    )
    assert store.latest_bar_as_of(**query) == winner


def test_latest_revision_filters_session_date_before_ranking() -> None:
    store = PointInTimeStore()
    target = make_bar(
        close=Decimal("333.00"),
        available_at=datetime(2026, 7, 25, tzinfo=UTC),
    )
    other_session = make_bar(
        session_date=date(2026, 7, 25),
        open=Decimal("998.00"),
        high=Decimal("1000.00"),
        low=Decimal("997.00"),
        close=Decimal("999.00"),
        available_at=datetime(2026, 7, 26, tzinfo=UTC),
    )
    store.append_bar(
        target,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="alpha-feed",
        source_record_id="record-a",
    )
    store.append_bar(
        other_session,
        ingested_at=datetime(2026, 7, 26, 1, tzinfo=UTC),
        source="zulu-feed",
        source_record_id="record-z",
    )
    query = {
        "market": Market.US,
        "symbol": "AAPL",
        "session_date": date(2026, 7, 24),
        "as_of": datetime(2026, 7, 27, tzinfo=UTC),
    }

    revision = store.latest_bar_revision_as_of(**query)

    assert revision == SelectedBarRevision(
        bar=target,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="alpha-feed",
        source_record_id="record-a",
    )
    assert store.latest_bar_as_of(**query) == target


def test_latest_revision_uses_source_record_id_as_final_tie_breaker() -> None:
    store = PointInTimeStore()
    ingested_at = datetime(2026, 7, 25, 1, tzinfo=UTC)
    lower_id = make_bar(close=Decimal("333.02"))
    higher_id = make_bar(close=Decimal("334.00"))
    store.append_bar(
        higher_id,
        ingested_at=ingested_at,
        source="test-feed",
        source_record_id="record-z",
    )
    store.append_bar(
        lower_id,
        ingested_at=ingested_at,
        source="test-feed",
        source_record_id="record-a",
    )

    revision = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 26, tzinfo=UTC),
    )

    assert revision is not None
    assert revision.bar == higher_id
    assert revision.source_record_id == "record-z"


def test_latest_revision_correction_is_hidden_until_exact_availability_instant() -> None:
    store = PointInTimeStore()
    original = make_bar()
    correction = make_bar(
        close=Decimal("334.00"),
        available_at=datetime(2026, 7, 27, tzinfo=UTC),
    )
    store.append_bar(
        original,
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="v1",
    )
    store.append_bar(
        correction,
        ingested_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
        source="correction-feed",
        source_record_id="v2",
    )

    before = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 27, 7, 59, 59, 999999, tzinfo=timezone(timedelta(hours=8))),
    )
    at = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=datetime(2026, 7, 27, 8, tzinfo=timezone(timedelta(hours=8))),
    )

    assert before is not None and before.bar == original
    assert at is not None and at.bar == correction
    assert at.ingested_at == datetime(2026, 7, 27, 1, tzinfo=UTC)
    assert at.source == "correction-feed"
    assert at.source_record_id == "v2"


def test_latest_revision_none_validation_and_closed_semantics_match_wrapper() -> None:
    store = PointInTimeStore()
    query = {
        "market": Market.US,
        "symbol": "AAPL",
        "session_date": date(2026, 7, 24),
        "as_of": datetime(2026, 7, 26, tzinfo=UTC),
    }
    assert store.latest_bar_revision_as_of(**query) is None
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        store.latest_bar_revision_as_of(**(query | {"as_of": datetime(2026, 7, 26)}))

    store.close()
    with pytest.raises(RuntimeError, match="PointInTimeStore is closed"):
        store.latest_bar_revision_as_of(**query)
    with pytest.raises(RuntimeError, match="PointInTimeStore is closed"):
        store.latest_bar_as_of(**query)


def test_latest_bar_wrapper_is_byte_equal_to_selected_revision_bar() -> None:
    store = PointInTimeStore()
    store.append_bar(
        make_bar(),
        ingested_at=datetime(2026, 7, 25, 1, tzinfo=UTC),
        source="test-feed",
        source_record_id="revision-1",
    )
    query = {
        "market": Market.US,
        "symbol": " aapl ",
        "session_date": date(2026, 7, 24),
        "as_of": datetime(2026, 7, 26, 8, tzinfo=timezone(timedelta(hours=8))),
    }

    revision = store.latest_bar_revision_as_of(**query)
    wrapped = store.latest_bar_as_of(**query)

    assert revision is not None and wrapped is not None
    assert wrapped.model_dump_json().encode() == revision.bar.model_dump_json().encode()


def test_data_public_api_exports_store_and_selected_revision() -> None:
    assert data.__all__ == [
        "BarRevisionWrite",
        "BatchAppendResult",
        "PointInTimeStore",
        "SelectedBarRevision",
    ]
    assert data.SelectedBarRevision is SelectedBarRevision


def _write(
    *,
    session_date: date,
    source_record_id: str,
    close: Decimal = Decimal("100"),
) -> BarRevisionWrite:
    available_at = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        21,
        tzinfo=UTC,
    )
    return BarRevisionWrite(
        bar=Bar(
            symbol="AAPL",
            market=Market.US,
            session_date=session_date,
            open=Decimal("100"),
            high=max(Decimal("101"), close),
            low=min(Decimal("99"), close),
            close=close,
            volume=Decimal("1000"),
            available_at=available_at,
        ),
        ingested_at=available_at + timedelta(seconds=1),
        source="alpha-vantage",
        source_record_id=source_record_id,
    )


def test_latest_observed_ignores_business_availability_and_orders_by_ingestion() -> None:
    store = PointInTimeStore()
    session_date = date(2026, 7, 24)
    baseline = _write(session_date=session_date, source_record_id="baseline")
    correction = BarRevisionWrite(
        bar=make_bar(
            session_date=session_date,
            open=Decimal("100"),
            high=Decimal("111"),
            low=Decimal("99"),
            close=Decimal("111"),
            volume=Decimal("1000"),
            available_at=datetime(2026, 7, 28, tzinfo=UTC),
        ),
        ingested_at=datetime(2026, 7, 28, 1, tzinfo=UTC),
        source="alpha-vantage",
        source_record_id="correction",
    )
    store.append_bar_revisions((baseline,))
    store.append_bar_revisions((correction,))

    observed = store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=session_date,
    )
    business = store.latest_bar_revision_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=session_date,
        as_of=datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert observed is not None and observed.source_record_id == "correction"
    assert business is not None and business.source_record_id == "baseline"


def test_atomic_batch_success_and_idempotent_result_are_exact() -> None:
    store = PointInTimeStore()
    writes = tuple(
        _write(
            session_date=date(2026, 7, day),
            source_record_id=f"record-{day}",
        )
        for day in (22, 23, 24)
    )
    first = store.append_bar_revisions(writes)
    second = store.append_bar_revisions(writes)

    assert first.appended == tuple(
        (item.source, item.source_record_id) for item in writes
    )
    assert first.unchanged == ()
    assert second.appended == ()
    assert second.unchanged == tuple(
        (item.source, item.source_record_id) for item in writes
    )


@pytest.mark.parametrize("conflict_position", range(3))
def test_atomic_batch_rolls_back_after_conflict_at_every_position(
    conflict_position: int,
) -> None:
    store = PointInTimeStore()
    session_dates = tuple(date(2026, 7, day) for day in (22, 23, 24))
    conflicting = _write(
        session_date=session_dates[conflict_position],
        source_record_id="conflict",
    )
    store.append_bar_revisions((conflicting,))
    candidates = [
        _write(
            session_date=session_date,
            source_record_id=f"candidate-{index}",
        )
        for index, session_date in enumerate(session_dates)
    ]
    candidates[conflict_position] = BarRevisionWrite(
        bar=make_bar(
            symbol="AAPL",
            session_date=session_dates[conflict_position],
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("102"),
            volume=Decimal("1000"),
            available_at=datetime(
                2026, 7, session_dates[conflict_position].day, 21, tzinfo=UTC
            ),
        ),
        ingested_at=datetime(
            2026, 7, session_dates[conflict_position].day, 22, tzinfo=UTC
        ),
        source="alpha-vantage",
        source_record_id="conflict",
    )

    with pytest.raises(ValueError, match="conflicting payload"):
        store.append_bar_revisions(tuple(candidates))

    for index, session_date in enumerate(session_dates):
        selected = store.latest_bar_revision_as_of(
            market=Market.US,
            symbol="AAPL",
            session_date=session_date,
            as_of=datetime(2026, 7, 30, tzinfo=UTC),
        )
        if index == conflict_position:
            assert selected is not None
            assert selected.source_record_id == "conflict"
        else:
            assert selected is None


def test_atomic_batch_rejects_duplicate_identities_before_transaction() -> None:
    store = PointInTimeStore()
    write = _write(session_date=date(2026, 7, 24), source_record_id="duplicate")
    with pytest.raises(ValueError, match="duplicate"):
        store.append_bar_revisions((write, write))
    assert store.latest_observed_bar_revision(
        provider_id="alpha-vantage",
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
    ) is None


def test_atomic_batch_rejects_second_provider_for_existing_event() -> None:
    store = PointInTimeStore()
    original = _write(
        session_date=date(2026, 7, 24),
        source_record_id="alpha-record",
    )
    store.append_bar_revisions((original,))
    values = {
        name: getattr(original, name) for name in BarRevisionWrite.model_fields
    }
    values.update(source="other-provider", source_record_id="other-record")
    conflicting = BarRevisionWrite(**values)

    with pytest.raises(ValueError, match="conflicting provider"):
        store.append_bar_revisions((conflicting,))

    assert store.latest_observed_bar_revision(
        provider_id="other-provider",
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
    ) is None
