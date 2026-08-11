from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from stock_agent.backtest import (
    BacktestResult,
    ChronologicalBacktestRunner,
    build_real_data_backtest_spec,
)
from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    AlphaVantageDailyBarProvider,
    BoundedSessionSchedule,
    DailyBarRequest,
    EastmoneyDailyBarProvider,
    FetchedDailyBar,
    IncrementalBarIngestor,
    IngestionReport,
    MarketDataError,
    MarketDataErrorCode,
)
from stock_agent.domain import Instrument, Market
from stock_agent.execution import FillStatus
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext

if __package__:
    from .live_market_schedules import LIVE_MARKET_SCHEDULES, LiveMarketSchedule
else:
    from live_market_schedules import LIVE_MARKET_SCHEDULES, LiveMarketSchedule

_EXACT_RELEVANT_SOURCE_FILES = frozenset(
    {
        "scripts/live_market_schedules.py",
        "scripts/verify_live_market_data.py",
        "src/stock_agent/data/policies.py",
        "src/stock_agent/data/store.py",
    }
)

_SAFE_ERROR_METADATA_KEYS = frozenset({"session_date", "stage", "status"})
_SCHEMA_VERSION = "live-market-data-verification/v1"
_FORBIDDEN_EVIDENCE_TEXT = ("body", "url", "apikey", "key", "http", "canary")


@dataclass(frozen=True)
class _EmptyStrategy:
    strategy_id: str = "live-market-data-verification"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[()]:
        del context
        return ()


@dataclass(frozen=True)
class _RunArtifacts:
    spec_json: str
    result: BacktestResult
    spec_fingerprint: str
    resolved_data_fingerprint: str
    open_frame_source_count: int
    selected_revision_count: int
    pit_knowledge_policy: str
    market_data_price_policy: str
    business_summary: dict[str, object]
    manifest_summary: dict[str, object]
    source_record_ids: tuple[str, ...]
    source_record_digest: str


@dataclass(frozen=True)
class _Assembly(_RunArtifacts):
    baseline: IngestionReport
    rerun: IngestionReport | None


def _provider_id(provider: object) -> str:
    failed = False
    value: object = None
    try:
        value = provider.provider_id
    except BaseException:
        failed = True
    if failed:
        raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT) from None
    if type(value) is not str or not value or value != value.strip():
        raise MarketDataError(MarketDataErrorCode.IDENTITY) from None
    return value


class _RecordingProvider:
    __slots__ = ("_provider", "_provider_id", "bars")

    def __init__(self, provider: object) -> None:
        self._provider_id = _provider_id(provider)
        self._provider = provider
        self.bars: tuple[FetchedDailyBar, ...] | None = None

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def fetch_daily_bars(
        self,
        request: DailyBarRequest,
    ) -> tuple[FetchedDailyBar, ...]:
        if self.bars is not None:
            return self.bars
        result: tuple[FetchedDailyBar, ...] | None = None
        descriptor: tuple[MarketDataErrorCode, dict[str, str | int | bool]] | None = None
        provider = self._provider
        try:
            result = provider.fetch_daily_bars(request)
        except MarketDataError as error:
            descriptor = error.code, dict(error.metadata)
        except BaseException:
            descriptor = MarketDataErrorCode.INTERNAL_CONTRACT, {}
        self._provider = None
        provider = None
        request = None  # type: ignore[assignment]
        if descriptor is not None:
            code, metadata = descriptor
            raise MarketDataError(code, metadata=metadata) from None
        assert result is not None
        self.bars = result
        return result


class _StaticProvider:
    __slots__ = ("_bars", "_provider_id")

    def __init__(
        self,
        *,
        provider_id: str,
        bars: tuple[FetchedDailyBar, ...],
    ) -> None:
        self._provider_id = provider_id
        self._bars = bars

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def fetch_daily_bars(
        self,
        request: DailyBarRequest,
    ) -> tuple[FetchedDailyBar, ...]:
        del request
        return self._bars


