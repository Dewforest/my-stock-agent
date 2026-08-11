from __future__ import annotations

import json
from datetime import datetime
from os import PathLike
from typing import Any, TypeVar

import duckdb
from pydantic import BaseModel, ValidationError

from stock_agent.audit import canonical_datetime
from stock_agent.domain import Side
from stock_agent.strategies.llm_contract import (
    LLMDecisionRecord,
    LLMDecisionSelection,
    LLMDecisionStatus,
    LLMInvocationAttempt,
    LLMInvocationMode,
    LLMRunAttestation,
)

_JournalModel = TypeVar(
    "_JournalModel", LLMDecisionRecord, LLMInvocationAttempt, LLMRunAttestation
)


class LLMJournalError(Exception):
    """Base class for stable decision-journal failures."""


class LLMJournalValidationError(LLMJournalError, ValueError):
    """Raised when a caller supplies a non-exact or inconsistent domain value."""


class LLMJournalConflictError(LLMJournalError):
    """Raised when an append-only identity already has different content."""


class LLMJournalPersistenceError(LLMJournalError):
    """Raised when durable storage cannot complete an operation."""


class LLMJournalDataError(LLMJournalError):
    """Raised when persisted content does not revalidate into its strict model."""


class LLMJournalClosedError(LLMJournalError):
    """Raised when an operation is attempted after close."""


