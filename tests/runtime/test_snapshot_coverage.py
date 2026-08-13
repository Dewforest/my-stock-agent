from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Market
from stock_agent.runtime.snapshots import (
    SnapshotIncompleteError,
    SnapshotManifest,
    canonical_manifest_digest,
    freeze_snapshot,
    manifest_from_payload,
    manifest_to_payload,
    rebuild_market_snapshot,
)
from stock_agent.runtime.store import RuntimeStore, StoreError

US_SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "XOM", "JNJ", "PG")
CN_SYMBOLS = (
    "600519",
    "601318",
    "600036",
    "600276",
    "600900",
    "601088",
    "600030",
    "000333",
    "000858",
    "300750",
)
SESSIONS = tuple(date(2026, 8, day) for day in (7, 10, 11, 12, 13))
AS_OF = datetime(2026, 8, 13, 22, 0, tzinfo=UTC)
RUN_ID = "paper-run-sha256:" + "a" * 64
CONFIG_VERSION = "strategy-a-v1"


def bar_for(symbol: str, session_date: date, market: Market = Market.US) -> Bar:
    available = datetime.combine(session_date, time(21, 0), tzinfo=UTC)
    return Bar(
        symbol=symbol,
        market=market,
        session_date=session_date,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("100"),
        available_at=available,
    )


def make_pit(
    symbols: tuple[str, ...] = US_SYMBOLS,
    sessions: tuple[date, ...] = SESSIONS,
    market: Market = Market.US,
) -> PointInTimeStore:
    pit = PointInTimeStore(":memory:")
    for symbol in symbols:
        for session_date in sessions:
            bar = bar_for(symbol, session_date, market)
            pit.append_bar(
                bar,
                ingested_at=bar.available_at + timedelta(hours=1),
                source="alpha-vantage-daily/v1" if market is Market.US else "eastmoney-daily/v1",
                source_record_id=f"rec-{symbol}-{session_date.isoformat()}",
            )
    return pit


def freeze(
    pit: PointInTimeStore,
    *,
    symbols: tuple[str, ...] = US_SYMBOLS,
    sessions: tuple[date, ...] = SESSIONS,
    market: Market = Market.US,
) -> SnapshotManifest:
    return freeze_snapshot(
        pit_store=pit,
        run_id=RUN_ID,
        market=market,
        symbols=symbols,
        sessions=sessions,
        as_of=AS_OF,
        strategy_config_version=CONFIG_VERSION,
    )


# ── 1. 10/10 freezes one canonical manifest ────────────────────────────────


def test_full_coverage_freezes_canonical_manifest() -> None:
    manifest = freeze(make_pit())
    assert manifest.run_id == RUN_ID
    assert manifest.market is Market.US
    assert len(manifest.revisions) == len(US_SYMBOLS) * len(SESSIONS)
    assert manifest.digest == canonical_manifest_digest(
        run_id=manifest.run_id,
        market=manifest.market,
        as_of=manifest.as_of,
        strategy_config_version=manifest.strategy_config_version,
        revisions=manifest.revisions,
    )


# ── 2. 9/10 is DATA_INCOMPLETE with zero LLM/Keychain calls ────────────────


def test_nine_of_ten_is_incomplete() -> None:
    pit = make_pit(symbols=US_SYMBOLS[:-1])  # 9 symbols
    with pytest.raises(SnapshotIncompleteError) as exc_info:
        freeze(pit)
    assert len(exc_info.value.missing) == len(SESSIONS)


# ── 3. missing current close halts progression ─────────────────────────────


def test_missing_current_close_halts() -> None:
    # One symbol lacks the most recent session's close.
    symbols_without_latest = tuple(s for s in US_SYMBOLS if s != "AAPL")
    pit2 = PointInTimeStore(":memory:")
    for symbol in symbols_without_latest:
        for session_date in SESSIONS:
            bar = bar_for(symbol, session_date)
            pit2.append_bar(
                bar,
                ingested_at=bar.available_at + timedelta(hours=1),
                source="alpha-vantage-daily/v1",
                source_record_id=f"rec-{symbol}-{session_date.isoformat()}",
            )
    # AAPL has only the first 4 sessions (no 2026-08-13 close).
    for session_date in SESSIONS[:-1]:
        bar = bar_for("AAPL", session_date)
        pit2.append_bar(
            bar,
            ingested_at=bar.available_at + timedelta(hours=1),
            source="alpha-vantage-daily/v1",
            source_record_id=f"rec-AAPL-{session_date.isoformat()}",
        )
    with pytest.raises(SnapshotIncompleteError) as exc_info:
        freeze(pit2)
    assert any(symbol == "AAPL" for symbol, _ in exc_info.value.missing)


# ── 4. reordered equivalent revisions produce same digest ──────────────────


