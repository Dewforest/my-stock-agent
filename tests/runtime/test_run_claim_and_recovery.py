from __future__ import annotations

import os
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_agent.domain import Market
from stock_agent.runtime.identities import (
    RunIdentity,
    run_config_digest_for,
    run_id_for,
    run_key_for,
)
from stock_agent.runtime.state import (
    AttemptKind,
    AttemptPhase,
    KillSwitchScope,
    RunPhase,
)
from stock_agent.runtime.store import (
    ClaimOutcome,
    LeaseHeldError,
    RunConflictError,
    RuntimeStore,
    StoreClosedError,
    StoreError,
)


def identity(**overrides: object) -> RunIdentity:
    values: dict[str, object] = {
        "schema_version": "paper-run/v1",
        "market": Market.US,
        "account_id": "us-account-1",
        "close_session_date": date(2026, 8, 13),
        "strategy_id": "strategy-a-bounded-llm",
        "config_version": "strategy-a-v1",
        "universe_id": "dual-market-20/v1",
        "universe_digest": "universe-sha256:" + "1" * 64,
        "calendar_version": "us-2026-v1",
        "model_policy_version": "deepseek-v4-pro",
    }
    values.update(overrides)
    return RunIdentity(**values)  # type: ignore[arg-type]


def store_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime.sqlite"


NOW = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)


def open_store(path: Path) -> RuntimeStore:
    return RuntimeStore(path)


# ── 1. deterministic run id ────────────────────────────────────────────────


def test_run_id_is_deterministic_and_ignores_process_variability() -> None:
    first = identity()
    second = identity()
    assert run_id_for(first) == run_id_for(second)
    assert run_key_for(first) == run_key_for(second)
    assert run_config_digest_for(first) == run_config_digest_for(second)


def test_run_id_changes_only_with_identity_fields() -> None:
    base = identity()
    # Config fingerprint fields change the run id (and config digest), not the run key.
    changed_config = identity(universe_digest="universe-sha256:" + "2" * 64)
    assert run_id_for(changed_config) != run_id_for(base)
    assert run_config_digest_for(changed_config) != run_config_digest_for(base)
    assert run_key_for(changed_config) == run_key_for(base)

    # Business identity fields change the run key.
    changed_account = identity(account_id="us-account-2")
    assert run_key_for(changed_account) != run_key_for(base)

    # Session/strategy changes also change the run key.
    assert run_key_for(identity(strategy_id="other-strategy")) != run_key_for(base)
    assert run_key_for(identity(close_session_date=date(2026, 8, 14))) != run_key_for(base)


def test_run_id_excludes_phase_pid_and_attempt_number() -> None:
    # The identity model must forbid process-variability fields entirely.
    for field in ("phase", "pid", "hostname", "wake_time", "attempt_number"):
        polluted = identity().model_dump()
        polluted[field] = "x"  # type: ignore[assignment]
        with pytest.raises(ValidationError):
            RunIdentity(**polluted)


# ── 2. first claim creates one run and one immutable attempt ───────────────


