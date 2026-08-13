from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import AwareDatetime, StringConstraints, field_validator, model_validator

from stock_agent.audit.canonical import canonical_datetime, canonical_decimal, tagged_sha256
from stock_agent.data import PointInTimeStore, SelectedBarRevision
from stock_agent.domain import Bar, Market
from stock_agent.runtime.models import RuntimeModel
from stock_agent.strategies.protocol import MarketSnapshot

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9-]+-sha256:[0-9a-f]{64}$"),
]

_MANIFEST_SCHEMA = "runtime-snapshot-manifest/v1"


class SnapshotIncompleteError(Exception):
    """Stable failure when the warm-up coverage matrix is incomplete."""

    def __init__(self, missing: tuple[tuple[str, date], ...]) -> None:
        self.missing = missing
        super().__init__("market snapshot warm-up coverage is incomplete")


class SnapshotManifest(RuntimeModel):
    run_id: Digest
    market: Market
    as_of: AwareDatetime
    strategy_config_version: NonEmptyStr
    digest: Digest
    revisions: tuple[SelectedBarRevision, ...]

    @field_validator("revisions", mode="before")
    @classmethod
    def revisions_are_exact(cls, value: object) -> object:
        if type(value) is not tuple or not value:
            raise ValueError("manifest revisions must be a nonempty exact tuple")
        if any(type(item) is not SelectedBarRevision for item in value):
            raise ValueError("manifest revisions must contain exact SelectedBarRevision values")
        return value

    @model_validator(mode="after")
    def manifest_is_consistent(self) -> Self:
        if any(rev.bar.market is not self.market for rev in self.revisions):
            raise ValueError("manifest revisions must match the manifest market")
        if any(rev.bar.available_at > self.as_of for rev in self.revisions):
            raise ValueError("manifest revisions must not be future-visible")
        keys = tuple((rev.bar.symbol, rev.bar.session_date) for rev in self.revisions)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("manifest revisions must be symbol/session sorted and unique")
        if self.digest != canonical_manifest_digest(
            run_id=self.run_id,
            market=self.market,
            as_of=self.as_of,
            strategy_config_version=self.strategy_config_version,
            revisions=self.revisions,
        ):
            raise ValueError("manifest digest does not match canonical content")
        return self


def canonical_manifest_digest(
    *,
    run_id: str,
    market: Market,
    as_of: datetime,
    strategy_config_version: str,
    revisions: tuple[SelectedBarRevision, ...],
) -> str:
    return tagged_sha256(
        "runtime-snapshot-manifest",
        (
            _MANIFEST_SCHEMA,
            run_id,
            market.value,
            canonical_datetime(as_of),
            strategy_config_version,
            [
                [
                    rev.bar.symbol,
                    rev.bar.market.value,
                    rev.bar.session_date.isoformat(),
                    canonical_decimal(rev.bar.open),
                    canonical_decimal(rev.bar.high),
                    canonical_decimal(rev.bar.low),
                    canonical_decimal(rev.bar.close),
                    canonical_decimal(rev.bar.volume),
                    canonical_datetime(rev.bar.available_at),
                    canonical_datetime(rev.ingested_at),
                    rev.source,
                    rev.source_record_id,
                ]
                for rev in revisions
            ],
        ),
    )