def validate_schedule_for_live_run(
    schedule: BoundedSessionSchedule,
    ingested_at: datetime,
) -> None:
    if type(schedule) is not BoundedSessionSchedule:
        raise MarketDataError(MarketDataErrorCode.SCHEDULE) from None
    if type(ingested_at) is not datetime or ingested_at.utcoffset() is None:
        raise MarketDataError(MarketDataErrorCode.CLOCK) from None
    if len(schedule.sessions) < 5:
        raise MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE) from None
    current = ingested_at.astimezone(UTC)
    if any(row.close_at.astimezone(UTC) >= current for row in schedule.sessions):
        raise MarketDataError(MarketDataErrorCode.SCHEDULE) from None


def relevant_source_files(repository_root: Path) -> tuple[str, ...]:
    relative_paths = set(_EXACT_RELEVANT_SOURCE_FILES)
    for directory in (
        repository_root / "src/stock_agent/data/providers",
        repository_root / "src/stock_agent/backtest",
    ):
        relative_paths.update(
            path.relative_to(repository_root).as_posix()
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return tuple(sorted(relative_paths))


def compute_relevant_source_digest(
    repository_root: Path,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"live-market-relevant-source/v1\x00")
    failed = False
    try:
        for relative in relevant_source_files(repository_root):
            if type(relative) is not str or not relative or relative.startswith("/"):
                raise ValueError("invalid relevant source path")
            relative_bytes = relative.encode("utf-8")
            content = (repository_root / relative).read_bytes()
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except BaseException:
        failed = True
    if failed:
        raise MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE) from None
    return f"relevant-source-sha256:{digest.hexdigest()}"


def evidence_is_current(
    evidence: Mapping[str, object],
    repository_root: Path,
) -> bool:
    recorded = evidence.get("relevant_source_digest")
    if type(recorded) is not str:
        return False
    try:
        current = compute_relevant_source_digest(repository_root)
    except MarketDataError:
        return False
    return hmac.compare_digest(recorded, current)


