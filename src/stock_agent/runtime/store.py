from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints

from stock_agent.audit.canonical import canonical_datetime, tagged_sha256
from stock_agent.domain import Market
from stock_agent.runtime.identities import (
    RunIdentity,
    run_config_digest_for,
    run_id_for,
    run_key_for,
)
from stock_agent.runtime.models import RuntimeModel
from stock_agent.runtime.state import (
    AttemptKind,
    AttemptPhase,
    KillSwitch,
    KillSwitchScope,
    RunPhase,
    RuntimeAttempt,
    RuntimeRun,
    is_legal_transition,
    is_terminal_phase,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StoreError(Exception):
    """Stable, secret-free failure of the runtime coordination store."""


class StoreClosedError(StoreError):
    """Raised when an operation targets a closed store."""


class RunConflictError(StoreError):
    """Raised when the same run key reappears with a different config digest."""


class LeaseHeldError(StoreError):
    """Raised when a live, unexpired lease blocks a second worker."""


class ClaimOutcome(StrEnum):
    CREATED = "CREATED"
    RESUMED = "RESUMED"
    RECOVERED = "RECOVERED"


class ClaimResult(RuntimeModel):
    outcome: ClaimOutcome
    run: RuntimeRun
    attempt: RuntimeAttempt | None = None


def attempt_id_for(run_id: str, attempt_number: int) -> str:
    return tagged_sha256("paper-run-attempt", (run_id, attempt_number))


class RuntimeStore:
    """Durable SQLite coordination authority for runtime runs, attempts, leases,
    and kill switches. SQLite runs in WAL mode with foreign keys and a busy
    timeout; files and parent directories are user-only."""

    def __init__(self, path: str | Path) -> None:
        self._closed = False
        self._path = str(path)
        self._memory = self._path == ":memory:"
        if not self._memory:
            parent = Path(self._path).resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        try:
            self._connection = sqlite3.connect(self._path, timeout=5.0)
            self._connection.isolation_level = None
            if not self._memory:
                try:
                    # Chmod before enabling WAL so -wal/-shm sidecar files
                    # inherit user-only permissions.
                    os.chmod(self._path, 0o600)
                except OSError:
                    pass
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._initialize_schema()
        except sqlite3.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise StoreError("runtime store initialization failed") from None

    def _initialize_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                run_key TEXT NOT NULL UNIQUE,
                config_digest TEXT NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                attempt_number INTEGER NOT NULL,
                kind TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                UNIQUE (run_id, attempt_number)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                attempt_id TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS kill_switches (
                scope TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                set_by TEXT NOT NULL,
                set_at TEXT NOT NULL,
                PRIMARY KEY (scope, scope_value)
            )
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("runtime store is closed")

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._connection.close()
        except sqlite3.Error:
            raise StoreError("runtime store close failed") from None
        finally:
            self._closed = True

    # ── claim / recovery ────────────────────────────────────────────────────

    def claim_run(
        self,
        identity: RunIdentity,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimResult:
        self._ensure_open()
        if type(identity) is not RunIdentity:
            raise StoreError("claim requires an exact RunIdentity")
        if type(now) is not datetime or now.tzinfo is None:
            raise StoreError("claim requires an aware UTC datetime")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise StoreError("lease_seconds must be a positive integer")

        run_id = run_id_for(identity)
        run_key = run_key_for(identity)
        config_digest = run_config_digest_for(identity)
        expires_at = now + timedelta(seconds=lease_seconds)

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._fetch_run_by_key(run_key)
            if existing is not None:
                if existing.config_digest != config_digest:
                    raise RunConflictError(
                        "same run key reappeared with a different config digest"
                    )
                if existing.is_terminal:
                    self._connection.execute("COMMIT")
                    return ClaimResult(
                        outcome=ClaimOutcome.RESUMED, run=existing, attempt=None
                    )

                lease = self._fetch_lease(existing.run_id)
                if lease is not None and lease[0] > now:
                    raise LeaseHeldError("a live lease already owns this run")

                attempt_number = self._next_attempt_number(existing.run_id)
                attempt = self._insert_attempt(
                    existing.run_id, attempt_number, AttemptKind.RECOVERY, now
                )
                self._upsert_lease(existing.run_id, attempt.attempt_id, expires_at)
                self._connection.execute("COMMIT")
                return ClaimResult(
                    outcome=ClaimOutcome.RECOVERED, run=existing, attempt=attempt
                )

            run = RuntimeRun(
                run_id=run_id,
                run_key=run_key,
                config_digest=config_digest,
                phase=RunPhase.CLAIMED,
                created_at=now,
                updated_at=now,
            )
            self._insert_run(run)
            attempt = self._insert_attempt(run_id, 1, AttemptKind.PRIMARY, now)
            self._upsert_lease(run_id, attempt.attempt_id, expires_at)
            self._connection.execute("COMMIT")
            return ClaimResult(outcome=ClaimOutcome.CREATED, run=run, attempt=attempt)
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def transition_run(self, run_id: str, to_phase: RunPhase) -> None:
        self._ensure_open()
        if type(run_id) is not str or not run_id:
            raise StoreError("transition requires a nonblank run id")
        if type(to_phase) is not RunPhase:
            raise StoreError("transition requires an exact RunPhase")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            run = self._fetch_run_by_id(run_id)
            if run is None:
                raise StoreError("transition targets an unknown run")
            if run.is_terminal:
                raise StoreError("terminal run cannot be mutated")
            if not is_legal_transition(run.phase, to_phase):
                raise StoreError(
                    f"illegal transition {run.phase.value} -> {to_phase.value}"
                )
            updated_at = datetime.now(UTC)
            self._connection.execute(
                "UPDATE runs SET phase = ?, updated_at = ? WHERE run_id = ?",
                [to_phase.value, canonical_datetime(updated_at), run_id],
            )
            if is_terminal_phase(to_phase):
                self._connection.execute("DELETE FROM leases WHERE run_id = ?", [run_id])
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    # ── queries ─────────────────────────────────────────────────────────────

    def list_runs(self) -> tuple[RuntimeRun, ...]:
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT run_id, run_key, config_digest, phase, created_at, updated_at "
                "FROM runs ORDER BY created_at, run_id"
            ).fetchall()
        except sqlite3.Error:
            raise StoreError("runtime store run read failed") from None
        return tuple(self._decode_run(row) for row in rows)

    def list_attempts(self) -> tuple[RuntimeAttempt, ...]:
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT attempt_id, run_id, attempt_number, kind, phase, "
                "started_at, ended_at FROM run_attempts ORDER BY run_id, attempt_number"
            ).fetchall()
        except sqlite3.Error:
            raise StoreError("runtime store attempt read failed") from None
        return tuple(self._decode_attempt(row) for row in rows)

    # ── kill switches ───────────────────────────────────────────────────────

    def set_kill_switch(
        self,
        scope: KillSwitchScope,
        scope_value: str,
        *,
        set_by: str,
        now: datetime,
    ) -> None:
        self._ensure_open()
        switch = KillSwitch(
            scope=scope,
            scope_value=scope_value,
            enabled=True,
            set_by=set_by,
            set_at=now,
        )
        switch.validate_scope_value()
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO kill_switches "
                "(scope, scope_value, enabled, set_by, set_at) VALUES (?, ?, 1, ?, ?)",
                [
                    scope.value,
                    scope_value,
                    set_by,
                    canonical_datetime(now),
                ],
            )
        except sqlite3.Error:
            raise StoreError("kill switch update failed") from None

    def clear_kill_switch(self, scope: KillSwitchScope, scope_value: str) -> None:
        self._ensure_open()
        if type(scope) is not KillSwitchScope or type(scope_value) is not str:
            raise StoreError("clear_kill_switch requires an exact scope and value")
        try:
            self._connection.execute(
                "DELETE FROM kill_switches WHERE scope = ? AND scope_value = ?",
                [scope.value, scope_value],
            )
        except sqlite3.Error:
            raise StoreError("kill switch clear failed") from None

    def is_blocked(self, market: Market, account_id: str) -> bool:
        self._ensure_open()
        if type(market) is not Market or type(account_id) is not str or not account_id:
            raise StoreError("is_blocked requires an exact market and account")
        try:
            rows = self._connection.execute(
                "SELECT enabled FROM kill_switches WHERE "
                "(scope = 'GLOBAL' AND scope_value = '') "
                "OR (scope = 'MARKET' AND scope_value = ?) "
                "OR (scope = 'ACCOUNT' AND scope_value = ?)",
                [market.value, account_id],
            ).fetchall()
        except sqlite3.Error:
            raise StoreError("kill switch read failed") from None
        return any(row[0] for row in rows)

    # ── internal helpers ────────────────────────────────────────────────────

    def _fetch_run_by_key(self, run_key: str) -> RuntimeRun | None:
        row = self._connection.execute(
            "SELECT run_id, run_key, config_digest, phase, created_at, updated_at "
            "FROM runs WHERE run_key = ?",
            [run_key],
        ).fetchone()
        return None if row is None else self._decode_run(row)

    def _fetch_run_by_id(self, run_id: str) -> RuntimeRun | None:
        row = self._connection.execute(
            "SELECT run_id, run_key, config_digest, phase, created_at, updated_at "
            "FROM runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        return None if row is None else self._decode_run(row)

    def _fetch_lease(self, run_id: str) -> tuple[datetime, str] | None:
        row = self._connection.execute(
            "SELECT expires_at, attempt_id FROM leases WHERE run_id = ?", [run_id]
        ).fetchone()
        if row is None:
            return None
        return (_decode_datetime(row[0]), row[1])

    def _next_attempt_number(self, run_id: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(attempt_number) FROM run_attempts WHERE run_id = ?", [run_id]
        ).fetchone()
        return 1 if row[0] is None else int(row[0]) + 1

    def _insert_run(self, run: RuntimeRun) -> None:
        self._connection.execute(
            "INSERT INTO runs (run_id, run_key, config_digest, phase, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                run.run_id,
                run.run_key,
                run.config_digest,
                run.phase.value,
                canonical_datetime(run.created_at),
                canonical_datetime(run.updated_at),
            ],
        )

    def _insert_attempt(
        self, run_id: str, attempt_number: int, kind: AttemptKind, now: datetime
    ) -> RuntimeAttempt:
        attempt_id = attempt_id_for(run_id, attempt_number)
        self._connection.execute(
            "INSERT INTO run_attempts "
            "(attempt_id, run_id, attempt_number, kind, phase, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            [
                attempt_id,
                run_id,
                attempt_number,
                kind.value,
                AttemptPhase.CLAIMED.value,
                canonical_datetime(now),
            ],
        )
        return RuntimeAttempt(
            attempt_id=attempt_id,
            run_id=run_id,
            attempt_number=attempt_number,
            kind=kind,
            phase=AttemptPhase.CLAIMED,
            started_at=now,
            ended_at=None,
        )

    def _upsert_lease(self, run_id: str, attempt_id: str, expires_at: datetime) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO leases (run_id, attempt_id, expires_at) VALUES (?, ?, ?)",
            [run_id, attempt_id, canonical_datetime(expires_at)],
        )

    @staticmethod
    def _decode_run(row: tuple[object, ...]) -> RuntimeRun:
        return RuntimeRun(
            run_id=str(row[0]),
            run_key=str(row[1]),
            config_digest=str(row[2]),
            phase=RunPhase(str(row[3])),
            created_at=_decode_datetime(row[4]),
            updated_at=_decode_datetime(row[5]),
        )

    @staticmethod
    def _decode_attempt(row: tuple[object, ...]) -> RuntimeAttempt:
        return RuntimeAttempt(
            attempt_id=str(row[0]),
            run_id=str(row[1]),
            attempt_number=int(str(row[2])),
            kind=AttemptKind(str(row[3])),
            phase=AttemptPhase(str(row[4])),
            started_at=_decode_datetime(row[5]),
            ended_at=None if row[6] is None else _decode_datetime(row[6]),
        )


def _decode_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise StoreError("persisted datetime is not text")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