def test_reordered_inputs_produce_same_digest() -> None:
    pit = make_pit()
    forward = freeze(pit, symbols=US_SYMBOLS, sessions=SESSIONS)
    reversed_symbols = tuple(reversed(US_SYMBOLS))
    reversed_sessions = tuple(reversed(SESSIONS))
    backward = freeze(pit, symbols=reversed_symbols, sessions=reversed_sessions)
    assert forward.digest == backward.digest
    assert forward.revisions == backward.revisions


# ── 5. content/provenance change under frozen run conflicts ────────────────


def test_manifest_content_change_conflicts(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    manifest = freeze(make_pit())
    store.store_snapshot_manifest(
        run_id=RUN_ID,
        market=Market.US,
        digest=manifest.digest,
        payload=manifest_to_payload(manifest),
    )
    # A second freeze over different content yields a different digest.
    pit2 = PointInTimeStore(":memory:")
    for symbol in US_SYMBOLS:
        for session_date in SESSIONS:
            bar = Bar(
                symbol=symbol,
                market=Market.US,
                session_date=session_date,
                open=Decimal("20"),
                high=Decimal("21"),
                low=Decimal("19"),
                close=Decimal("20.5"),
                volume=Decimal("200"),
                available_at=datetime.combine(session_date, time(21, 0), tzinfo=UTC),
            )
            pit2.append_bar(
                bar,
                ingested_at=bar.available_at + timedelta(hours=1),
                source="alpha-vantage-daily/v1",
                source_record_id=f"rec2-{symbol}-{session_date.isoformat()}",
            )
    other = freeze(pit2)
    assert other.digest != manifest.digest
    with pytest.raises(StoreError):
        store.store_snapshot_manifest(
            run_id=RUN_ID,
            market=Market.US,
            digest=other.digest,
            payload=manifest_to_payload(other),
        )


# ── 6. future-visible revision is rejected ─────────────────────────────────


def test_future_visible_revision_is_rejected() -> None:
    pit = PointInTimeStore(":memory:")
    for symbol in US_SYMBOLS:
        for session_date in SESSIONS:
            bar = bar_for(symbol, session_date)
            pit.append_bar(
                bar,
                ingested_at=bar.available_at + timedelta(hours=1),
                source="alpha-vantage-daily/v1",
                source_record_id=f"rec-{symbol}-{session_date.isoformat()}",
            )
    # Add a future-visible revision for a future session beyond as_of.
    future = date(2026, 8, 14)
    fut_bar = bar_for("AAPL", future)
    pit.append_bar(
        fut_bar,
        ingested_at=fut_bar.available_at + timedelta(hours=1),
        source="alpha-vantage-daily/v1",
        source_record_id="rec-AAPL-future",
    )
    with pytest.raises(SnapshotIncompleteError):
        freeze(pit, sessions=(*SESSIONS, future))


# ── 7. frozen manifest survives restart and rebuilds byte-identical ────────


def test_manifest_survives_restart_and_rebuilds(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    manifest = freeze(make_pit())
    store.store_snapshot_manifest(
        run_id=RUN_ID,
        market=Market.US,
        digest=manifest.digest,
        payload=manifest_to_payload(manifest),
    )
    store.close()

    reopened = RuntimeStore(path)
    loaded = reopened.load_snapshot_manifest(RUN_ID)
    assert loaded is not None
    market_str, digest, payload = loaded
    assert market_str == "US"
    assert digest == manifest.digest
    rebuilt_manifest = manifest_from_payload(
        payload, run_id=RUN_ID, market=Market.US, digest=digest
    )
    snapshot = rebuild_market_snapshot(rebuilt_manifest)
    assert snapshot == rebuild_market_snapshot(manifest)


# ── 8. CN and US manifests cannot be mixed ─────────────────────────────────


def test_mixed_market_manifest_is_rejected() -> None:
    pit = PointInTimeStore(":memory:")
    for symbol in US_SYMBOLS:
        for session_date in SESSIONS:
            bar = bar_for(symbol, session_date, Market.US)
            pit.append_bar(
                bar,
                ingested_at=bar.available_at + timedelta(hours=1),
                source="alpha-vantage-daily/v1",
                source_record_id=f"rec-{symbol}-{session_date.isoformat()}",
            )
    # Inject a CN-market bar into the US store.
    cn_bar = bar_for("600519", SESSIONS[0], Market.CN)
    pit.append_bar(
        cn_bar,
        ingested_at=cn_bar.available_at + timedelta(hours=1),
        source="eastmoney-daily/v1",
        source_record_id="rec-cn",
    )
    with pytest.raises(SnapshotIncompleteError):
        freeze(pit, symbols=(*US_SYMBOLS, "600519"))