def atomic_write_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    staging: Path | None = None
    failed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staging_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        staging = Path(staging_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        staging = None
    except BaseException:
        failed = True
    if failed:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except BaseException:
                pass
        raise MarketDataError(MarketDataErrorCode.EVIDENCE_WRITE) from None


def _require_safe_evidence_value(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str or any(
                fragment in key.lower() for fragment in _FORBIDDEN_EVIDENCE_TEXT
            ):
                raise MarketDataError(MarketDataErrorCode.EVIDENCE_WRITE) from None
            _require_safe_evidence_value(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _require_safe_evidence_value(nested)
        return
    if isinstance(value, str) and any(
        fragment in value.lower() for fragment in _FORBIDDEN_EVIDENCE_TEXT
    ):
        raise MarketDataError(MarketDataErrorCode.EVIDENCE_WRITE) from None


def _cn_session_states(
    frozen: LiveMarketSchedule,
) -> Mapping[date, tuple[CnSessionState, ...]] | None:
    if frozen.market is Market.US:
        return None
    return {
        row.session_date: (
            CnSessionState(
                symbol=frozen.symbol,
                session_date=row.session_date,
                suspended=False,
                price_limit_state=CnPriceLimitState.NONE,
            ),
        )
        for row in frozen.schedule.sessions
    }


def _canonical_decimal_summary(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("summary Decimal must be finite")
    if value.is_zero():
        return "0"
    decimal_tuple = value.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = int(decimal_tuple.exponent)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    sign = "-" if decimal_tuple.sign else ""
    return f"{sign}{''.join(str(digit) for digit in digits)}e{exponent}"


def _business_summary(result: BacktestResult) -> dict[str, object]:
    return {
        "realized_pnl": _canonical_decimal_summary(result.realized_pnl),
        "final_cash": _canonical_decimal_summary(result.final_snapshot.cash),
        "final_nav": _canonical_decimal_summary(result.final_snapshot.nav),
        "filled_trade_count": sum(
            item.status is FillStatus.FILLED
            for session in result.sessions
            for item in session.execution_results
        ),
        "ledger_event_count": len(result.ledger_events),
        "final_position_count": len(result.final_snapshot.positions),
    }


def _manifest_summary(result: BacktestResult) -> dict[str, object]:
    manifest = result.manifest
    return {
        "account_id": manifest.account_id,
        "market": manifest.market.value,
        "initial_cash": _canonical_decimal_summary(manifest.initial_cash),
        "symbols": [item.symbol for item in manifest.instruments],
        "calendar_session_count": len(manifest.calendar_sessions),
        "strategy": {
            "id": manifest.strategy_id,
            "version": manifest.strategy_config_version,
        },
        "transaction_cost_bps": _canonical_decimal_summary(
            manifest.transaction_cost_bps
        ),
        "policy_pair": [
            manifest.pit_knowledge_policy,
            manifest.market_data_price_policy,
        ],
    }


def _source_record_summary(result: BacktestResult) -> tuple[tuple[str, ...], str]:
    source_record_ids = tuple(
        sorted(
            {
                revision.source_record_id
                for session in result.sessions
                for revision in session.selected_revisions
            }
        )
    )
    payload = json.dumps(
        source_record_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = f"source-record-ids-sha256:{hashlib.sha256(payload).hexdigest()}"
    return source_record_ids, digest


def _build_and_run(
    *,
    store: PointInTimeStore,
    frozen: LiveMarketSchedule,
    run_id: str,
    runner_factory: Callable[..., ChronologicalBacktestRunner],
) -> _RunArtifacts:
    build_error: MarketDataErrorCode | None = None
    spec = None
    try:
        spec = build_real_data_backtest_spec(
            store=store,
            schedule=frozen.schedule,
            run_id=run_id,
            account_id=frozen.account_id,
            instruments=(
                Instrument(
                    symbol=frozen.symbol,
                    market=frozen.market,
                    currency=frozen.currency,
                    sector=frozen.sector,
                ),
            ),
            initial_cash=Decimal("1000000"),
            strategy_config_version="v1",
            cn_session_states_by_date=_cn_session_states(frozen),
        )
    except ValueError as error:
        build_error = (
            MarketDataErrorCode.MISSING_SESSION_DATA
            if str(error).startswith("missing session data")
            else MarketDataErrorCode.LIVE_INCOMPLETE
        )
    if build_error is not None:
        raise MarketDataError(build_error) from None
    assert spec is not None
    runner_error: MarketDataError | None = None
    result = None
    try:
        runner = runner_factory(
            store=store,
            calendar=TradingCalendar(
                frozen.market,
                tuple(row.session_date for row in frozen.schedule.sessions),
            ),
            strategy=_EmptyStrategy(),
        )
        result = runner.run(spec)
    except MarketDataError as error:
        runner_error = MarketDataError(error.code, metadata=error.metadata)
    except BaseException:
        runner_error = MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE)
    if runner_error is not None:
        raise runner_error from None
    assert result is not None
    source_record_ids, source_record_digest = _source_record_summary(result)
    return _RunArtifacts(
        spec_json=spec.model_dump_json(),
        result=result,
        spec_fingerprint=result.spec_fingerprint,
        resolved_data_fingerprint=result.resolved_data_fingerprint,
        open_frame_source_count=sum(len(item.open_frame_sources) for item in spec.sessions),
        selected_revision_count=sum(len(item.selected_revisions) for item in result.sessions),
        pit_knowledge_policy=result.manifest.pit_knowledge_policy,
        market_data_price_policy=result.manifest.market_data_price_policy,
        business_summary=_business_summary(result),
        manifest_summary=_manifest_summary(result),
        source_record_ids=source_record_ids,
        source_record_digest=source_record_digest,
    )


def _assemble(
    *,
    database: Path,
    provider: object,
    frozen: LiveMarketSchedule,
    ingested_at: datetime,
    run_id: str,
    rerun: bool,
    store_factory: Callable[[str], PointInTimeStore],
    runner_factory: Callable[..., ChronologicalBacktestRunner],
) -> _Assembly:
    store = store_factory(str(database))
    try:
        baseline = IncrementalBarIngestor(provider=provider, store=store).ingest(
            request=frozen.request,
            schedule=frozen.schedule,
            ingested_at=ingested_at,
        )
    finally:
        store.close()

    store = store_factory(str(database))
    try:
        rerun_report = None
        if rerun:
            rerun_report = IncrementalBarIngestor(provider=provider, store=store).ingest(
                request=frozen.request,
                schedule=frozen.schedule,
                ingested_at=ingested_at,
            )
        completed = _build_and_run(
            store=store,
            frozen=frozen,
            run_id=run_id,
            runner_factory=runner_factory,
        )
    finally:
        store.close()
    return _Assembly(
        baseline=baseline,
        rerun=rerun_report,
        spec_json=completed.spec_json,
        result=completed.result,
        spec_fingerprint=completed.spec_fingerprint,
        resolved_data_fingerprint=completed.resolved_data_fingerprint,
        open_frame_source_count=completed.open_frame_source_count,
        selected_revision_count=completed.selected_revision_count,
        pit_knowledge_policy=completed.pit_knowledge_policy,
        market_data_price_policy=completed.market_data_price_policy,
        business_summary=completed.business_summary,
        manifest_summary=completed.manifest_summary,
        source_record_ids=completed.source_record_ids,
        source_record_digest=completed.source_record_digest,
    )


def _require_complete_assemblies(
    first: _Assembly,
    second: _Assembly,
    expected_sessions: int,
) -> None:
    rerun = first.rerun
    complete = (
        first.baseline.requested == expected_sessions
        and first.baseline.received == expected_sessions
        and first.baseline.appended == expected_sessions
        and first.baseline.unchanged == 0
        and rerun is not None
        and rerun.requested == expected_sessions
        and rerun.received == expected_sessions
        and rerun.appended == 0
        and rerun.unchanged == expected_sessions
        and second.baseline.requested == expected_sessions
        and second.baseline.received == expected_sessions
        and second.baseline.appended == expected_sessions
        and second.baseline.unchanged == 0
        and first.spec_json == second.spec_json
        and first.result == second.result
        and first.spec_fingerprint == second.spec_fingerprint
        and first.resolved_data_fingerprint == second.resolved_data_fingerprint
        and first.open_frame_source_count == second.open_frame_source_count
        and first.selected_revision_count == second.selected_revision_count
        and first.pit_knowledge_policy == second.pit_knowledge_policy
        and first.market_data_price_policy == second.market_data_price_policy
        and first.business_summary == second.business_summary
        and first.manifest_summary == second.manifest_summary
        and first.source_record_ids == second.source_record_ids
        and first.source_record_digest == second.source_record_digest
        and bool(first.source_record_ids)
    )
    if not complete:
        raise MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE) from None


def _schedule_evidence(frozen: LiveMarketSchedule) -> dict[str, object]:
    rows = frozen.schedule.sessions
    return {
        "id": frozen.schedule_id,
        "provenance_id": frozen.provenance_id,
        "start": frozen.schedule.start.isoformat(),
        "end": frozen.schedule.end.isoformat(),
        "sessions": [
            {
                "date": row.session_date.isoformat(),
                "open": row.open_at.isoformat(),
                "close": row.close_at.isoformat(),
                "timezone": row.timezone,
                "generated_on": row.generated_on.isoformat(),
            }
            for row in rows
        ],
    }


def _verify_live_schedule(
    schedule_id: str,
    *,
    provider: object,
    clock: Callable[[], datetime],
    repository_root: Path,
    evidence_path: Path,
    git_commit: str,
    temp_parent: Path | None = None,
    store_factory: Callable[[str], PointInTimeStore] = PointInTimeStore,
    runner_factory: Callable[..., ChronologicalBacktestRunner] = ChronologicalBacktestRunner,
) -> dict[str, object]:
    invalid_schedule = False
    frozen: LiveMarketSchedule | None = None
    try:
        frozen = LIVE_MARKET_SCHEDULES[schedule_id]
    except (KeyError, TypeError):
        invalid_schedule = True
    if invalid_schedule:
        raise MarketDataError(MarketDataErrorCode.INVALID_REQUEST) from None
    assert frozen is not None
    ingested_at = clock()
    validate_schedule_for_live_run(frozen.schedule, ingested_at)
    run_id = f"live-market-{uuid4().hex}"
    recording = _RecordingProvider(provider)
    provider = None
    provider_id = recording.provider_id

    with tempfile.TemporaryDirectory(
        prefix="live-market-verification-",
        dir=temp_parent,
    ) as temporary:
        temporary_path = Path(temporary)
        first = _assemble(
            database=temporary_path / "first.duckdb",
            provider=recording,
            frozen=frozen,
            ingested_at=ingested_at,
            run_id=run_id,
            rerun=True,
            store_factory=store_factory,
            runner_factory=runner_factory,
        )
        bars = recording.bars
        if bars is None:
            raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT) from None
        normalized = _StaticProvider(provider_id=provider_id, bars=bars)
        second = _assemble(
            database=temporary_path / "second.duckdb",
            provider=normalized,
            frozen=frozen,
            ingested_at=ingested_at,
            run_id=run_id,
            rerun=False,
            store_factory=store_factory,
            runner_factory=runner_factory,
        )
        _require_complete_assemblies(first, second, len(frozen.schedule.sessions))

    run_at_utc = ingested_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    session_dates = [row.session_date.isoformat() for row in frozen.schedule.sessions]
    rerun_report = first.rerun
    assert rerun_report is not None
    evidence: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "status": "success",
        "run_id": run_id,
        "run_at_utc": run_at_utc,
        "git_commit": git_commit,
        "relevant_source_digest": compute_relevant_source_digest(repository_root),
        "market": frozen.market.value,
        "symbol": frozen.symbol,
        "request": {
            "start": frozen.request.start.isoformat(),
            "end": frozen.request.end.isoformat(),
            "price_mode": frozen.request.price_mode,
        },
        "schedule": _schedule_evidence(frozen),
        "provider_id": provider_id,
        "coverage": {
            "first_session": session_dates[0],
            "last_session": session_dates[-1],
            "session_dates": session_dates,
        },
        "counts": {
            "requested": first.baseline.requested,
            "received": first.baseline.received,
            "baseline_appended": first.baseline.appended,
            "baseline_unchanged": first.baseline.unchanged,
            "rerun_appended": rerun_report.appended,
            "rerun_unchanged": rerun_report.unchanged,
            "open_frame_sources": first.open_frame_source_count,
            "selected_revisions": first.selected_revision_count,
        },
        "policies": {
            "pit_knowledge_policy": first.pit_knowledge_policy,
            "market_data_price_policy": first.market_data_price_policy,
        },
        "fingerprints": {
            "spec": first.spec_fingerprint,
            "resolved_data": first.resolved_data_fingerprint,
        },
        "source_records": {
            "ids": list(first.source_record_ids),
            "digest": first.source_record_digest,
            "observed_at_utc": run_at_utc,
        },
        "manifest": first.manifest_summary,
        "business_summary": first.business_summary,
        "result": {
            "dense": True,
            "baseline_rerun": True,
            "independent_assemblies_equal": True,
            "price_basis": "raw_unadjusted",
            "corporate_actions": "not_modeled",
            "total_return_claimed": False,
        },
    }
    _require_safe_evidence_value(evidence)
    atomic_write_evidence(evidence_path, evidence)
    return evidence


def verify_live_schedule(
    schedule_id: str,
    *,
    provider: object,
    clock: Callable[[], datetime],
    repository_root: Path,
    evidence_path: Path,
    git_commit: str,
    temp_parent: Path | None = None,
    store_factory: Callable[[str], PointInTimeStore] = PointInTimeStore,
    runner_factory: Callable[..., ChronologicalBacktestRunner] = ChronologicalBacktestRunner,
) -> dict[str, object]:
    evidence: dict[str, object] | None = None
    descriptor: tuple[MarketDataErrorCode, dict[str, str | int | bool]] | None = None
    try:
        evidence = _verify_live_schedule(
            schedule_id,
            provider=provider,
            clock=clock,
            repository_root=repository_root,
            evidence_path=evidence_path,
            git_commit=git_commit,
            temp_parent=temp_parent,
            store_factory=store_factory,
            runner_factory=runner_factory,
        )
    except MarketDataError as error:
        descriptor = error.code, dict(error.metadata)
    provider = None
    clock = None  # type: ignore[assignment]
    store_factory = None  # type: ignore[assignment]
    runner_factory = None  # type: ignore[assignment]
    if descriptor is not None:
        code, metadata = descriptor
        raise MarketDataError(code, metadata=metadata) from None
    assert evidence is not None
    return evidence


def production_provider_for(frozen: LiveMarketSchedule) -> object:
    if frozen.market is Market.CN:
        return EastmoneyDailyBarProvider()
    if frozen.market is not Market.US:
        raise MarketDataError(MarketDataErrorCode.INVALID_REQUEST) from None
    missing_key = False
    api_key: str | None = None
    try:
        api_key = os.environ["ALPHA_VANTAGE_API_KEY"]
    except KeyError:
        missing_key = True
    if missing_key:
        raise MarketDataError(MarketDataErrorCode.AUTH) from None
    assert api_key is not None
    descriptor: tuple[MarketDataErrorCode, dict[str, str | int | bool]] | None = None
    provider: object | None = None
    try:
        provider = AlphaVantageDailyBarProvider(api_key=api_key)
    except MarketDataError as error:
        descriptor = error.code, dict(error.metadata)
    except BaseException:
        descriptor = MarketDataErrorCode.INTERNAL_CONTRACT, {}
    api_key = None
    if descriptor is not None:
        code, metadata = descriptor
        raise MarketDataError(code, metadata=metadata) from None
    assert provider is not None
    return provider


def _read_git_commit(repository_root: Path) -> str:
    failed = False
    commit = ""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        commit = completed.stdout.strip()
    except BaseException:
        failed = True
    if failed:
        raise MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE) from None
    if not commit:
        raise MarketDataError(MarketDataErrorCode.LIVE_INCOMPLETE) from None
    return commit


