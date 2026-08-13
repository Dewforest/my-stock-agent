from __future__ import annotations

import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
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


class BudgetKind(StrEnum):
    NORMAL = "NORMAL"
    RECOVERY = "RECOVERY"


class BudgetClaimOutcome(StrEnum):
    CLAIMED = "CLAIMED"
    EXHAUSTED = "EXHAUSTED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class DecisionInvocationStatus(StrEnum):
    SEND_INTENT_RECORDED = "SEND_INTENT_RECORDED"
    DECISION_RECORDED = "DECISION_RECORDED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    ABANDONED_NO_ORDER = "ABANDONED_NO_ORDER"


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_day_budgets (
                provider_id TEXT NOT NULL,
                day TEXT NOT NULL,
                normal_limit INTEGER NOT NULL,
                recovery_limit INTEGER NOT NULL,
                reserved INTEGER NOT NULL,
                normal_spent INTEGER NOT NULL DEFAULT 0,
                recovery_spent INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider_id, day)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_fetch_attempts (
                attempt_id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                session_date TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                UNIQUE (provider_id, market, symbol, session_date, attempt_number)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_manifests (
                run_id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_invocations (
                run_id TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_id TEXT,
                marked_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_records (
                record_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                occurred_at TEXT NOT NULL
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
                    raise RunConflictError("same run key reappeared with a different config digest")
                if existing.is_terminal:
                    self._connection.execute("COMMIT")
                    return ClaimResult(outcome=ClaimOutcome.RESUMED, run=existing, attempt=None)

                lease = self._fetch_lease(existing.run_id)
                if lease is not None and lease[0] > now:
                    raise LeaseHeldError("a live lease already owns this run")

                attempt_number = self._next_attempt_number(existing.run_id)
                attempt = self._insert_attempt(
                    existing.run_id, attempt_number, AttemptKind.RECOVERY, now
                )
                self._upsert_lease(existing.run_id, attempt.attempt_id, expires_at)
                self._connection.execute("COMMIT")
                return ClaimResult(outcome=ClaimOutcome.RECOVERED, run=existing, attempt=attempt)

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
                raise StoreError(f"illegal transition {run.phase.value} -> {to_phase.value}")
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

    # ── provider budgets and symbol attempts ───────────────────────────────

    def ensure_provider_budget(
        self,
        provider_id: str,
        day: date,
        *,
        normal_limit: int,
        recovery_limit: int,
        reserved: int,
    ) -> None:
        self._ensure_open()
        if (
            type(provider_id) is not str
            or not provider_id
            or type(day) is not date
            or type(normal_limit) is not int
            or normal_limit <= 0
            or type(recovery_limit) is not int
            or recovery_limit < 0
            or type(reserved) is not int
            or reserved < 0
        ):
            raise StoreError("ensure_provider_budget received invalid arguments")
        try:
            self._connection.execute(
                "INSERT OR IGNORE INTO provider_day_budgets "
                "(provider_id, day, normal_limit, recovery_limit, reserved, "
                "normal_spent, recovery_spent, circuit_open) "
                "VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
                [provider_id, day.isoformat(), normal_limit, recovery_limit, reserved],
            )
        except sqlite3.Error:
            raise StoreError("provider budget ensure failed") from None

    def claim_provider_budget(
        self, provider_id: str, day: date, kind: BudgetKind
    ) -> BudgetClaimOutcome:
        self._ensure_open()
        if (
            type(provider_id) is not str
            or not provider_id
            or type(day) is not date
            or type(kind) is not BudgetKind
        ):
            raise StoreError("claim_provider_budget received invalid arguments")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT circuit_open, normal_spent, normal_limit, "
                "recovery_spent, recovery_limit FROM provider_day_budgets "
                "WHERE provider_id = ? AND day = ?",
                [provider_id, day.isoformat()],
            ).fetchone()
            if row is None:
                raise StoreError("provider budget row does not exist")
            if bool(row[0]):
                self._connection.execute("COMMIT")
                return BudgetClaimOutcome.CIRCUIT_OPEN
            if kind is BudgetKind.NORMAL:
                if int(row[1]) >= int(row[2]):
                    self._connection.execute("COMMIT")
                    return BudgetClaimOutcome.EXHAUSTED
                self._connection.execute(
                    "UPDATE provider_day_budgets SET normal_spent = normal_spent + 1 "
                    "WHERE provider_id = ? AND day = ?",
                    [provider_id, day.isoformat()],
                )
            else:
                if int(row[3]) >= int(row[4]):
                    self._connection.execute("COMMIT")
                    return BudgetClaimOutcome.EXHAUSTED
                self._connection.execute(
                    "UPDATE provider_day_budgets SET recovery_spent = recovery_spent + 1 "
                    "WHERE provider_id = ? AND day = ?",
                    [provider_id, day.isoformat()],
                )
            self._connection.execute("COMMIT")
            return BudgetClaimOutcome.CLAIMED
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def open_provider_circuit(self, provider_id: str, day: date) -> None:
        self._ensure_open()
        if type(provider_id) is not str or not provider_id or type(day) is not date:
            raise StoreError("open_provider_circuit received invalid arguments")
        try:
            self._connection.execute(
                "UPDATE provider_day_budgets SET circuit_open = 1 "
                "WHERE provider_id = ? AND day = ?",
                [provider_id, day.isoformat()],
            )
        except sqlite3.Error:
            raise StoreError("provider circuit update failed") from None

    def is_circuit_open(self, provider_id: str, day: date) -> bool:
        self._ensure_open()
        if type(provider_id) is not str or not provider_id or type(day) is not date:
            raise StoreError("is_circuit_open received invalid arguments")
        try:
            row = self._connection.execute(
                "SELECT circuit_open FROM provider_day_budgets WHERE provider_id = ? AND day = ?",
                [provider_id, day.isoformat()],
            ).fetchone()
        except sqlite3.Error:
            raise StoreError("provider circuit read failed") from None
        return row is not None and bool(row[0])

    def record_symbol_attempt(
        self,
        *,
        provider_id: str,
        market: Market,
        symbol: str,
        session_date: date,
        attempt_number: int,
        status: str,
        error_code: str | None,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        self._ensure_open()
        if (
            type(provider_id) is not str
            or not provider_id
            or type(market) is not Market
            or type(symbol) is not str
            or not symbol
            or type(session_date) is not date
            or type(attempt_number) is not int
            or attempt_number <= 0
            or type(status) is not str
            or not status
            or (error_code is not None and type(error_code) is not str)
            or type(started_at) is not datetime
            or started_at.tzinfo is None
            or type(ended_at) is not datetime
            or ended_at.tzinfo is None
        ):
            raise StoreError("record_symbol_attempt received invalid arguments")
        attempt_id = tagged_sha256(
            "symbol-fetch-attempt",
            (
                provider_id,
                market.value,
                symbol,
                session_date.isoformat(),
                attempt_number,
            ),
        )
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO symbol_fetch_attempts "
                "(attempt_id, provider_id, market, symbol, session_date, "
                "attempt_number, status, error_code, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    attempt_id,
                    provider_id,
                    market.value,
                    symbol,
                    session_date.isoformat(),
                    attempt_number,
                    status,
                    error_code,
                    canonical_datetime(started_at),
                    canonical_datetime(ended_at),
                ],
            )
        except sqlite3.Error:
            raise StoreError("symbol attempt record failed") from None

    def has_completed_symbol(
        self, provider_id: str, market: Market, symbol: str, session_date: date
    ) -> bool:
        self._ensure_open()
        if (
            type(provider_id) is not str
            or not provider_id
            or type(market) is not Market
            or type(symbol) is not str
            or not symbol
            or type(session_date) is not date
        ):
            raise StoreError("has_completed_symbol received invalid arguments")
        try:
            row = self._connection.execute(
                "SELECT 1 FROM symbol_fetch_attempts WHERE provider_id = ? "
                "AND market = ? AND symbol = ? AND session_date = ? "
                "AND status = 'SUCCESS' LIMIT 1",
                [provider_id, market.value, symbol, session_date.isoformat()],
            ).fetchone()
        except sqlite3.Error:
            raise StoreError("symbol attempt read failed") from None
        return row is not None

    # ── snapshot manifests ──────────────────────────────────────────────────

    def store_snapshot_manifest(
        self,
        *,
        run_id: str,
        market: Market,
        digest: str,
        payload: str,
    ) -> None:
        self._ensure_open()
        if (
            type(run_id) is not str
            or not run_id
            or type(market) is not Market
            or type(digest) is not str
            or not digest
            or type(payload) is not str
        ):
            raise StoreError("store_snapshot_manifest received invalid arguments")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT digest FROM snapshot_manifests WHERE run_id = ?", [run_id]
            ).fetchone()
            if row is not None and row[0] != digest:
                raise StoreError("snapshot manifest identity conflict")
            self._connection.execute(
                "INSERT OR REPLACE INTO snapshot_manifests "
                "(run_id, market, digest, payload) VALUES (?, ?, ?, ?)",
                [run_id, market.value, digest, payload],
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def load_snapshot_manifest(self, run_id: str) -> tuple[str, str, str] | None:
        self._ensure_open()
        if type(run_id) is not str or not run_id:
            raise StoreError("load_snapshot_manifest received an invalid run id")
        try:
            row = self._connection.execute(
                "SELECT market, digest, payload FROM snapshot_manifests WHERE run_id = ?",
                [run_id],
            ).fetchone()
        except sqlite3.Error:
            raise StoreError("snapshot manifest read failed") from None
        if row is None:
            return None
        return (str(row[0]), str(row[1]), str(row[2]))

    # ── decision invocations and reconciliation ─────────────────────────────

    def mark_send_intent(self, run_id: str, request_fingerprint: str, now: datetime) -> None:
        self._ensure_open()
        if (
            type(run_id) is not str
            or not run_id
            or type(request_fingerprint) is not str
            or not request_fingerprint
            or type(now) is not datetime
            or now.tzinfo is None
        ):
            raise StoreError("mark_send_intent received invalid arguments")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT status, request_fingerprint FROM decision_invocations WHERE run_id = ?",
                [run_id],
            ).fetchone()
            if row is not None:
                status, fingerprint = str(row[0]), str(row[1])
                if (
                    status == DecisionInvocationStatus.SEND_INTENT_RECORDED.value
                    and fingerprint == request_fingerprint
                ):
                    self._connection.execute("COMMIT")
                    return
                if fingerprint != request_fingerprint:
                    raise StoreError("send intent fingerprint conflict")
                raise StoreError("run already advanced past send intent")
            self._connection.execute(
                "INSERT INTO decision_invocations "
                "(run_id, request_fingerprint, status, decision_id, marked_at, resolved_at) "
                "VALUES (?, ?, ?, NULL, ?, NULL)",
                [
                    run_id,
                    request_fingerprint,
                    DecisionInvocationStatus.SEND_INTENT_RECORDED.value,
                    canonical_datetime(now),
                ],
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def record_decision(self, run_id: str, decision_id: str, now: datetime) -> None:
        self._ensure_open()
        if (
            type(run_id) is not str
            or not run_id
            or type(decision_id) is not str
            or not decision_id
            or type(now) is not datetime
            or now.tzinfo is None
        ):
            raise StoreError("record_decision received invalid arguments")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT status, decision_id FROM decision_invocations WHERE run_id = ?",
                [run_id],
            ).fetchone()
            if row is None:
                raise StoreError("decision recorded without a prior send intent")
            status = str(row[0])
            existing = None if row[1] is None else str(row[1])
            if status == DecisionInvocationStatus.DECISION_RECORDED.value:
                if existing == decision_id:
                    self._connection.execute("COMMIT")
                    return
                raise StoreError("decision identity conflict")
            if status != DecisionInvocationStatus.SEND_INTENT_RECORDED.value:
                raise StoreError("run cannot transition to decision recorded")
            self._connection.execute(
                "UPDATE decision_invocations SET status = ?, decision_id = ?, resolved_at = ? "
                "WHERE run_id = ?",
                [
                    DecisionInvocationStatus.DECISION_RECORDED.value,
                    decision_id,
                    canonical_datetime(now),
                    run_id,
                ],
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def mark_needs_reconciliation(self, run_id: str, now: datetime) -> None:
        self._ensure_open()
        if type(run_id) is not str or not run_id or type(now) is not datetime or now.tzinfo is None:
            raise StoreError("mark_needs_reconciliation received invalid arguments")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT status FROM decision_invocations WHERE run_id = ?", [run_id]
            ).fetchone()
            if row is None:
                raise StoreError("reconciliation marked without a prior send intent")
            if str(row[0]) == DecisionInvocationStatus.SEND_INTENT_RECORDED.value:
                self._connection.execute(
                    "UPDATE decision_invocations SET status = ?, resolved_at = ? WHERE run_id = ?",
                    [
                        DecisionInvocationStatus.NEEDS_RECONCILIATION.value,
                        canonical_datetime(now),
                        run_id,
                    ],
                )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def get_decision_invocation(self, run_id: str) -> tuple[str, str, str | None] | None:
        self._ensure_open()
        if type(run_id) is not str or not run_id:
            raise StoreError("get_decision_invocation received an invalid run id")
        try:
            row = self._connection.execute(
                "SELECT status, request_fingerprint, decision_id FROM decision_invocations "
                "WHERE run_id = ?",
                [run_id],
            ).fetchone()
        except sqlite3.Error:
            raise StoreError("decision invocation read failed") from None
        if row is None:
            return None
        return (str(row[0]), str(row[1]), None if row[2] is None else str(row[2]))

    def abandon_decision(
        self,
        *,
        run_id: str,
        operator: str,
        reason: str,
        request_fingerprint: str,
        now: datetime,
    ) -> None:
        self._ensure_open()
        if (
            type(run_id) is not str
            or not run_id
            or type(operator) is not str
            or not operator
            or type(reason) is not str
            or not reason
            or type(request_fingerprint) is not str
            or not request_fingerprint
            or type(now) is not datetime
            or now.tzinfo is None
        ):
            raise StoreError("abandon_decision received invalid arguments")
        record_id = tagged_sha256(
            "reconciliation-record",
            (run_id, operator, reason, request_fingerprint, canonical_datetime(now)),
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT status FROM decision_invocations WHERE run_id = ?", [run_id]
            ).fetchone()
            if row is None:
                raise StoreError("abandon targets an unknown decision invocation")
            if str(row[0]) != DecisionInvocationStatus.NEEDS_RECONCILIATION.value:
                raise StoreError("only a needs-reconciliation run can be abandoned")
            self._connection.execute(
                "INSERT INTO reconciliation_records "
                "(record_id, run_id, operator, reason, request_fingerprint, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [record_id, run_id, operator, reason, request_fingerprint, canonical_datetime(now)],
            )
            self._connection.execute(
                "UPDATE decision_invocations SET status = ?, resolved_at = ? WHERE run_id = ?",
                [
                    DecisionInvocationStatus.ABANDONED_NO_ORDER.value,
                    canonical_datetime(now),
                    run_id,
                ],
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

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