def test_first_claim_creates_one_run_and_one_attempt(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    result = store.claim_run(identity(), now=NOW, lease_seconds=60)
    assert result.outcome is ClaimOutcome.CREATED
    assert result.attempt.attempt_number == 1
    assert result.attempt.kind is AttemptKind.PRIMARY
    assert result.attempt.phase is AttemptPhase.CLAIMED
    assert result.run.phase is RunPhase.CLAIMED

    runs = store.list_runs()
    attempts = store.list_attempts()
    assert len(runs) == 1
    assert len(attempts) == 1
    assert runs[0].run_id == result.run.run_id
    assert attempts[0].attempt_id == result.attempt.attempt_id


def test_attempt_records_are_immutable(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    result = store.claim_run(identity(), now=NOW, lease_seconds=60)
    attempt = result.attempt
    with pytest.raises((TypeError, ValueError)):
        attempt.model_copy(update={"phase": AttemptPhase.SUCCEEDED})  # type: ignore[arg-type]


# ── 3. concurrent identical claims produce one run authority ───────────────


def test_concurrent_identical_claims_produce_single_run(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = open_store(path)
    store.claim_run(identity(), now=NOW, lease_seconds=5)

    import threading

    outcomes: list[object] = []
    errors: list[object] = []

    def worker() -> None:
        try:
            # A fresh connection per worker simulates separate processes.
            concurrent = RuntimeStore(path)
            outcomes.append(concurrent.claim_run(identity(), now=NOW, lease_seconds=5))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Either every worker saw the same existing run (idempotent), or all but the
    # original holder saw a held lease. In no case is a second run row created.
    assert not errors or all(isinstance(item, LeaseHeldError) for item in errors)
    assert len(store.list_runs()) == 1
    assert len(store.list_attempts()) == 1


# ── 4. same run/config returns existing terminal result ────────────────────


def test_same_run_and_config_returns_terminal_result_idempotently(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    first = store.claim_run(identity(), now=NOW, lease_seconds=60)
    # Walk the frozen state machine to a terminal phase.
    store.transition_run(first.run.run_id, RunPhase.SNAPSHOT_FROZEN)
    store.transition_run(first.run.run_id, RunPhase.CREDENTIAL_READY)
    store.transition_run(first.run.run_id, RunPhase.SEND_INTENT_RECORDED)
    store.transition_run(first.run.run_id, RunPhase.DECISION_RECORDED)
    store.transition_run(first.run.run_id, RunPhase.ORDERS_PERSISTED)
    store.transition_run(first.run.run_id, RunPhase.SUCCEEDED)

    second = store.claim_run(identity(), now=NOW + timedelta(minutes=1), lease_seconds=60)
    assert second.outcome is ClaimOutcome.RESUMED
    assert second.run.run_id == first.run.run_id
    assert second.run.phase is RunPhase.SUCCEEDED
    assert len(store.list_attempts()) == 1


# ── 5. same run key with different config digest conflicts ─────────────────


def test_same_run_key_with_different_config_digest_conflicts(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.claim_run(identity(), now=NOW, lease_seconds=60)

    with pytest.raises(RunConflictError):
        store.claim_run(
            identity(universe_digest="universe-sha256:" + "9" * 64),
            now=NOW,
            lease_seconds=60,
        )


# ── 6. active unexpired lease blocks a second worker ───────────────────────


def test_active_lease_blocks_second_worker(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = open_store(path)
    store.claim_run(identity(), now=NOW, lease_seconds=60)

    second = RuntimeStore(path)
    with pytest.raises(LeaseHeldError):
        second.claim_run(identity(), now=NOW + timedelta(seconds=30), lease_seconds=60)


# ── 7. expired lease creates exactly one recovery attempt ──────────────────


def test_expired_lease_creates_exactly_one_recovery_attempt(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = open_store(path)
    store.claim_run(identity(), now=NOW, lease_seconds=60)

    second = RuntimeStore(path)
    recovered = second.claim_run(
        identity(), now=NOW + timedelta(seconds=120), lease_seconds=60
    )
    assert recovered.outcome is ClaimOutcome.RECOVERED
    assert recovered.attempt.kind is AttemptKind.RECOVERY
    assert recovered.attempt.attempt_number == 2

    attempts = second.list_attempts()
    assert len(attempts) == 2
    assert sum(item.kind is AttemptKind.RECOVERY for item in attempts) == 1


# ── 8. close/reopen preserves all state ────────────────────────────────────


def test_close_and_reopen_preserves_state(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = open_store(path)
    result = store.claim_run(identity(), now=NOW, lease_seconds=60)
    store.transition_run(result.run.run_id, RunPhase.FAILED_BEFORE_DECISION)
    store.close()

    reopened = open_store(path)
    runs = reopened.list_runs()
    attempts = reopened.list_attempts()
    assert len(runs) == 1
    assert runs[0].phase is RunPhase.FAILED_BEFORE_DECISION
    assert len(attempts) == 1


def test_closed_store_rejects_operations(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.close()
    with pytest.raises(StoreClosedError):
        store.list_runs()


# ── 9. invalid transition and terminal mutation fail atomically ────────────


def test_invalid_transition_fails_without_mutation(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    result = store.claim_run(identity(), now=NOW, lease_seconds=60)

    # CLAIMED -> SUCCEEDED is not a legal direct transition in the frozen machine.
    with pytest.raises(StoreError):
        store.transition_run(result.run.run_id, RunPhase.SUCCEEDED)

    assert store.list_runs()[0].phase is RunPhase.CLAIMED


def test_terminal_run_cannot_be_mutated(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    result = store.claim_run(identity(), now=NOW, lease_seconds=60)
    store.transition_run(result.run.run_id, RunPhase.FAILED_BEFORE_DECISION)

    with pytest.raises(StoreError):
        store.transition_run(result.run.run_id, RunPhase.CLAIMED)

    assert store.list_runs()[0].phase is RunPhase.FAILED_BEFORE_DECISION


# ── 10. sqlite pragmas and user-only permissions ───────────────────────────


def test_sqlite_wal_foreign_keys_and_busy_timeout(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    # Reaching through the store's connection to assert durability settings.
    connection = store._connection
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_store_files_are_user_only(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    store = open_store(path)
    # Trigger a write so the WAL sidecar files are materialized.
    store.claim_run(identity(), now=NOW, lease_seconds=60)
    store.close()

    store_files = [entry for entry in path.parent.iterdir() if entry.name.startswith(path.name)]
    assert store_files, "expected at least the main store file"
    for entry in store_files:
        mode = stat.S_IMODE(os.stat(entry).st_mode)
        assert mode & 0o077 == 0, f"{entry.name} is not user-only"


# ── 11. independent kill switch scopes ─────────────────────────────────────


def test_kill_switch_scopes_are_independent(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.set_kill_switch(KillSwitchScope.ACCOUNT, "cn-account-1", set_by="operator", now=NOW)

    assert store.is_blocked(Market.CN, "cn-account-1")
    assert not store.is_blocked(Market.CN, "cn-account-2")
    assert not store.is_blocked(Market.US, "us-account-1")


def test_market_kill_switch_does_not_block_other_market(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.set_kill_switch(KillSwitchScope.MARKET, "CN", set_by="operator", now=NOW)

    assert store.is_blocked(Market.CN, "cn-account-1")
    assert not store.is_blocked(Market.US, "us-account-1")


def test_global_kill_switch_blocks_everything(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.set_kill_switch(KillSwitchScope.GLOBAL, "", set_by="operator", now=NOW)

    assert store.is_blocked(Market.CN, "cn-account-1")
    assert store.is_blocked(Market.US, "us-account-1")


def test_clearing_kill_switch_restores_access(tmp_path: Path) -> None:
    store = open_store(store_path(tmp_path))
    store.set_kill_switch(KillSwitchScope.ACCOUNT, "cn-account-1", set_by="operator", now=NOW)
    store.clear_kill_switch(KillSwitchScope.ACCOUNT, "cn-account-1")

    assert not store.is_blocked(Market.CN, "cn-account-1")
