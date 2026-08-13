from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Never, Protocol, runtime_checkable

from pydantic import ValidationError

from stock_agent.domain import Side
from stock_agent.strategies.llm_contract import (
    LLMDecisionRecord,
    LLMDecisionRequest,
    LLMDecisionResponse,
    LLMDecisionSelection,
    LLMDecisionStatus,
    LLMInvocationAttempt,
    decision_id_for,
    response_digest_for,
)

_ERROR_MESSAGES = {
    LLMDecisionStatus.TIMEOUT: "LLM transport timed out",
    LLMDecisionStatus.TRANSPORT: "LLM transport failed",
    LLMDecisionStatus.SCHEMA: "LLM response schema validation failed",
    LLMDecisionStatus.ENVELOPE: "LLM response violates candidate envelope",
    LLMDecisionStatus.IDENTITY_POLICY: "LLM model identity policy validation failed",
    LLMDecisionStatus.CONFLICT: "LLM replay decision unavailable or conflicting",
    LLMDecisionStatus.AUDIT_PERSISTENCE: "LLM decision audit persistence failed",
}


class LLMProviderError(Exception):
    """Stable, secret-free terminal failure from an LLM decision provider."""

    def __init__(self, code: LLMDecisionStatus) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@runtime_checkable
class LLMDecisionProvider(Protocol):
    """Provider-neutral bounded-decision port."""

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord: ...


@dataclass(frozen=True)
class ExactModelIdentityPolicy:
    policy_id: str
    model_identity: str
    model_revision: str

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value.strip() for value in vars(self).values()):
            raise ValueError("model identity policy values must be nonblank strings")

    def accepts_request(self, request: LLMDecisionRequest) -> bool:
        return request.model_identity_policy_id == self.policy_id

    def accepts_identity(self, model_identity: object, model_revision: object) -> bool:
        return (
            type(model_identity) is str
            and type(model_revision) is str
            and model_identity == self.model_identity
            and model_revision == self.model_revision
        )


@dataclass(frozen=True)
class RawLLMResponse:
    payload: object
    model_identity: str
    model_revision: str


@dataclass(frozen=True)
class InvocationStart:
    attempt_id: str
    started_at: datetime

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not str or not self.attempt_id.strip():
            raise ValueError("attempt ID must be a nonblank string")
        if type(self.started_at) is not datetime or self.started_at.tzinfo is None:
            raise ValueError("attempt start must be an aware datetime")


class RawLLMTransport(Protocol):
    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse: ...


class InvocationBoundary(Protocol):
    def begin(self) -> InvocationStart: ...

    def end_at(self, invocation: InvocationStart) -> datetime: ...

    # Optional: invoked immediately before the transport send boundary. A
    # runtime coordination layer may persist a SEND_INTENT marker here so a
    # crash after this point can be distinguished from a proven pre-send
    # failure. Implementations that omit it simply skip the marker.
    def mark_send_intent(
        self, invocation: InvocationStart, request: LLMDecisionRequest
    ) -> None: ...


class DecisionJournal(Protocol):
    def append_attempt(
        self,
        attempt: LLMInvocationAttempt,
        *,
        decision: LLMDecisionRecord | None = None,
    ) -> None: ...

    def decision_by_request_fingerprint(
        self, request_fingerprint: str
    ) -> LLMDecisionRecord | None: ...


