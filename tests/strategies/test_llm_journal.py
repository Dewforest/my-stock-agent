from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import duckdb
import pytest

from stock_agent.domain import Side
from stock_agent.strategies.llm_contract import (
    LLMDecisionRecord,
    LLMDecisionResponse,
    LLMDecisionSelection,
    LLMDecisionStatus,
    LLMInvocationAttempt,
    LLMInvocationMode,
    LLMRunAttestation,
    decision_id_for,
    response_digest_for,
)
from stock_agent.strategies.llm_journal import (
    LLMDecisionJournal,
    LLMJournalClosedError,
    LLMJournalConflictError,
    LLMJournalDataError,
    LLMJournalPersistenceError,
    LLMJournalValidationError,
)

NOW = datetime(2026, 8, 6, 20, tzinfo=UTC)
REQUEST_A = "llm-decision-request-sha256:" + "a" * 64
REQUEST_B = "llm-decision-request-sha256:" + "b" * 64


def _selection(*, thesis: str = "Trend confirmed") -> LLMDecisionSelection:
    return LLMDecisionSelection(
        symbol="IBM",
        action=Side.BUY,
        confidence=80,
        thesis=thesis,
        invalidation="Trend breaks",
    )


def _record(
    *,
    request_fingerprint: str = REQUEST_A,
    thesis: str = "Trend confirmed",
    started_at: datetime = NOW,
) -> LLMDecisionRecord:
    selections = (_selection(thesis=thesis),)
    response = LLMDecisionResponse(
        schema_version="llm-decision-response/v1",
        request_fingerprint=request_fingerprint,
        selections=selections,
        provider_response_id="response-1",
    )
    response_digest = response_digest_for(response)
    return LLMDecisionRecord(
        decision_id=decision_id_for(request_fingerprint, response_digest),
        request_fingerprint=request_fingerprint,
        response_digest=response_digest,
        selections=selections,
        config_version="strategy-a-v1",
        model_identity_policy_id="exact-model-v1",
        prompt_template_id="strategy-a-decision-v1",
        prompt_template_digest="prompt-sha256:" + "c" * 64,
        model_identity="fixture/model",
        model_revision="revision-1",
        provider_response_id="response-1",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
    )


def _attempt(
    record: LLMDecisionRecord | None = None,
    *,
    attempt_id: str = "attempt-1",
    request_fingerprint: str = REQUEST_A,
    status: LLMDecisionStatus = LLMDecisionStatus.TIMEOUT,
    started_at: datetime = NOW,
) -> LLMInvocationAttempt:
    if record is not None:
        request_fingerprint = record.request_fingerprint
        status = LLMDecisionStatus.SUCCESS
        started_at = record.started_at
    return LLMInvocationAttempt(
        attempt_id=attempt_id,
        request_fingerprint=request_fingerprint,
        status=status,
        response_digest=None if record is None else record.response_digest,
        decision_id=None if record is None else record.decision_id,
        provider_response_id=None if record is None else record.provider_response_id,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=1),
    )


def _attestation(
    decision_id: str,
    *,
    attestation_id: str = "attestation-1",
    occurred_at: datetime = NOW,
) -> LLMRunAttestation:
    return LLMRunAttestation(
        attestation_id=attestation_id,
        execution_label="run-1",
        invocation_mode=LLMInvocationMode.REPLAY,
        decision_ids=(decision_id,),
        occurred_at=occurred_at,
    )


def test_initializes_separate_schema_and_atomically_appends_success(tmp_path: Path) -> None:
    database = tmp_path / "journal.duckdb"
    record = _record()
    attempt = _attempt(record)

    with LLMDecisionJournal(database) as journal:
        journal.append_attempt(attempt, decision=record)
        assert journal.decision_by_request_fingerprint(REQUEST_A) == record
        assert journal.decision_by_id(record.decision_id) == record
        assert journal.list_decisions() == (record,)
        assert journal.list_attempts() == (attempt,)

    connection = duckdb.connect(str(database), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SHOW TABLES").fetchall()
        }
    finally:
        connection.close()
    assert tables == {
        "llm_decision_records",
        "llm_invocation_attempts",
        "llm_run_attestations",
    }


def test_identical_terminal_append_is_idempotent_across_reopen(tmp_path: Path) -> None:
    database = tmp_path / "journal.duckdb"
    record = _record()
    attempt = _attempt(record)
    attestation = _attestation(record.decision_id)

    with LLMDecisionJournal(database) as journal:
        journal.append_attempt(attempt, decision=record)
        journal.append_attempt(attempt, decision=record)
        journal.append_attestation(attestation)

    with LLMDecisionJournal(database) as journal:
        assert journal.list_decisions() == (record,)
        assert journal.list_attempts() == (attempt,)
        assert journal.list_attestations() == (attestation,)