class LLMDecisionJournal:
    """Durable append-only DuckDB journal for bounded-LLM audit values."""

    def __init__(self, database: str | PathLike[str] = ":memory:") -> None:
        self._closed = False
        try:
            self._connection = duckdb.connect(str(database))
            self._initialize_schema()
        except duckdb.Error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise LLMJournalPersistenceError(
                "LLM decision journal initialization failed"
            ) from None

    def _initialize_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_decision_records (
                decision_id VARCHAR PRIMARY KEY,
                request_fingerprint VARCHAR NOT NULL UNIQUE,
                payload VARCHAR NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_invocation_attempts (
                attempt_id VARCHAR PRIMARY KEY,
                request_fingerprint VARCHAR NOT NULL,
                payload VARCHAR NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_run_attestations (
                attestation_id VARCHAR PRIMARY KEY,
                occurred_at VARCHAR NOT NULL,
                payload VARCHAR NOT NULL
            )
            """
        )

    def __enter__(self) -> LLMDecisionJournal:
        self._ensure_open()
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._connection.close()
        except duckdb.Error:
            raise LLMJournalPersistenceError(
                "LLM decision journal close failed"
            ) from None
        finally:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise LLMJournalClosedError("LLM decision journal is closed")

    def append_attempt(
        self,
        attempt: LLMInvocationAttempt,
        *,
        decision: LLMDecisionRecord | None = None,
    ) -> None:
        self._ensure_open()
        exact_attempt = _exact_model(attempt, LLMInvocationAttempt, "attempt")
        exact_decision = (
            None
            if decision is None
            else _exact_model(decision, LLMDecisionRecord, "decision")
        )
        self._validate_terminal_pair(exact_attempt, exact_decision)
        attempt_payload = _encode(exact_attempt)
        decision_payload = None if exact_decision is None else _encode(exact_decision)
        by_id: str | None = None

        try:
            self._connection.execute("BEGIN TRANSACTION")
            existing_attempt = self._payload_for(
                "llm_invocation_attempts", "attempt_id", exact_attempt.attempt_id
            )
            if existing_attempt is not None and existing_attempt != attempt_payload:
                raise LLMJournalConflictError(
                    "conflicting invocation attempt identity"
                )

            if exact_decision is not None:
                by_request = self._payload_for(
                    "llm_decision_records",
                    "request_fingerprint",
                    exact_decision.request_fingerprint,
                )
                by_id = self._payload_for(
                    "llm_decision_records", "decision_id", exact_decision.decision_id
                )
                if (by_request is not None and by_request != decision_payload) or (
                    by_id is not None and by_id != decision_payload
                ):
                    raise LLMJournalConflictError(
                        "conflicting canonical decision identity"
                    )
                if existing_attempt is not None and by_id is None:
                    raise LLMJournalDataError(
                        "successful attempt exists without its canonical decision"
                    )

            if existing_attempt is None:
                self._connection.execute(
                    "INSERT INTO llm_invocation_attempts VALUES (?, ?, ?)",
                    [
                        exact_attempt.attempt_id,
                        exact_attempt.request_fingerprint,
                        attempt_payload,
                    ],
                )
            if exact_decision is not None and by_id is None:
                self._connection.execute(
                    "INSERT INTO llm_decision_records VALUES (?, ?, ?)",
                    [
                        exact_decision.decision_id,
                        exact_decision.request_fingerprint,
                        decision_payload,
                    ],
                )
            self._connection.execute("COMMIT")
        except LLMJournalError:
            self._rollback()
            raise
        except (duckdb.ConstraintException, duckdb.TransactionException):
            self._rollback()
            raise LLMJournalConflictError(
                "concurrent append-only journal conflict"
            ) from None
        except duckdb.Error:
            self._rollback()
            raise LLMJournalPersistenceError(
                "LLM decision journal append failed"
            ) from None

    @staticmethod
    def _validate_terminal_pair(
        attempt: LLMInvocationAttempt, decision: LLMDecisionRecord | None
    ) -> None:
        if attempt.status is not LLMDecisionStatus.SUCCESS:
            if decision is not None:
                raise LLMJournalValidationError(
                    "failed invocations cannot store canonical decisions"
                )
            return
        if decision is None:
            raise LLMJournalValidationError(
                "successful invocations require a canonical decision"
            )
        linked = (
            attempt.request_fingerprint == decision.request_fingerprint
            and attempt.response_digest == decision.response_digest
            and attempt.decision_id == decision.decision_id
            and attempt.provider_response_id == decision.provider_response_id
            and attempt.started_at == decision.started_at
            and attempt.ended_at == decision.ended_at
        )
        if not linked:
            raise LLMJournalValidationError(
                "attempt and canonical decision provenance do not match"
            )

    def append_attestation(self, attestation: LLMRunAttestation) -> None:
        self._ensure_open()
        exact = _exact_model(attestation, LLMRunAttestation, "attestation")
        payload = _encode(exact)
        try:
            self._connection.execute("BEGIN TRANSACTION")
            existing = self._payload_for(
                "llm_run_attestations", "attestation_id", exact.attestation_id
            )
            if existing is not None and existing != payload:
                raise LLMJournalConflictError("conflicting run attestation identity")
            if any(
                self._payload_for("llm_decision_records", "decision_id", decision_id)
                is None
                for decision_id in exact.decision_ids
            ):
                raise LLMJournalValidationError(
                    "run attestation references an unknown decision"
                )
            if existing is None:
                self._connection.execute(
                    "INSERT INTO llm_run_attestations VALUES (?, ?, ?)",
                    [exact.attestation_id, canonical_datetime(exact.occurred_at), payload],
                )
            self._connection.execute("COMMIT")
        except LLMJournalError:
            self._rollback()
            raise
        except (duckdb.ConstraintException, duckdb.TransactionException):
            self._rollback()
            raise LLMJournalConflictError(
                "concurrent append-only journal conflict"
            ) from None
        except duckdb.Error:
            self._rollback()
            raise LLMJournalPersistenceError(
                "LLM decision journal append failed"
            ) from None

    def decision_by_request_fingerprint(
        self, request_fingerprint: str
    ) -> LLMDecisionRecord | None:
        return self._read_one(
            "llm_decision_records",
            "request_fingerprint",
            request_fingerprint,
            LLMDecisionRecord,
        )

    def decision_by_id(self, decision_id: str) -> LLMDecisionRecord | None:
        return self._read_one(
            "llm_decision_records", "decision_id", decision_id, LLMDecisionRecord
        )

    def list_decisions(self) -> tuple[LLMDecisionRecord, ...]:
        return self._read_all(
            "llm_decision_records",
            "request_fingerprint, decision_id",
            ("decision_id", "request_fingerprint"),
            LLMDecisionRecord,
        )

    def list_attempts(self) -> tuple[LLMInvocationAttempt, ...]:
        return self._read_all(
            "llm_invocation_attempts",
            "attempt_id",
            ("attempt_id", "request_fingerprint"),
            LLMInvocationAttempt,
        )

    def list_attestations(self) -> tuple[LLMRunAttestation, ...]:
        return self._read_all(
            "llm_run_attestations",
            "occurred_at, attestation_id",
            ("attestation_id", "occurred_at"),
            LLMRunAttestation,
        )

    def _read_one(
        self,
        table: str,
        key_column: str,
        key: str,
        model: type[_JournalModel],
    ) -> _JournalModel | None:
        self._ensure_open()
        if type(key) is not str or not key:
            raise LLMJournalValidationError("journal lookup key must be a nonblank string")
        try:
            row = self._connection.execute(
                f"SELECT payload FROM {table} WHERE {key_column} = ?", [key]
            ).fetchone()
        except duckdb.Error:
            raise LLMJournalPersistenceError(
                "LLM decision journal read failed"
            ) from None
        if row is None:
            return None
        decoded = _decode(row[0], model)
        if getattr(decoded, key_column) != key:
            raise LLMJournalDataError("persisted LLM journal identity is inconsistent")
        return decoded

    def _read_all(
        self,
        table: str,
        order_by: str,
        identity_columns: tuple[str, ...],
        model: type[_JournalModel],
    ) -> tuple[_JournalModel, ...]:
        self._ensure_open()
        try:
            rows = self._connection.execute(
                f"SELECT {', '.join(identity_columns)}, payload "
                f"FROM {table} ORDER BY {order_by}"
            ).fetchall()
        except duckdb.Error:
            raise LLMJournalPersistenceError(
                "LLM decision journal read failed"
            ) from None
        decoded: list[_JournalModel] = []
        for row in rows:
            value = _decode(row[-1], model)
            if any(
                _identity_value(value, column) != row[index]
                for index, column in enumerate(identity_columns)
            ):
                raise LLMJournalDataError(
                    "persisted LLM journal identity is inconsistent"
                )
            decoded.append(value)
        return tuple(decoded)

    def _payload_for(self, table: str, column: str, value: str) -> str | None:
        row = self._connection.execute(
            f"SELECT payload FROM {table} WHERE {column} = ?", [value]
        ).fetchone()
        return None if row is None else row[0]

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except duckdb.Error:
            pass


DuckDBLLMDecisionJournal = LLMDecisionJournal


def _exact_model(
    value: object, model: type[_JournalModel], label: str
) -> _JournalModel:
    if type(value) is not model:
        raise LLMJournalValidationError(f"{label} must be an exact {model.__name__}")
    try:
        _check_model_shape(value)
        fields = {name: getattr(value, name) for name in model.model_fields}
        return model.model_validate(fields, strict=True)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise LLMJournalValidationError(
            f"{label} must be an unpolluted strict {model.__name__}"
        ) from None


def _check_model_shape(value: BaseModel) -> None:
    if set(value.__dict__) != set(type(value).model_fields):
        raise ValueError("polluted model")
    for name in type(value).model_fields:
        item = getattr(value, name)
        if isinstance(item, BaseModel):
            annotation = type(value).model_fields[name].annotation
            if type(item) is not annotation:
                raise TypeError("non-exact nested model")
            _check_model_shape(item)
        elif isinstance(item, tuple):
            if type(item) is not tuple:
                raise TypeError("non-exact tuple")
            for nested in item:
                if isinstance(nested, BaseModel):
                    _check_model_shape(nested)


def _encode(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(payload: object, model: type[_JournalModel]) -> _JournalModel:
    try:
        if type(payload) is not str:
            raise TypeError("payload is not text")
        raw = json.loads(payload)
        if type(raw) is not dict:
            raise TypeError("payload is not an object")
        if model is LLMDecisionRecord:
            raw["selections"] = tuple(_decode_selection(item) for item in raw["selections"])
            raw["started_at"] = _decode_datetime(raw["started_at"])
            raw["ended_at"] = _decode_datetime(raw["ended_at"])
        elif model is LLMInvocationAttempt:
            raw["status"] = LLMDecisionStatus(raw["status"])
            raw["started_at"] = _decode_datetime(raw["started_at"])
            raw["ended_at"] = _decode_datetime(raw["ended_at"])
        else:
            raw["invocation_mode"] = LLMInvocationMode(raw["invocation_mode"])
            raw["decision_ids"] = tuple(raw["decision_ids"])
            raw["occurred_at"] = _decode_datetime(raw["occurred_at"])
        return model.model_validate(raw, strict=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
        raise LLMJournalDataError(
            "persisted LLM journal payload is invalid"
        ) from None


def _decode_selection(raw: object) -> LLMDecisionSelection:
    if type(raw) is not dict:
        raise TypeError("selection is not an object")
    values: dict[str, Any] = dict(raw)
    values["action"] = Side(values["action"])
    return LLMDecisionSelection.model_validate(values, strict=True)


def _decode_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise TypeError("datetime is not text")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _identity_value(value: BaseModel, column: str) -> object:
    identity = getattr(value, column)
    if isinstance(identity, datetime):
        return canonical_datetime(identity)
    return identity