class RecordedLLMDecisionProvider:
    def __init__(
        self,
        *,
        transport: RawLLMTransport,
        journal: DecisionJournal,
        model_policy: ExactModelIdentityPolicy,
        invocation_boundary: InvocationBoundary,
    ) -> None:
        self._transport = transport
        self._journal = journal
        self._model_policy = model_policy
        self._boundary = invocation_boundary

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord:
        request = _exact_request(request)
        invocation = self._begin()
        if not self._model_policy.accepts_request(request):
            self._fail(request, invocation, LLMDecisionStatus.IDENTITY_POLICY)

        marker = getattr(self._boundary, "mark_send_intent", None)
        if marker is not None:
            try:
                marker(invocation, request)
            except Exception:
                self._fail(request, invocation, LLMDecisionStatus.AUDIT_PERSISTENCE)

        try:
            raw = self._transport.invoke(request)
        except TimeoutError:
            self._fail(request, invocation, LLMDecisionStatus.TIMEOUT)
        except Exception:
            self._fail(request, invocation, LLMDecisionStatus.TRANSPORT)

        if type(raw) is not RawLLMResponse:
            self._fail(request, invocation, LLMDecisionStatus.TRANSPORT)
        if not self._model_policy.accepts_identity(raw.model_identity, raw.model_revision):
            self._fail(request, invocation, LLMDecisionStatus.IDENTITY_POLICY)

        try:
            response = _parse_response(raw.payload, request)
        except _ResponseEnvelopeError:
            self._fail(request, invocation, LLMDecisionStatus.ENVELOPE)
        except _ResponseSchemaError:
            self._fail(request, invocation, LLMDecisionStatus.SCHEMA)
        if not _response_fits_request(response, request):
            self._fail(request, invocation, LLMDecisionStatus.ENVELOPE)

        ended_at = self._end(invocation)
        try:
            response_digest = response_digest_for(response)
            decision = LLMDecisionRecord(
                decision_id=decision_id_for(request.request_fingerprint, response_digest),
                request_fingerprint=request.request_fingerprint,
                response_digest=response_digest,
                selections=response.selections,
                config_version=request.config_version,
                model_identity_policy_id=request.model_identity_policy_id,
                prompt_template_id=request.prompt_template_id,
                prompt_template_digest=request.prompt_template_digest,
                model_identity=raw.model_identity,
                model_revision=raw.model_revision,
                provider_response_id=response.provider_response_id,
                started_at=invocation.started_at,
                ended_at=ended_at,
            )
            attempt = LLMInvocationAttempt(
                attempt_id=invocation.attempt_id,
                request_fingerprint=request.request_fingerprint,
                status=LLMDecisionStatus.SUCCESS,
                response_digest=response_digest,
                decision_id=decision.decision_id,
                provider_response_id=response.provider_response_id,
                started_at=invocation.started_at,
                ended_at=ended_at,
            )
        except (TypeError, ValueError, ValidationError):
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None

        self._append(attempt, decision=decision)
        return decision

    def _begin(self) -> InvocationStart:
        try:
            invocation = self._boundary.begin()
            if type(invocation) is not InvocationStart:
                raise TypeError("non-exact invocation start")
            return invocation
        except Exception:
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None

    def _end(self, invocation: InvocationStart) -> datetime:
        try:
            ended_at = self._boundary.end_at(invocation)
            if type(ended_at) is not datetime or ended_at.tzinfo is None:
                raise TypeError("attempt end must be an aware datetime")
            return ended_at
        except Exception:
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None

    def _fail(
        self,
        request: LLMDecisionRequest,
        invocation: InvocationStart,
        status: LLMDecisionStatus,
    ) -> Never:
        ended_at = self._end(invocation)
        try:
            attempt = LLMInvocationAttempt(
                attempt_id=invocation.attempt_id,
                request_fingerprint=request.request_fingerprint,
                status=status,
                started_at=invocation.started_at,
                ended_at=ended_at,
            )
        except (TypeError, ValueError, ValidationError):
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None
        self._append(attempt)
        raise LLMProviderError(status)

    def _append(
        self,
        attempt: LLMInvocationAttempt,
        *,
        decision: LLMDecisionRecord | None = None,
    ) -> None:
        try:
            self._journal.append_attempt(attempt, decision=decision)
        except Exception:
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None


class ReplayLLMDecisionProvider:
    def __init__(
        self,
        *,
        journal: DecisionJournal,
        model_policy: ExactModelIdentityPolicy,
    ) -> None:
        self._journal = journal
        self._model_policy = model_policy

    def decide(self, request: LLMDecisionRequest) -> LLMDecisionRecord:
        request = _exact_request(request)
        try:
            record = self._journal.decision_by_request_fingerprint(request.request_fingerprint)
        except Exception:
            raise LLMProviderError(LLMDecisionStatus.AUDIT_PERSISTENCE) from None
        if record is None:
            raise LLMProviderError(LLMDecisionStatus.CONFLICT)
        if (
            record.config_version != request.config_version
            or record.prompt_template_id != request.prompt_template_id
            or record.prompt_template_digest != request.prompt_template_digest
        ):
            raise LLMProviderError(LLMDecisionStatus.CONFLICT)
        if (
            not self._model_policy.accepts_request(request)
            or record.model_identity_policy_id != self._model_policy.policy_id
            or not self._model_policy.accepts_identity(record.model_identity, record.model_revision)
        ):
            raise LLMProviderError(LLMDecisionStatus.IDENTITY_POLICY)
        if not _selections_fit_candidates(record.selections, request):
            raise LLMProviderError(LLMDecisionStatus.ENVELOPE)
        return record


