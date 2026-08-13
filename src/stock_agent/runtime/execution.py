from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from stock_agent.execution.models import Fill, FillStatus
from stock_agent.runtime.capability_gates import (
    CapabilityGates,
    ExecutionAuthorityBlockedError,
)
from stock_agent.runtime.ledger_store import LedgerStore
from stock_agent.runtime.models import RuntimeModel
from stock_agent.runtime.store import RuntimeStore

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ExecutionOutcome(StrEnum):
    BLOCKED = "BLOCKED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class ExecutionResult(RuntimeModel):
    order_id: NonEmptyStr
    outcome: ExecutionOutcome
    execution_id: str | None = None


class ExecutionAdapter:
    """Durable execution/booking boundary behind the execution-authority gate.

    The gate is fail-closed: while no validated execution-open authority exists,
    every order stays ``BLOCKED`` and never produces a fill or ledger event. When
    a validated authority is present, a single injected executor produces a
    ``Fill`` and the adapter books the matching ledger batch and terminal
    execution atomically.
    """

    def __init__(
        self,
        *,
        store: RuntimeStore,
        ledger_store: LedgerStore,
        gates: CapabilityGates,
        executor: Callable[[str, date], Fill],
    ) -> None:
        if type(store) is not RuntimeStore:
            raise TypeError("store must be exactly RuntimeStore")
        if type(ledger_store) is not LedgerStore:
            raise TypeError("ledger_store must be exactly LedgerStore")
        if type(gates) is not CapabilityGates:
            raise TypeError("gates must be exactly CapabilityGates")
        if not callable(executor):
            raise TypeError("executor must be callable")
        self._store = store
        self._ledger_store = ledger_store
        self._gates = gates
        self._executor = executor

    def execute(
        self, *, order_id: str, intended_session_date: date, now: datetime
    ) -> ExecutionResult:
        if type(order_id) is not str or not order_id:
            raise ValueError("order_id must be a nonblank string")
        if type(intended_session_date) is not date:
            raise TypeError("intended_session_date must be a plain date")

        # Fail-closed: no validated authority, no fill.
        try:
            self._gates.require_execution_open()
        except ExecutionAuthorityBlockedError:
            return ExecutionResult(order_id=order_id, outcome=ExecutionOutcome.BLOCKED)

        fill = self._executor(order_id, intended_session_date)
        if fill.status is FillStatus.REJECTED:
            self._store.finalize_rejection(order_id=order_id, now=now)
            return ExecutionResult(order_id=order_id, outcome=ExecutionOutcome.REJECTED)

        # A fill books its ledger batch and terminal execution in one durable store.
        self._store.finalize_fill(order_id=order_id, now=now)
        return ExecutionResult(
            order_id=order_id,
            outcome=ExecutionOutcome.FILLED,
            execution_id=fill.order_id,
        )
