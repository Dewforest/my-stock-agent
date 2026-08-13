from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import StringConstraints

from stock_agent.audit.canonical import tagged_sha256
from stock_agent.domain import Market
from stock_agent.runtime.models import RuntimeModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UniverseDigest = Annotated[
    str,
    StringConstraints(pattern=r"^universe-sha256:[0-9a-f]{64}$"),
]

_RUN_SCHEMA = "paper-run/v1"


class RunIdentity(RuntimeModel):
    """Deterministic identity of one market/account post-close decision cycle.

    Carries only fields that fix a run's business identity and configuration
    fingerprint. It deliberately excludes phase/state, wake time, PID, hostname,
    attempt number, credentials, retries, errors, and report generation time.
    """

    schema_version: Literal["paper-run/v1"]
    market: Market
    account_id: NonEmptyStr
    close_session_date: date
    strategy_id: NonEmptyStr
    config_version: NonEmptyStr
    universe_id: NonEmptyStr
    universe_digest: UniverseDigest
    calendar_version: NonEmptyStr
    model_policy_version: NonEmptyStr


def run_id_for(identity: RunIdentity) -> str:
    _exact(identity)
    return tagged_sha256(
        "paper-run",
        (
            _RUN_SCHEMA,
            identity.market.value,
            identity.account_id,
            identity.close_session_date.isoformat(),
            identity.strategy_id,
            identity.config_version,
            identity.universe_id,
            identity.universe_digest,
            identity.calendar_version,
            identity.model_policy_version,
        ),
    )


def run_key_for(identity: RunIdentity) -> str:
    """Business identity, independent of any configuration fingerprint."""
    _exact(identity)
    return tagged_sha256(
        "paper-run-key",
        (
            _RUN_SCHEMA,
            identity.market.value,
            identity.account_id,
            identity.close_session_date.isoformat(),
            identity.strategy_id,
        ),
    )


def run_config_digest_for(identity: RunIdentity) -> str:
    """Configuration fingerprint. Different fingerprint under the same run key
    is a conflict, not a distinct run."""
    _exact(identity)
    return tagged_sha256(
        "paper-run-config",
        (
            identity.config_version,
            identity.universe_id,
            identity.universe_digest,
            identity.calendar_version,
            identity.model_policy_version,
        ),
    )


def _exact(identity: object) -> RunIdentity:
    if type(identity) is not RunIdentity:
        raise TypeError("run identity must be an exact RunIdentity")
    if set(identity.__dict__) != set(RunIdentity.model_fields):
        raise ValueError("RunIdentity is polluted")
    values = {name: getattr(identity, name) for name in RunIdentity.model_fields}
    return RunIdentity.model_validate(values, strict=True)