def test_request_fingerprint_conflict_rolls_back_new_attempt(tmp_path: Path) -> None:
    original = _record()
    conflicting = _record(thesis="Different bounded decision")
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(_attempt(original), decision=original)

        with pytest.raises(LLMJournalConflictError, match="canonical decision"):
            journal.append_attempt(
                _attempt(conflicting, attempt_id="attempt-2"),
                decision=conflicting,
            )

        assert journal.list_decisions() == (original,)
        assert journal.list_attempts() == (_attempt(original),)


def test_decision_id_with_different_provenance_conflicts_without_partial_write(
    tmp_path: Path,
) -> None:
    original = _record()
    conflicting = LLMDecisionRecord(
        **{
            **original.model_dump(),
            "model_revision": "revision-2",
        }
    )
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(_attempt(original), decision=original)

        with pytest.raises(LLMJournalConflictError):
            journal.append_attempt(
                _attempt(conflicting, attempt_id="attempt-2"),
                decision=conflicting,
            )

        assert journal.list_decisions() == (original,)
        assert tuple(item.attempt_id for item in journal.list_attempts()) == ("attempt-1",)


def test_failed_invocation_appends_only_attempt_and_rejects_a_decision(tmp_path: Path) -> None:
    failure = _attempt(attempt_id="failed-1", status=LLMDecisionStatus.TRANSPORT)
    record = _record()
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(failure)
        assert journal.list_attempts() == (failure,)
        assert journal.list_decisions() == ()

        with pytest.raises(LLMJournalValidationError, match="failed invocations"):
            journal.append_attempt(failure, decision=record)
        assert journal.list_decisions() == ()


def test_attempt_and_attestation_exact_ids_are_idempotent_and_conflicting(
    tmp_path: Path,
) -> None:
    record = _record()
    attempt = _attempt(record)
    attestation = _attestation(record.decision_id)
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(attempt, decision=record)
        journal.append_attempt(attempt, decision=record)
        journal.append_attestation(attestation)
        journal.append_attestation(attestation)

        changed_attempt = _attempt(
            attempt_id=attempt.attempt_id,
            status=LLMDecisionStatus.TIMEOUT,
        )
        with pytest.raises(LLMJournalConflictError, match="attempt"):
            journal.append_attempt(changed_attempt)

        changed_attestation = LLMRunAttestation(
            **{**attestation.model_dump(), "execution_label": "run-2"}
        )
        with pytest.raises(LLMJournalConflictError, match="attestation"):
            journal.append_attestation(changed_attestation)

        assert journal.list_attempts() == (attempt,)
        assert journal.list_attestations() == (attestation,)


def test_lists_have_deterministic_identity_and_time_order(tmp_path: Path) -> None:
    record_b = _record(request_fingerprint=REQUEST_B, started_at=NOW + timedelta(hours=1))
    record_a = _record()
    attestation_b = _attestation(
        record_b.decision_id,
        attestation_id="attestation-b",
        occurred_at=NOW + timedelta(hours=1),
    )
    attestation_a = _attestation(record_a.decision_id, attestation_id="attestation-a")
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(_attempt(record_b, attempt_id="attempt-b"), decision=record_b)
        journal.append_attempt(_attempt(record_a, attempt_id="attempt-a"), decision=record_a)
        journal.append_attestation(attestation_b)
        journal.append_attestation(attestation_a)

        assert journal.list_decisions() == (record_a, record_b)
        assert tuple(item.attempt_id for item in journal.list_attempts()) == (
            "attempt-a",
            "attempt-b",
        )
        assert journal.list_attestations() == (attestation_a, attestation_b)


def test_attestation_history_cannot_replace_the_canonical_decision(tmp_path: Path) -> None:
    record = _record()
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        journal.append_attempt(_attempt(record), decision=record)
        journal.append_attestation(_attestation(record.decision_id))
        journal.append_attestation(
            LLMRunAttestation(
                attestation_id="attestation-record",
                execution_label="record-run",
                invocation_mode=LLMInvocationMode.RECORD,
                decision_ids=(record.decision_id,),
                occurred_at=NOW + timedelta(seconds=1),
            )
        )
        assert journal.decision_by_request_fingerprint(REQUEST_A) == record
        assert journal.decision_by_id(record.decision_id) == record


