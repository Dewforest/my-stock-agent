from __future__ import annotations

import pytest

from stock_agent.runtime.capability_gates import (
    CORPORATE_ACTIONS,
    AuthorityRecord,
    AuthorityVerdict,
    CapabilityGates,
    CorporateActionAuthorityBlockedError,
)


def record(
    authority: str, verdict: AuthorityVerdict, version: str = "2026-08-12/v1"
) -> AuthorityRecord:
    return AuthorityRecord(
        authority=authority,
        verdict=verdict,
        version=version,
        record_digest="verdict-sha256:" + "a" * 64,
    )


def test_partial_corporate_action_blocks_position_activation() -> None:
    gates = CapabilityGates((record(CORPORATE_ACTIONS, AuthorityVerdict.PARTIAL),))
    assert not gates.is_validated(CORPORATE_ACTIONS)
    with pytest.raises(CorporateActionAuthorityBlockedError):
        gates.require_corporate_actions()


def test_validated_corporate_action_admits() -> None:
    gates = CapabilityGates((record(CORPORATE_ACTIONS, AuthorityVerdict.VALIDATED),))
    gates.require_corporate_actions()


def test_provider_cannot_be_promoted_to_authority_by_configuration() -> None:
    # Even a "validated" claim without a matching recorded verdict is not admitted.
    gates = CapabilityGates((record(CORPORATE_ACTIONS, AuthorityVerdict.PARTIAL),))
    assert not gates.is_validated(CORPORATE_ACTIONS)


def test_authority_record_is_immutable() -> None:
    rec = record(CORPORATE_ACTIONS, AuthorityVerdict.PARTIAL)
    with pytest.raises((TypeError, ValueError)):
        rec.model_copy(update={"verdict": AuthorityVerdict.VALIDATED})


def test_later_amendment_requires_new_version_and_digest() -> None:
    # A new validated verdict is a distinct record with its own version/digest,
    # replacing (not mutating) the prior verdict.
    amended = record(CORPORATE_ACTIONS, AuthorityVerdict.VALIDATED, version="2026-09-01/v2")
    gates = CapabilityGates((amended,))
    assert gates.is_validated(CORPORATE_ACTIONS)