def _safe_error_payload(error: MarketDataError, run_id: str) -> dict[str, object]:
    metadata = {
        key: value
        for key, value in error.metadata.items()
        if key in _SAFE_ERROR_METADATA_KEYS
    }
    return {"code": error.code.value, "metadata": metadata, "run_id": run_id}


def run_cli(
    argv: list[str],
    *,
    provider_factory: Callable[[LiveMarketSchedule], object] = production_provider_for,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    repository_root: Path | None = None,
    evidence_directory: Path | None = None,
    output: Callable[[str], Any] = print,
    git_commit_reader: Callable[[Path], str] = _read_git_commit,
) -> int:
    run_id = f"live-market-{uuid4().hex}"
    if (
        len(argv) != 2
        or argv[0] != "--schedule"
        or argv[1] not in LIVE_MARKET_SCHEDULES
    ):
        output(
            json.dumps(
                {
                    "code": MarketDataErrorCode.INVALID_REQUEST.value,
                    "metadata": {},
                    "run_id": run_id,
                },
                sort_keys=True,
            )
        )
        return 2
    frozen = LIVE_MARKET_SCHEDULES[argv[1]]
    root = Path(__file__).resolve().parents[1] if repository_root is None else repository_root
    evidence_root = root / "docs/verification" if evidence_directory is None else evidence_directory
    provider: object | None = None
    try:
        provider = provider_factory(frozen)
        commit = git_commit_reader(root)
        evidence = verify_live_schedule(
            frozen.schedule_id,
            provider=provider,
            clock=clock,
            repository_root=root,
            evidence_path=evidence_root / frozen.evidence_filename,
            git_commit=commit,
        )
        provider = None
        output(json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except MarketDataError as error:
        provider = None
        payload = _safe_error_payload(error, run_id)
        output(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 1
    except BaseException as error:
        provider = None
        payload = {"error_type": type(error).__name__, "run_id": run_id}
        output(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 1


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
