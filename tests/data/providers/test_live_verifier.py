from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

import scripts.verify_live_market_data as verifier
from scripts.live_market_schedules import (
    CN_SCHEDULE_ID,
    LIVE_MARKET_SCHEDULES,
    US_SCHEDULE_ID,
)
from scripts.verify_live_market_data import (
    atomic_write_evidence,
    compute_relevant_source_digest,
    evidence_is_current,
    production_provider_for,
    relevant_source_files,
    run_cli,
    validate_schedule_for_live_run,
    verify_live_schedule,
)
from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    BoundedSessionSchedule,
    FetchedDailyBar,
    MarketDataError,
    MarketDataErrorCode,
    SessionScheduleRow,
)
from stock_agent.domain import Market

SECRET_CANARY = "CAP4_SECRET_CANARY_f6107b"
_EXACT_RELEVANT_FILES = frozenset(
    {
        "scripts/live_market_schedules.py",
        "scripts/verify_live_market_data.py",
        "src/stock_agent/data/policies.py",
        "src/stock_agent/data/store.py",
    }
)


def _independent_relevant_source_files(repository_root: Path) -> tuple[str, ...]:
    discovered = set(_EXACT_RELEVANT_FILES)
    for directory in (
        repository_root / "src/stock_agent/data/providers",
        repository_root / "src/stock_agent/backtest",
    ):
        discovered.update(
            path.relative_to(repository_root).as_posix()
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return tuple(sorted(discovered))


def _assert_evidence_has_no_sensitive_names_or_text(value: object) -> None:
    forbidden = ("body", "url", "apikey", "key", "http", "canary", SECRET_CANARY.lower())
    if isinstance(value, dict):
        for key, nested in value.items():
            assert type(key) is str
            lowered = key.lower()
            assert not any(fragment in lowered for fragment in forbidden)
            _assert_evidence_has_no_sensitive_names_or_text(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_evidence_has_no_sensitive_names_or_text(nested)
    elif isinstance(value, str):
        lowered = value.lower()
        assert not any(fragment in lowered for fragment in forbidden)


@pytest.fixture(autouse=True)
def _block_network_and_real_credential_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    def forbidden_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("Capability 4 standard tests must never open sockets")

    for name in ("create_connection", "create_server", "fromfd", "socketpair", "socket"):
        monkeypatch.setattr(socket, name, forbidden_socket)

    original_environment = os.environ

    class GuardedEnvironment:
        def __getitem__(self, key: object) -> str:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not read the live credential")
            return original_environment[key]  # type: ignore[index]

        def get(self, key: object, default: object = None) -> object:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not read the live credential")
            return original_environment.get(key, default)  # type: ignore[arg-type]

        def __contains__(self, key: object) -> bool:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not inspect the live credential")
            return key in original_environment

        def __setitem__(self, key: str, value: str) -> None:
            original_environment[key] = value

        def __delitem__(self, key: str) -> None:
            del original_environment[key]

    def guarded_getenv(key: str, default: str | None = None) -> str | None:
        if key == "ALPHA_VANTAGE_API_KEY":
            raise AssertionError("standard tests must not read the live credential")
        return original_environment.get(key, default)

    monkeypatch.setattr(os, "environ", GuardedEnvironment())
    monkeypatch.setattr(os, "getenv", guarded_getenv)
    yield


def test_frozen_cn_and_us_schedules_have_exact_authoritative_sessions_and_clocks() -> None:
    cn = LIVE_MARKET_SCHEDULES[CN_SCHEDULE_ID]
    us = LIVE_MARKET_SCHEDULES[US_SCHEDULE_ID]

    assert (cn.market, cn.symbol, cn.request.start, cn.request.end) == (
        Market.CN,
        "600000",
        date(2025, 7, 1),
        date(2025, 7, 10),
    )
    assert cn.provenance_id == "sse-2025-trading-calendar-and-hours-reviewed-2026-08-04"
    assert tuple(row.session_date.day for row in cn.schedule.sessions) == (
        1,
        2,
        3,
        4,
        7,
        8,
        9,
        10,
    )
    assert all(row.timezone == "Asia/Shanghai" for row in cn.schedule.sessions)
    assert all(
        row.open_at.timetz().replace(tzinfo=None) == time(9, 30)
        for row in cn.schedule.sessions
    )
    assert all(
        row.close_at.timetz().replace(tzinfo=None) == time(15)
        for row in cn.schedule.sessions
    )
    assert {row.open_at.utcoffset() for row in cn.schedule.sessions} == {
        row.close_at.utcoffset() for row in cn.schedule.sessions
    } == {
        datetime(2025, 7, 1, tzinfo=UTC)
        .astimezone(row.open_at.tzinfo)
        .utcoffset()
        for row in cn.schedule.sessions
    }

    assert (us.market, us.symbol, us.request.start, us.request.end) == (
        Market.US,
        "IBM",
        date(2026, 7, 27),
        date(2026, 7, 31),
    )
    assert us.provenance_id == "nyse-2026-holidays-and-core-hours-reviewed-2026-08-04"
    assert tuple(row.session_date.day for row in us.schedule.sessions) == (27, 28, 29, 30, 31)
    assert all(row.timezone == "America/New_York" for row in us.schedule.sessions)
    assert all(
        row.open_at.timetz().replace(tzinfo=None) == time(9, 30)
        for row in us.schedule.sessions
    )
    assert all(
        row.close_at.timetz().replace(tzinfo=None) == time(16)
        for row in us.schedule.sessions
    )
    assert {row.open_at.utcoffset() for row in us.schedule.sessions} == {
        row.close_at.utcoffset() for row in us.schedule.sessions
    }
    us_offset = next(iter({row.open_at.utcoffset() for row in us.schedule.sessions}))
    assert us_offset is not None
    assert us_offset.total_seconds() == -4 * 3600

    for frozen in (cn, us):
        assert frozen.schedule_id in LIVE_MARKET_SCHEDULES
        assert frozen.provenance_id
        assert frozen.provenance_id == frozen.provenance_id.strip()
        assert "http://" not in frozen.provenance_id.lower()
        assert "https://" not in frozen.provenance_id.lower()
        assert frozen.request.market is frozen.schedule.market is frozen.market
        assert frozen.request.symbol == frozen.symbol
        assert frozen.request.price_mode == "RAW"
        assert len(frozen.schedule.sessions) >= 5
        assert all(row.generated_on == date(2026, 8, 4) for row in frozen.schedule.sessions)
        assert len({row.provenance for row in frozen.schedule.sessions}) == 1
        assert "https://" in frozen.schedule.sessions[0].provenance
        assert all(row.open_at.tzinfo is row.close_at.tzinfo for row in frozen.schedule.sessions)
        assert all(
            row.open_at.date() == row.close_at.date() == row.session_date
            for row in frozen.schedule.sessions
        )

    with pytest.raises(ValueError, match="provenance_id must not contain a URL"):
        replace(cn, provenance_id="https://must-not-enter-evidence.example")


def test_schedule_validation_uses_explicit_early_close_and_requires_closed_dense_minimum() -> None:
    frozen = LIVE_MARKET_SCHEDULES[US_SCHEDULE_ID]
    rows = list(frozen.schedule.sessions)
    final = rows[-1]
    rows[-1] = SessionScheduleRow(
        session_date=final.session_date,
        open_at=final.open_at,
        close_at=datetime.combine(final.session_date, time(13), final.close_at.tzinfo),
        timezone=final.timezone,
        provenance="explicit test early close; no inference",
        generated_on=final.generated_on,
    )
    early_close = BoundedSessionSchedule(
        market=frozen.market,
        start=frozen.schedule.start,
        end=frozen.schedule.end,
        sessions=tuple(rows),
    )

    validate_schedule_for_live_run(early_close, datetime(2026, 8, 4, tzinfo=UTC))

    with pytest.raises(MarketDataError) as not_closed:
        validate_schedule_for_live_run(early_close, rows[-1].close_at)
    assert not_closed.value.code is MarketDataErrorCode.SCHEDULE

    with pytest.raises(MarketDataError) as too_short:
        validate_schedule_for_live_run(
            BoundedSessionSchedule(
                market=frozen.market,
                start=rows[0].session_date,
                end=rows[3].session_date,
                sessions=tuple(rows[:4]),
            ),
            datetime(2026, 8, 4, tzinfo=UTC),
        )
    assert too_short.value.code is MarketDataErrorCode.LIVE_INCOMPLETE


class StaticProvider:
    def __init__(
        self,
        provider_id: str,
        bars: tuple[FetchedDailyBar, ...],
        *,
        secret: str = SECRET_CANARY,
    ) -> None:
        self.provider_id = provider_id
        self.bars = bars
        self.secret = secret
        self.calls = 0

    def __repr__(self) -> str:
        return f"StaticProvider(secret={self.secret!r})"

    def fetch_daily_bars(self, request: object) -> tuple[FetchedDailyBar, ...]:
        self.calls += 1
        return self.bars


def _bars(schedule_id: str) -> tuple[FetchedDailyBar, ...]:
    frozen = LIVE_MARKET_SCHEDULES[schedule_id]
    provider_id = "eastmoney" if frozen.market is Market.CN else "alpha-vantage"
    return tuple(
        FetchedDailyBar(
            market=frozen.market,
            symbol=frozen.symbol,
            session_date=row.session_date,
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=Decimal(1_000_000 + index),
            provider_id=provider_id,
            provider_native_symbol=frozen.symbol,
            provider_record_id=f"fixture-{schedule_id}-{row.session_date.isoformat()}",
        )
        for index, row in enumerate(frozen.schedule.sessions)
    )


@pytest.mark.parametrize("schedule_id", [CN_SCHEDULE_ID, US_SCHEDULE_ID])
def test_provider_to_reopened_file_store_builder_runner_is_dense_deterministic_and_rerunnable(
    tmp_path: Path,
    schedule_id: str,
) -> None:
    provider_id = "eastmoney" if schedule_id == CN_SCHEDULE_ID else "alpha-vantage"
    provider = StaticProvider(provider_id, _bars(schedule_id))
    opened_databases: list[Path] = []

    def store_factory(database: str) -> PointInTimeStore:
        opened_databases.append(Path(database))
        return PointInTimeStore(database)

    evidence_path = tmp_path / "evidence" / f"{schedule_id}.json"
    temp_parent = tmp_path / "temporary"
    temp_parent.mkdir()
    evidence = verify_live_schedule(
        schedule_id,
        provider=provider,
        clock=lambda: datetime(2026, 8, 4, 12, tzinfo=UTC),
        repository_root=Path.cwd(),
        evidence_path=evidence_path,
        git_commit="b3cd65091f44296825a0edbbaa78683b80baf1e0",
        temp_parent=temp_parent,
        store_factory=store_factory,
    )

    session_count = len(LIVE_MARKET_SCHEDULES[schedule_id].schedule.sessions)
    assert provider.calls == 1
    assert len(opened_databases) == 4
    assert opened_databases[0] == opened_databases[1]
    assert opened_databases[2] == opened_databases[3]
    assert opened_databases[0] != opened_databases[2]
    assert not any(path.exists() for path in opened_databases)
    assert list(temp_parent.iterdir()) == []
    assert evidence == json.loads(evidence_path.read_text())
    assert evidence["schema_version"] == "live-market-data-verification/v1"
    assert set(evidence) == {
        "business_summary",
        "counts",
        "coverage",
        "fingerprints",
        "git_commit",
        "manifest",
        "market",
        "policies",
        "provider_id",
        "relevant_source_digest",
        "request",
        "result",
        "run_at_utc",
        "run_id",
        "schedule",
        "schema_version",
        "source_records",
        "status",
        "symbol",
    }
    assert evidence["status"] == "success"
    assert evidence["run_at_utc"] == "2026-08-04T12:00:00Z"
    assert evidence["request"] == {
        "start": LIVE_MARKET_SCHEDULES[schedule_id].request.start.isoformat(),
        "end": LIVE_MARKET_SCHEDULES[schedule_id].request.end.isoformat(),
        "price_mode": "RAW",
    }
    assert set(evidence["schedule"]) == {
        "id",
        "provenance_id",
        "start",
        "end",
        "sessions",
    }
    assert evidence["schedule"]["provenance_id"] == (
        LIVE_MARKET_SCHEDULES[schedule_id].provenance_id
    )
    assert all(
        set(row) == {"date", "open", "close", "timezone", "generated_on"}
        for row in evidence["schedule"]["sessions"]
    )
    assert set(evidence["coverage"]) == {
        "first_session",
        "last_session",
        "session_dates",
    }
    assert evidence["coverage"]["session_dates"] == [
        row.session_date.isoformat()
        for row in LIVE_MARKET_SCHEDULES[schedule_id].schedule.sessions
    ]
    assert evidence["counts"] == {
        "requested": session_count,
        "received": session_count,
        "baseline_appended": session_count,
        "baseline_unchanged": 0,
        "rerun_appended": 0,
        "rerun_unchanged": session_count,
        "open_frame_sources": session_count,
        "selected_revisions": session_count * (session_count + 1) // 2,
    }
    assert evidence["policies"] == {
        "pit_knowledge_policy": "current-view-baseline/v1",
        "market_data_price_policy": "raw-unadjusted/no-corporate-actions/v1",
    }
    assert set(evidence["fingerprints"]) == {"resolved_data", "spec"}
    assert all(evidence["fingerprints"].values())
    source_records = evidence["source_records"]
    assert set(source_records) == {"digest", "ids", "observed_at_utc"}
    assert source_records["ids"] == sorted(set(source_records["ids"]))
    assert len(source_records["ids"]) == session_count
    source_payload = json.dumps(
        tuple(source_records["ids"]),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert source_records["digest"] == (
        "source-record-ids-sha256:" + hashlib.sha256(source_payload).hexdigest()
    )
    assert source_records["observed_at_utc"] == evidence["run_at_utc"]
    assert evidence["manifest"] == {
        "account_id": LIVE_MARKET_SCHEDULES[schedule_id].account_id,
        "market": LIVE_MARKET_SCHEDULES[schedule_id].market.value,
        "initial_cash": "1e6",
        "symbols": [LIVE_MARKET_SCHEDULES[schedule_id].symbol],
        "calendar_session_count": session_count,
        "strategy": {"id": "live-market-data-verification", "version": "v1"},
        "transaction_cost_bps": "0",
        "policy_pair": [
            "current-view-baseline/v1",
            "raw-unadjusted/no-corporate-actions/v1",
        ],
    }
    assert evidence["business_summary"] == {
        "realized_pnl": "0",
        "final_cash": "1e6",
        "final_nav": "1e6",
        "filled_trade_count": 0,
        "ledger_event_count": session_count + 1,
        "final_position_count": 0,
    }
    assert evidence["result"] == {
        "dense": True,
        "baseline_rerun": True,
        "independent_assemblies_equal": True,
        "price_basis": "raw_unadjusted",
        "corporate_actions": "not_modeled",
        "total_return_claimed": False,
    }
    _assert_evidence_has_no_sensitive_names_or_text(evidence)
    _assert_evidence_has_no_sensitive_names_or_text(json.loads(evidence_path.read_text()))


def test_failure_paths_clean_temporary_duckdb_and_classify_missing_dense_data(
    tmp_path: Path,
) -> None:
    schedule_id = US_SCHEDULE_ID
    provider = StaticProvider("alpha-vantage", _bars(schedule_id)[:-1])
    temp_parent = tmp_path / "temporary"
    temp_parent.mkdir()

    with pytest.raises(MarketDataError) as caught:
        verify_live_schedule(
            schedule_id,
            provider=provider,
            clock=lambda: datetime(2026, 8, 4, 12, tzinfo=UTC),
            repository_root=Path.cwd(),
            evidence_path=tmp_path / "must-not-exist.json",
            git_commit="b3cd650",
            temp_parent=temp_parent,
        )

    assert caught.value.code is MarketDataErrorCode.MISSING_SESSION_DATA
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert list(temp_parent.iterdir()) == []
    assert not (tmp_path / "must-not-exist.json").exists()


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("business_summary", {"mismatch": True}),
        ("manifest_summary", {"mismatch": True}),
        ("source_record_ids", ("mismatch",)),
        ("source_record_digest", "source-record-ids-sha256:" + "0" * 64),
    ],
)
def test_independent_assemblies_must_match_every_deterministic_artifact_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    replacement: object,
) -> None:
    original = verifier._build_and_run
    calls = 0

    def mismatching_build(**kwargs: object) -> object:
        nonlocal calls
        calls += 1
        completed = original(**kwargs)
        if calls == 2:
            return replace(completed, **{field_name: replacement})
        return completed

    monkeypatch.setattr(verifier, "_build_and_run", mismatching_build)
    evidence_path = tmp_path / "must-not-exist.json"

    with pytest.raises(MarketDataError) as caught:
        verify_live_schedule(
            US_SCHEDULE_ID,
            provider=StaticProvider("alpha-vantage", _bars(US_SCHEDULE_ID)),
            clock=lambda: datetime(2026, 8, 4, 12, tzinfo=UTC),
            repository_root=Path.cwd(),
            evidence_path=evidence_path,
            git_commit="b3cd650",
            temp_parent=tmp_path,
        )

    assert caught.value.code is MarketDataErrorCode.LIVE_INCOMPLETE
    assert not evidence_path.exists()
    assert tuple(tmp_path.iterdir()) == ()


