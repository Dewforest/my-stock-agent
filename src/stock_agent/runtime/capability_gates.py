from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from stock_agent.runtime.models import RuntimeModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9-]+-sha256:[0-9a-f]{64}$"),
]

EXECUTION_OPEN_AND_CN_SESSION_STATE = "execution_open_and_cn_session_state"
CORPORATE_ACTIONS = "corporate_actions"
ANNUAL_CALENDARS = "annual_calendars"
LAUNCHAGENT_KEYCHAIN_ACL = "launchagent_keychain_acl"


class AuthorityVerdict(StrEnum):
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    PARTIAL_WITH_CN_PRICE_LIMIT_INVALIDATED = "PARTIAL_WITH_CN_PRICE_LIMIT_INVALIDATED"
    INVALIDATED = "INVALIDATED"
    NOT_RUN = "NOT_RUN"


class AuthorityRecord(RuntimeModel):
    authority: NonEmptyStr
    verdict: AuthorityVerdict
    version: NonEmptyStr
    record_digest: Digest


class ExecutionAuthorityBlockedError(Exception):
    """Raised when no validated execution-open / CN-session-state authority exists."""


class CorporateActionAuthorityBlockedError(Exception):
    """Raised when no validated corporate-action authority exists."""


class CapabilityGates:
    """Fail-closed capability gates over immutable authority verdict records.

    Only a verdict of ``VALIDATED`` admits the corresponding capability. Every
    recorded Phase 0 verdict is ``PARTIAL``/``INVALIDATED``/``NOT_RUN``, so all
    execution and position-bearing gates are closed until a later authority
    amendment records a new version/digest with a ``VALIDATED`` verdict.
    """

    def __init__(self, records: tuple[AuthorityRecord, ...]) -> None:
        if type(records) is not tuple:
            raise TypeError("records must be an exact tuple")
        if any(type(item) is not AuthorityRecord for item in records):
            raise TypeError("records must contain exact AuthorityRecord values")
        authorities = tuple(record.authority for record in records)
        if len(authorities) != len(set(authorities)):
            raise ValueError("authority records must be unique by authority")
        self._records = {record.authority: record for record in records}

    def verdict_for(self, authority: str) -> AuthorityVerdict | None:
        record = self._records.get(authority)
        return None if record is None else record.verdict

    def is_validated(self, authority: str) -> bool:
        return self.verdict_for(authority) is AuthorityVerdict.VALIDATED

    def require_execution_open(self) -> None:
        if not self.is_validated(EXECUTION_OPEN_AND_CN_SESSION_STATE):
            raise ExecutionAuthorityBlockedError(
                "no validated execution-open authority; automatic fills remain blocked"
            )

    def require_cn_session_state(self) -> None:
        if not self.is_validated(EXECUTION_OPEN_AND_CN_SESSION_STATE):
            raise ExecutionAuthorityBlockedError(
                "no validated CN suspension/price-limit authority; CN fills remain blocked"
            )

    def require_corporate_actions(self) -> None:
        if not self.is_validated(CORPORATE_ACTIONS):
            raise CorporateActionAuthorityBlockedError(
                "no validated corporate-action authority; position-bearing activation blocked"
            )
