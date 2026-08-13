from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, StringConstraints

from stock_agent.domain import Market, Side
from stock_agent.runtime.models import RuntimeModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9-]+-sha256:[0-9a-f]{64}$"),
]


class RunPhase(StrEnum):
    DISCOVERED = "DISCOVERED"
    SKIPPED_NOT_SESSION = "SKIPPED_NOT_SESSION"
    SKIPPED_TOO_EARLY = "SKIPPED_TOO_EARLY"
    MISSED_DECISION_DEADLINE = "MISSED_DECISION_DEADLINE"
    CLAIMED = "CLAIMED"
    FAILED_BEFORE_DECISION = "FAILED_BEFORE_DECISION"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    SNAPSHOT_FROZEN = "SNAPSHOT_FROZEN"
    CREDENTIAL_READY = "CREDENTIAL_READY"
    FAILED_DECISION_PRE_SEND = "FAILED_DECISION_PRE_SEND"
    SEND_INTENT_RECORDED = "SEND_INTENT_RECORDED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    DECISION_RECORDED = "DECISION_RECORDED"
    FAILED_AFTER_DECISION = "FAILED_AFTER_DECISION"
    ORDERS_PERSISTED = "ORDERS_PERSISTED"
    SUCCEEDED = "SUCCEEDED"


class AttemptKind(StrEnum):
    PRIMARY = "PRIMARY"
    RECOVERY = "RECOVERY"


class AttemptPhase(StrEnum):
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class KillSwitchScope(StrEnum):
    GLOBAL = "GLOBAL"
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"


TERMINAL_RUN_PHASES = frozenset(
    {
        RunPhase.SKIPPED_NOT_SESSION,
        RunPhase.SKIPPED_TOO_EARLY,
        RunPhase.MISSED_DECISION_DEADLINE,
        RunPhase.FAILED_BEFORE_DECISION,
        RunPhase.DATA_INCOMPLETE,
        RunPhase.FAILED_DECISION_PRE_SEND,
        RunPhase.NEEDS_RECONCILIATION,
        RunPhase.FAILED_AFTER_DECISION,
        RunPhase.SUCCEEDED,
    }
)

# Monotonic forward transitions of the frozen run state machine. Terminal phases
# are absent and therefore admit no outgoing transition.
_RUN_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.DISCOVERED: frozenset(
        {
            RunPhase.SKIPPED_NOT_SESSION,
            RunPhase.SKIPPED_TOO_EARLY,
            RunPhase.MISSED_DECISION_DEADLINE,
            RunPhase.CLAIMED,
        }
    ),
    RunPhase.CLAIMED: frozenset(
        {
            RunPhase.FAILED_BEFORE_DECISION,
            RunPhase.DATA_INCOMPLETE,
            RunPhase.SNAPSHOT_FROZEN,
        }
    ),
    RunPhase.SNAPSHOT_FROZEN: frozenset({RunPhase.CREDENTIAL_READY}),
    RunPhase.CREDENTIAL_READY: frozenset(
        {RunPhase.FAILED_DECISION_PRE_SEND, RunPhase.SEND_INTENT_RECORDED}
    ),
    RunPhase.SEND_INTENT_RECORDED: frozenset(
        {
            RunPhase.FAILED_DECISION_PRE_SEND,
            RunPhase.NEEDS_RECONCILIATION,
            RunPhase.DECISION_RECORDED,
        }
    ),
    RunPhase.DECISION_RECORDED: frozenset(
        {RunPhase.FAILED_AFTER_DECISION, RunPhase.ORDERS_PERSISTED}
    ),
    RunPhase.ORDERS_PERSISTED: frozenset({RunPhase.SUCCEEDED}),
}


def is_terminal_phase(phase: RunPhase) -> bool:
    return phase in TERMINAL_RUN_PHASES


def is_legal_transition(from_phase: RunPhase, to_phase: RunPhase) -> bool:
    return to_phase in _RUN_TRANSITIONS.get(from_phase, frozenset())


class RuntimeRun(RuntimeModel):
    run_id: Digest
    run_key: Digest
    config_digest: Digest
    phase: RunPhase
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def is_terminal(self) -> bool:
        return is_terminal_phase(self.phase)


class RuntimeAttempt(RuntimeModel):
    attempt_id: Digest
    run_id: Digest
    attempt_number: int
    kind: AttemptKind
    phase: AttemptPhase
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None

    def validate_run_link(self, run: RuntimeRun) -> Self:
        if self.run_id != run.run_id:
            raise ValueError("attempt run_id does not match its run")
        return self


class KillSwitch(RuntimeModel):
    scope: KillSwitchScope
    scope_value: str
    enabled: bool
    set_by: NonEmptyStr
    set_at: AwareDatetime

    def validate_scope_value(self) -> Self:
        if self.scope is KillSwitchScope.GLOBAL:
            if self.scope_value != "":
                raise ValueError("GLOBAL kill switch must carry an empty scope value")
        elif not self.scope_value:
            raise ValueError("scoped kill switch requires a nonblank scope value")
        return self


class OrderStatus(StrEnum):
    PENDING = "PENDING"


class PendingOrder(RuntimeModel):
    order_id: str
    run_id: str
    symbol: NonEmptyStr
    market: Market
    side: Side
    target_weight: Decimal
    intended_session_date: date
    ordinal: int
    status: OrderStatus = OrderStatus.PENDING

    def validate_order(self) -> Self:
        if self.target_weight < 0 or self.target_weight > 1:
            raise ValueError("target_weight must be a unit decimal")
        if self.ordinal <= 0:
            raise ValueError("ordinal must be positive")
        if self.side is Side.SELL and self.target_weight != 0:
            raise ValueError("SELL orders must have zero target weight")
        return self


class RiskResultEnvelope(RuntimeModel):
    run_id: str
    symbol: NonEmptyStr
    status: NonEmptyStr
    approved_target_weight: Decimal | None
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