def test_relevant_source_digest_has_exact_scope_and_ignores_evidence_only_changes(
    tmp_path: Path,
) -> None:
    repository_root = Path.cwd()
    expected = _independent_relevant_source_files(repository_root)
    assert relevant_source_files(repository_root) == expected
    assert expected
    assert len(expected) == len(set(expected))
    assert tuple(sorted(expected)) == expected
    assert not any("__pycache__" in Path(relative).parts for relative in expected)
    assert not any(relative.startswith(("docs/", "tests/")) for relative in expected)

    for relative in expected:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"production:{relative}\n".encode())
    assert relevant_source_files(tmp_path) == expected

    digest = compute_relevant_source_digest(tmp_path)
    evidence = {"relevant_source_digest": digest, "git_commit": "old-evidence-commit"}

    for relative in expected:
        relevant = tmp_path / relative
        original = relevant.read_bytes()
        relevant.write_bytes(original + b"changed\n")
        assert not evidence_is_current(evidence, tmp_path), relative
        relevant.write_bytes(original)
        assert evidence_is_current(evidence, tmp_path), relative

    evidence_only = tmp_path / "docs/verification/real-market-data-us.json"
    evidence_only.parent.mkdir(parents=True)
    evidence_only.write_text('{"changed":true}\n')
    assert evidence_is_current(evidence, tmp_path)

    unrelated = tmp_path / "docs/review.md"
    unrelated.write_text("changed\n")
    assert evidence_is_current(evidence, tmp_path)

    for relative in (
        "src/stock_agent/data/providers/new_provider.py",
        "src/stock_agent/backtest/new_component.py",
    ):
        added = tmp_path / relative
        added.write_text("NEW_SCOPE = True\n")
        assert not evidence_is_current(evidence, tmp_path)
        added.unlink()
        assert evidence_is_current(evidence, tmp_path)

    deleted = tmp_path / "src/stock_agent/data/providers/models.py"
    deleted_bytes = deleted.read_bytes()
    deleted.unlink()
    assert not evidence_is_current(evidence, tmp_path)
    deleted.write_bytes(deleted_bytes)
    assert evidence_is_current(evidence, tmp_path)


