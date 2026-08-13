from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from stock_agent.audit.canonical import canonical_datetime, tagged_sha256
from stock_agent.runtime.models import RuntimeModel
from stock_agent.runtime.store import (
    DecisionInvocationStatus,
    RuntimeStore,
    StoreError,
)
from stock_agent.strategies.llm_contract import LLMDecisionRecord, LLMDecisionRequest
from stock_agent.strategies.llm_provider import (
    DecisionJournal,
    ExactModelIdentityPolicy,
    InvocationStart,
    LLMProviderError,
    RawLLMTransport,
    RecordedLLMDecisionProvider,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DecisionOutcome(StrEnum):
    RESUMED = "RESUMED"
    DECIDED = "DECIDED"
    FAILED_PRE_SEND = "FAILED_PRE_SEND"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class DecisionResult(RuntimeModel):
    outcome: DecisionOutcome
    record: LLMDecisionRecord | None = None
    error_code: str | None = None


class RuntimeInvocationBoundary:
    """Persists a SEND_INTENT marker immediately before the transport send."""

    def __init__(self, store: RuntimeStore, run_id: str, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._run_id = run_id
        self._clock = clock

    def begin(self) -> InvocationStart:
        started_at = self._clock()
        attempt_id = tagged_sha256(
            "llm-invocation-attempt", (self._run_id, canonical_datetime(started_at))
        )
        return InvocationStart(attempt_id=attempt_id, started_at=started_at)

    def mark_send_intent(self, invocation: InvocationStart, request: LLMDecisionRequest) -> None:
        self._store.mark_send_intent(self._run_id, request.request_fingerprint, self._clock())

    def end_at(self, invocation: InvocationStart) -> datetime:
        return self._clock()


class DecisionCoordinator:
    """Crash-safe bounded-decision coordination for one market run.

    Queries the LLM journal by request fingerprint before any Keychain or
    transport access, persists a SEND_INTENT marker immediately before the
    transport send boundary, and only auto-recalls through fingerprint replay
    after a canonical decision has committed.
    """

    def __init__(
        self,
        *,
        store: RuntimeStore,
        journal: DecisionJournal,
        model_policy: ExactModelIdentityPolicy,
        credential_provider: Callable[[], object],
        transport_factory: Callable[[object], RawLLMTransport],
    ) -> None:
        if type(store) is not RuntimeStore:
            raise TypeError("store must be exactly RuntimeStore")
        if type(model_policy) is not ExactModelIdentityPolicy:
            raise TypeError("model_policy must be exactly ExactModelIdentityPolicy")
        if not callable(credential_provider) or not callable(transport_factory):
            raise TypeError("credential_provider and transport_factory must be callable")
        self._store = store
        self._journal = journal
        self._model_policy = model_policy
        self._credential_provider = credential_provider
        self._transport_factory = transport_factory

    def run_decision(
        self, *, run_id: str, request: LLMDecisionRequest, now: datetime
    ) -> DecisionResult:
        if type(run_id) is not str or not run_id:
            raise StoreError("run_decision requires a nonblank run id")
        if type(request) is not LLMDecisionRequest:
            raise StoreError("run_decision requires an exact LLMDecisionRequest")
        if type(now) is not datetime or now.tzinfo is None:
            raise StoreError("run_decision requires an aware datetime")

        # 1. Fingerprint-first lookup: zero transport / Keychain access.
        try:
            existing = self._journal.decision_by_request_fingerprint(request.request_fingerprint)
        except Exception:
            raise StoreError("LLM journal read failed") from None
        if existing is not None:
            return DecisionResult(outcome=DecisionOutcome.RESUMED, record=existing)

        # 2. A prior SEND_INTENT without a canonical decision is ambiguity.
        invocation = self._store.get_decision_invocation(run_id)
        if (
            invocation is not None
            and invocation[0] == DecisionInvocationStatus.SEND_INTENT_RECORDED.value
        ):
            self._store.mark_needs_reconciliation(run_id, now)
            return DecisionResult(outcome=DecisionOutcome.NEEDS_RECONCILIATION)

        # 3. Credential read is a proven pre-send failure if it fails.
        try:
            credential = self._credential_provider()
        except Exception as error:
            return DecisionResult(
                outcome=DecisionOutcome.FAILED_PRE_SEND, error_code=_safe_code(error)
            )

        # 4. Bounded invocation. The boundary persists SEND_INTENT right before send.
        boundary = RuntimeInvocationBoundary(self._store, run_id, lambda: now)
        transport = self._transport_factory(credential)
        recorded = RecordedLLMDecisionProvider(
            transport=transport,
            journal=self._journal,
            model_policy=self._model_policy,
            invocation_boundary=boundary,
        )
        try:
            record = recorded.decide(request)
        except LLMProviderError as error:
            invocation = self._store.get_decision_invocation(run_id)
            if (
                invocation is not None
                and invocation[0] == DecisionInvocationStatus.SEND_INTENT_RECORDED.value
            ):
                self._store.mark_needs_reconciliation(run_id, now)
                return DecisionResult(outcome=DecisionOutcome.NEEDS_RECONCILIATION)
            return DecisionResult(
                outcome=DecisionOutcome.FAILED_PRE_SEND, error_code=error.code.value
            )

        # 5. Canonical decision committed: persist and replay thereafter.
        self._store.record_decision(run_id, record.decision_id, now)
        return DecisionResult(outcome=DecisionOutcome.DECIDED, record=record)

    def reconcile_abandon(self, *, run_id: str, operator: str, reason: str, now: datetime) -> None:
        invocation = self._store.get_decision_invocation(run_id)
        if invocation is None:
            raise StoreError("reconcile abandon targets an unknown decision invocation")
        self._store.abandon_decision(
            run_id=run_id,
            operator=operator,
            reason=reason,
            request_fingerprint=invocation[1],
            now=now,
        )


def _safe_code(error: BaseException) -> str:
    # Credential failures surface only a stable, secret-free code.
    return "credential"