class _ResponseSchemaError(Exception):
    pass


class _ResponseEnvelopeError(Exception):
    pass


def _exact_request(request: object) -> LLMDecisionRequest:
    if type(request) is not LLMDecisionRequest:
        raise LLMProviderError(LLMDecisionStatus.SCHEMA)
    try:
        if set(request.__dict__) != set(LLMDecisionRequest.model_fields):
            raise ValueError("polluted request")
        fields = {name: getattr(request, name) for name in LLMDecisionRequest.model_fields}
        return LLMDecisionRequest.model_validate(fields, strict=True)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise LLMProviderError(LLMDecisionStatus.SCHEMA) from None


def _parse_response(payload: object, request: LLMDecisionRequest) -> LLMDecisionResponse:
    try:
        raw = _decode_payload(payload)
        expected_top = {
            "schema_version",
            "request_fingerprint",
            "selections",
            "provider_response_id",
        }
        required_top = expected_top - {"provider_response_id"}
        if not required_top <= set(raw) <= expected_top:
            raise _ResponseSchemaError
        raw_selections = raw["selections"]
        if type(raw_selections) is not list:
            raise _ResponseSchemaError
        selections = tuple(_parse_selection(item) for item in raw_selections)
        if raw["request_fingerprint"] != request.request_fingerprint or tuple(
            item.symbol for item in selections
        ) != tuple(candidate.symbol for candidate in request.candidates):
            raise _ResponseEnvelopeError
        provider_response_id = raw.get("provider_response_id")
        if provider_response_id is not None and type(provider_response_id) is not str:
            raise _ResponseSchemaError
        return LLMDecisionResponse.model_validate(
            {
                "schema_version": raw["schema_version"],
                "request_fingerprint": raw["request_fingerprint"],
                "selections": selections,
                "provider_response_id": provider_response_id,
            },
            strict=True,
        )
    except (_ResponseSchemaError, _ResponseEnvelopeError):
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
        raise _ResponseSchemaError from None


def _decode_payload(payload: object) -> dict[str, object]:
    if type(payload) is str:
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    elif type(payload) is dict:
        decoded = payload
    else:
        raise _ResponseSchemaError
    if type(decoded) is not dict:
        raise _ResponseSchemaError
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _ResponseSchemaError
        value[key] = item
    return value


def _parse_selection(raw: object) -> LLMDecisionSelection:
    if type(raw) is not dict or set(raw) != {
        "symbol",
        "action",
        "confidence",
        "thesis",
        "invalidation",
    }:
        raise _ResponseSchemaError
    if (
        type(raw["symbol"]) is not str
        or type(raw["action"]) is not str
        or type(raw["confidence"]) is not int
        or type(raw["thesis"]) is not str
        or type(raw["invalidation"]) is not str
    ):
        raise _ResponseSchemaError
    try:
        action = Side(raw["action"])
    except ValueError:
        raise _ResponseSchemaError from None
    return LLMDecisionSelection.model_validate({**raw, "action": action}, strict=True)


def _response_fits_request(response: LLMDecisionResponse, request: LLMDecisionRequest) -> bool:
    return (
        response.request_fingerprint == request.request_fingerprint
        and _selections_fit_candidates(response.selections, request)
    )


def _selections_fit_candidates(
    selections: tuple[LLMDecisionSelection, ...], request: LLMDecisionRequest
) -> bool:
    if tuple(item.symbol for item in selections) != tuple(
        item.symbol for item in request.candidates
    ):
        return False
    return all(
        selection.action in {target.action for target in candidate.action_targets}
        for selection, candidate in zip(selections, request.candidates, strict=True)
    )