def test_atomic_evidence_failure_preserves_previous_file_and_removes_staging_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "evidence.json"
    target.write_text('{"previous":true}\n')

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(MarketDataError) as caught:
        atomic_write_evidence(target, {"status": "success"})

    assert caught.value.code is MarketDataErrorCode.EVIDENCE_WRITE
    assert caught.value.__cause__ is None
    assert target.read_text() == '{"previous":true}\n'
    assert tuple(tmp_path.iterdir()) == (target,)


class RaisingProviderFactory:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, schedule: object) -> object:
        raise self.error


def _capture_cli(
    argv: list[str],
    *,
    provider_factory: Callable[[object], object],
    tmp_path: Path,
) -> tuple[int, str]:
    lines: list[str] = []
    exit_code = run_cli(
        argv,
        provider_factory=provider_factory,
        clock=lambda: datetime(2026, 8, 4, 12, tzinfo=UTC),
        repository_root=Path.cwd(),
        evidence_directory=tmp_path,
        output=lines.append,
        git_commit_reader=lambda root: "b3cd650",
    )
    return exit_code, "\n".join(lines)


def test_cli_accepts_only_exact_schedule_id_and_never_echoes_argv_secret(
    tmp_path: Path,
) -> None:
    for argv in (
        [],
        ["--market", "US", "--schedule", US_SCHEDULE_ID],
        ["--schedule", SECRET_CANARY],
        ["--schedule", US_SCHEDULE_ID, "--api-key", SECRET_CANARY],
    ):
        exit_code, output = _capture_cli(
            argv,
            provider_factory=RaisingProviderFactory(AssertionError("must not compose")),
            tmp_path=tmp_path,
        )
        assert exit_code != 0
        assert json.loads(output) == {
            "code": "invalid_request",
            "metadata": {},
            "run_id": json.loads(output)["run_id"],
        }
        assert SECRET_CANARY not in output