def freeze_snapshot(
    *,
    pit_store: PointInTimeStore,
    run_id: str,
    market: Market,
    symbols: tuple[str, ...],
    sessions: tuple[date, ...],
    as_of: datetime,
    strategy_config_version: str,
) -> SnapshotManifest:
    if type(pit_store) is not PointInTimeStore:
        raise TypeError("pit_store must be exactly PointInTimeStore")
    if type(run_id) is not str or not run_id:
        raise ValueError("run_id must be a nonblank string")
    if type(market) is not Market:
        raise ValueError("market must be exactly Market")
    if type(symbols) is not tuple or not symbols:
        raise ValueError("symbols must be a nonempty exact tuple")
    if type(sessions) is not tuple or not sessions:
        raise ValueError("sessions must be a nonempty exact tuple")
    if any(type(item) is not str or not item for item in symbols):
        raise ValueError("symbols must be nonblank strings")
    if any(type(item) is not date for item in sessions):
        raise ValueError("sessions must be plain dates")
    if type(as_of) is not datetime or as_of.tzinfo is None:
        raise ValueError("as_of must be an aware datetime")
    if type(strategy_config_version) is not str or not strategy_config_version:
        raise ValueError("strategy_config_version must be a nonblank string")

    ordered_symbols = tuple(sorted(symbols))
    ordered_sessions = tuple(sorted(sessions))

    revisions: list[SelectedBarRevision] = []
    missing: list[tuple[str, date]] = []
    for symbol in ordered_symbols:
        for session_date in ordered_sessions:
            revision = pit_store.latest_bar_revision_as_of(
                market=market,
                symbol=symbol,
                session_date=session_date,
                as_of=as_of,
            )
            if revision is None:
                missing.append((symbol, session_date))
                continue
            if revision.bar.available_at > as_of:
                missing.append((symbol, session_date))
                continue
            revisions.append(revision)

    if missing:
        raise SnapshotIncompleteError(tuple(missing))

    digest = canonical_manifest_digest(
        run_id=run_id,
        market=market,
        as_of=as_of,
        strategy_config_version=strategy_config_version,
        revisions=tuple(revisions),
    )
    return SnapshotManifest(
        run_id=run_id,
        market=market,
        as_of=as_of,
        strategy_config_version=strategy_config_version,
        digest=digest,
        revisions=tuple(revisions),
    )


def rebuild_market_snapshot(manifest: SnapshotManifest) -> MarketSnapshot:
    if type(manifest) is not SnapshotManifest:
        raise TypeError("manifest must be exactly SnapshotManifest")
    bars = tuple(rev.bar for rev in manifest.revisions)
    return MarketSnapshot(
        as_of=manifest.as_of,
        market=manifest.market,
        bars=bars,
    )


def manifest_to_payload(manifest: SnapshotManifest) -> str:
    if type(manifest) is not SnapshotManifest:
        raise TypeError("manifest must be exactly SnapshotManifest")
    return json.dumps(
        {
            "run_id": manifest.run_id,
            "market": manifest.market.value,
            "as_of": canonical_datetime(manifest.as_of),
            "strategy_config_version": manifest.strategy_config_version,
            "digest": manifest.digest,
            "revisions": [
                {
                    "symbol": rev.bar.symbol,
                    "market": rev.bar.market.value,
                    "session_date": rev.bar.session_date.isoformat(),
                    "open": canonical_decimal(rev.bar.open),
                    "high": canonical_decimal(rev.bar.high),
                    "low": canonical_decimal(rev.bar.low),
                    "close": canonical_decimal(rev.bar.close),
                    "volume": canonical_decimal(rev.bar.volume),
                    "available_at": canonical_datetime(rev.bar.available_at),
                    "ingested_at": canonical_datetime(rev.ingested_at),
                    "source": rev.source,
                    "source_record_id": rev.source_record_id,
                }
                for rev in manifest.revisions
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def manifest_from_payload(
    payload: str,
    *,
    run_id: str,
    market: Market,
    digest: str,
) -> SnapshotManifest:
    if type(payload) is not str:
        raise ValueError("manifest payload must be text")
    try:
        raw = json.loads(payload)
        if type(raw) is not dict:
            raise ValueError("manifest payload must be an object")
        revisions = tuple(
            SelectedBarRevision(
                bar=Bar(
                    symbol=item["symbol"],
                    market=Market(item["market"]),
                    session_date=date.fromisoformat(item["session_date"]),
                    open=Decimal(item["open"]),
                    high=Decimal(item["high"]),
                    low=Decimal(item["low"]),
                    close=Decimal(item["close"]),
                    volume=Decimal(item["volume"]),
                    available_at=_decode_datetime(item["available_at"]),
                ),
                ingested_at=_decode_datetime(item["ingested_at"]),
                source=item["source"],
                source_record_id=item["source_record_id"],
            )
            for item in raw["revisions"]
        )
        manifest = SnapshotManifest(
            run_id=raw["run_id"],
            market=Market(raw["market"]),
            as_of=_decode_datetime(raw["as_of"]),
            strategy_config_version=raw["strategy_config_version"],
            digest=raw["digest"],
            revisions=revisions,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("manifest payload is invalid") from error
    if manifest.run_id != run_id or manifest.market is not market or manifest.digest != digest:
        raise ValueError("persisted manifest identity does not match its key")
    return manifest


def _decode_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("datetime must be text")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