def test_attestation_rejects_unknown_decision_ids_without_partial_write(
    tmp_path: Path,
) -> None:
    unknown = "llm-decision-sha256:" + "f" * 64
    with LLMDecisionJournal(tmp_path / "journal.duckdb") as journal:
        with pytest.raises(LLMJournalValidationError, match="unknown decision"):
            journal.append_attestation(_attestation(unknown))

        assert journal.list_attestations() == ()


def test_read_detects_index_and_payload_identity_corruption(tmp_path: Path) -> None:
    database = tmp_path / "journal.duckdb"
    record = _record()
    tampered_id = "llm-decision-sha256:" + "f" * 64
    with LLMDecisionJournal(database) as journal:
        journal.append_attempt(_attempt(record), decision=record)

    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "UPDATE llm_decision_records SET decision_id = ?",
            [tampered_id],
        )
    finally:
        connection.close()

    with LLMDecisionJournal(database) as journal:
        with pytest.raises(LLMJournalDataError, match="identity"):
            journal.decision_by_id(tampered_id)
        with pytest.raises(LLMJournalDataError, match="identity"):
            journal.list_decisions()


@pytest.mark.parametrize("polluted_kind", ["top", "nested", "wrong-model"])
def test_rejects_constructed_polluted_or_non_exact_models(
    tmp_path: Path, polluted_kind: str
) -> None:
    record = _record()
    attempt: object = _attempt(record)
    decision: object = record
    if polluted_kind == "top":
        attempt.__dict__["api_key"] = "secret"  # type: ignore[attr-defined]
    elif polluted_kind == "nested":
        bad_selection = LLMDecisionSelection.model_construct(
            **{**record.selections[0].model_dump(), "confidence": "eighty"}
        )
        decision = LLMDecisionRecord.model_construct(
            **{**record.model_dump(), "selections": (bad_selection,)}
        )
    else:
        attempt = record

    with LLMDecisionJournal(tmp_path / f"{polluted_kind}.duckdb") as journal:
        with pytest.raises(LLMJournalValidationError):
            journal.append_attempt(attempt, decision=decision)  # type: ignore[arg-type]
        assert journal.list_attempts() == ()
        assert journal.list_decisions() == ()


def test_persisted_payloads_revalidate_and_corruption_has_stable_error(tmp_path: Path) -> None:
    database = tmp_path / "journal.duckdb"
    record = _record()
    with LLMDecisionJournal(database) as journal:
        journal.append_attempt(_attempt(record), decision=record)

    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "UPDATE llm_decision_records SET payload = ?",
            ['{"api_key":"must-not-leak"}'],
        )
    finally:
        connection.close()

    with LLMDecisionJournal(database) as journal:
        with pytest.raises(LLMJournalDataError) as captured:
            journal.decision_by_id(record.decision_id)
    assert str(captured.value) == "persisted LLM journal payload is invalid"
    assert "api_key" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_close_is_idempotent_and_operations_raise_stable_closed_error(tmp_path: Path) -> None:
    journal = LLMDecisionJournal(tmp_path / "journal.duckdb")
    journal.close()
    journal.close()
    with pytest.raises(LLMJournalClosedError) as captured:
        journal.list_attempts()
    assert str(captured.value) == "LLM decision journal is closed"


def test_initialization_failure_does_not_leak_raw_duckdb_message(tmp_path: Path) -> None:
    with pytest.raises(LLMJournalPersistenceError) as captured:
        LLMDecisionJournal(tmp_path)
    assert str(captured.value) == "LLM decision journal initialization failed"
    assert captured.value.__cause__ is None


def test_two_connections_concurrently_choose_one_canonical_winner(tmp_path: Path) -> None:
    database = tmp_path / "journal.duckdb"
    first = LLMDecisionJournal(database)
    second = LLMDecisionJournal(database)
    barrier = Barrier(2)
    records = (_record(), _record(thesis="Concurrent alternative"))

    def append(
        journal: LLMDecisionJournal, record: LLMDecisionRecord, attempt_id: str
    ) -> str:
        barrier.wait()
        try:
            journal.append_attempt(_attempt(record, attempt_id=attempt_id), decision=record)
        except LLMJournalConflictError:
            return "conflict"
        return "winner"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(
                future.result()
                for future in (
                    pool.submit(append, first, records[0], "attempt-a"),
                    pool.submit(append, second, records[1], "attempt-b"),
                )
            )
        assert sorted(outcomes) == ["conflict", "winner"]
    finally:
        first.close()
        second.close()

    with LLMDecisionJournal(database) as journal:
        assert len(journal.list_decisions()) == 1
        assert len(journal.list_attempts()) == 1