def test_cli_classified_and_unknown_errors_have_minimal_safe_output(
    tmp_path: Path,
) -> None:
    classified = MarketDataError(
        MarketDataErrorCode.AUTH,
        metadata={"status": 401},
    )
    exit_code, output = _capture_cli(
        ["--schedule", US_SCHEDULE_ID],
        provider_factory=RaisingProviderFactory(classified),
        tmp_path=tmp_path,
    )
    payload = json.loads(output)
    assert exit_code == 1
    assert payload == {
        "code": "auth",
        "metadata": {"status": 401},
        "run_id": payload["run_id"],
    }

    unknown = RuntimeError(f"unknown {SECRET_CANARY}")
    exit_code, output = _capture_cli(
        ["--schedule", US_SCHEDULE_ID],
        provider_factory=RaisingProviderFactory(unknown),
        tmp_path=tmp_path,
    )
    payload = json.loads(output)
    assert exit_code == 1
    assert payload == {"error_type": "RuntimeError", "run_id": payload["run_id"]}
    assert SECRET_CANARY not in output
    assert "traceback" not in output.lower()


def test_cn_production_composition_does_not_touch_alpha_vantage_environment() -> None:
    provider = production_provider_for(LIVE_MARKET_SCHEDULES[CN_SCHEDULE_ID])
    assert provider.provider_id == "eastmoney"
