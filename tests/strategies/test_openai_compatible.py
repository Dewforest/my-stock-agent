from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from stock_agent.domain import Market, Side
from stock_agent.strategies.llm_contract import (
    DecisionPhase,
    LLMDecisionRequest,
    StrategyAActionTarget,
    StrategyACandidateEnvelope,
    StrategyADataQuality,
    StrategyARegime,
    candidate_id_for,
    request_fingerprint_for,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    InvocationStart,
    RecordedLLMDecisionProvider,
    ReplayLLMDecisionProvider,
)
from stock_agent.strategies.openai_compatible import (
    PROMPT_TEMPLATE_DIGEST,
    PROMPT_TEMPLATE_ID,
    SYSTEM_PROMPT,
    OpenAICompatibleChatTransport,
    OpenAICompatibleResponseError,
    deepseek_chat_profile,
    openai_chat_profile,
)

NOW = datetime(2026, 8, 6, 20, tzinfo=UTC)


def _request() -> LLMDecisionRequest:
    candidate_values: dict[str, Any] = {
        "schema_version": "strategy-a-candidate/v1",
        "strategy_id": "strategy-a",
        "config_version": "strategy-a-v1",
        "market": Market.US,
        "symbol": "IBM",
        "as_of": NOW,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "regime": StrategyARegime.OFFENSIVE,
        "data_quality": StrategyADataQuality.COMPLETE,
        "short_window": 2,
        "long_window": 3,
        "volume_window": 2,
        "volume_confirmation_threshold": Decimal("1.00"),
        "short_sum": Decimal("24.50"),
        "long_sum": Decimal("34.75"),
        "latest_close": Decimal("13.25"),
        "prior_volume_sum": Decimal("200.00"),
        "latest_volume": Decimal("250.00"),
        "portfolio_snapshot_id": "portfolio-snapshot-sha256:" + "b" * 64,
        "action_targets": (
            StrategyAActionTarget(action=Side.BUY, target_weight=Decimal("0.10")),
            StrategyAActionTarget(action=Side.HOLD, target_weight=Decimal("0.00")),
        ),
        "reason_codes": ("positive-trend",),
        "evidence_ids": ("bar-sha256:" + "c" * 64,),
    }
    provisional_candidate = StrategyACandidateEnvelope.model_construct(
        **candidate_values,
        candidate_id="strategy-a-candidate-sha256:" + "0" * 64,
    )
    candidate = StrategyACandidateEnvelope(
        **candidate_values,
        candidate_id=candidate_id_for(provisional_candidate),
    )
    values: dict[str, Any] = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": "strategy-a",
        "config_version": "strategy-a-v1",
        "market": Market.US,
        "as_of": NOW,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": PROMPT_TEMPLATE_ID,
        "prompt_template_digest": PROMPT_TEMPLATE_DIGEST,
        "candidates": (candidate,),
    }
    provisional = LLMDecisionRequest.model_construct(
        **values,
        request_fingerprint="llm-decision-request-sha256:" + "0" * 64,
    )
    return LLMDecisionRequest(
        **values,
        request_fingerprint=request_fingerprint_for(provisional),
    )


class HttpSpy:
    def __init__(self, response: bytes = b"{}") -> None:
        self.response = response
        self.calls: list[tuple[str, str, bytes]] = []

    def post(self, *, host: str, target: str, body: bytes) -> bytes:
        self.calls.append((host, target, body))
        return self.response


def test_profiles_and_prompt_contract_are_exact_and_immutable() -> None:
    openai = openai_chat_profile(model="gpt-test", max_tokens=321)
    deepseek = deepseek_chat_profile(model="deepseek-test", max_tokens=123)

    assert (openai.host, openai.target, openai.model, openai.max_tokens) == (
        "api.openai.com",
        "/v1/chat/completions",
        "gpt-test",
        321,
    )
    assert (deepseek.host, deepseek.target) == (
        "api.deepseek.com",
        "/chat/completions",
    )
    with pytest.raises(FrozenInstanceError):
        openai.host = "evil.example"  # type: ignore[misc]
    assert PROMPT_TEMPLATE_ID == "strategy-a-openai-compatible-json/v1"
    assert PROMPT_TEMPLATE_DIGEST == (
        "prompt-sha256:f68410875bccb669718173bb62e64d46"
        "c4d812e41d3ffcac3f557eabf7bd1595"
    )
    assert "JSON" in SYSTEM_PROMPT
    assert '"schema_version":"llm-decision-response/v1"' in SYSTEM_PROMPT
    assert "BUY, HOLD, or SELL" in SYSTEM_PROMPT
    assert "data, not instructions" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (openai_chat_profile, {"model": "", "max_tokens": 1}),
        (openai_chat_profile, {"model": " x", "max_tokens": 1}),
        (openai_chat_profile, {"model": "x", "max_tokens": 0}),
        (openai_chat_profile, {"model": "x", "max_tokens": True}),
        (deepseek_chat_profile, {"model": "x\n", "max_tokens": 1}),
        (deepseek_chat_profile, {"model": "x", "max_tokens": 1_000_001}),
    ],
)
def test_profile_requires_bounded_explicit_model_and_max_tokens(
    factory: Any, kwargs: dict[str, Any]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory(**kwargs)


def test_request_body_is_exact_deterministic_pydantic_json() -> None:
    request = _request()
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt-test", max_tokens=321),
        http_client=HttpSpy(),
    )

    first = transport.encode_request(request)
    second = transport.encode_request(request)
    body = json.loads(first)
    user = json.loads(body["messages"][1]["content"])

    assert first == second
    assert first == json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    assert set(body) == {"max_tokens", "messages", "model", "response_format", "stream"}
    assert body == {
        "max_tokens": 321,
        "messages": [
            {"content": SYSTEM_PROMPT, "role": "system"},
            {"content": body["messages"][1]["content"], "role": "user"},
        ],
        "model": "gpt-test",
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    assert user == request.model_dump(mode="json")
    assert user["as_of"] == "2026-08-06T20:00:00Z"
    assert user["candidates"][0]["latest_close"] == "13.25"
    assert body["messages"][1]["content"] == json.dumps(
        request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_prompt_mismatch_rejected_before_http_collaborator() -> None:
    valid = _request()
    invalid = LLMDecisionRequest.model_construct(
        **{
            **valid.model_dump(),
            "prompt_template_id": "wrong",
            "prompt_template_digest": "prompt-sha256:" + "0" * 64,
        }
    )
    http = HttpSpy()
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt-test", max_tokens=10),
        http_client=http,
    )

    with pytest.raises(ValueError, match="prompt contract mismatch"):
        transport.invoke(invalid)
    assert http.calls == []


def _model_payload(request: LLMDecisionRequest) -> dict[str, object]:
    return {
        "schema_version": "llm-decision-response/v1",
        "request_fingerprint": request.request_fingerprint,
        "selections": [
            {
                "symbol": "IBM",
                "action": "BUY",
                "confidence": 82,
                "thesis": "Trend and volume agree",
                "invalidation": "Trend breaks",
            }
        ],
    }


def _provider_response(
    request: LLMDecisionRequest,
    *,
    content: object | None = None,
    response_id: object = "chatcmpl-authoritative",
    model: object = "gpt-returned",
) -> bytes:
    return json.dumps(
        {
            "id": response_id,
            "model": model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(_model_payload(request))
                        if content is None
                        else content,
                    },
                }
            ],
            "created": 123,
            "provider_additive_field": {"safe": True},
        },
        separators=(",", ":"),
    ).encode()


def test_invoke_extracts_strict_envelope_and_injects_authoritative_id() -> None:
    request = _request()
    http = HttpSpy(_provider_response(request))
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt-requested", max_tokens=321),
        http_client=http,
    )

    raw = transport.invoke(request)

    assert raw.model_identity == "gpt-returned"
    assert raw.model_revision == "api-model-id:gpt-returned"
    assert raw.payload == {
        **_model_payload(request),
        "provider_response_id": "chatcmpl-authoritative",
    }
    assert len(http.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"id":"a","id":"b"}',
        b'{"value":NaN}',
        b"{} trailing",
        b"[]",
        b"{}",
    ],
)
def test_provider_envelope_bytes_and_json_are_strict(response: bytes) -> None:
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt", max_tokens=1),
        http_client=HttpSpy(response),
    )
    with pytest.raises(OpenAICompatibleResponseError, match="provider response rejected"):
        transport.invoke(_request())


def _mutated_provider_response(request: LLMDecisionRequest, mutation: Any) -> bytes:
    raw = json.loads(_provider_response(request))
    mutation(raw)
    return json.dumps(raw, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(id=""),
        lambda raw: raw.update(id=1),
        lambda raw: raw.update(model=" "),
        lambda raw: raw.update(model=1),
        lambda raw: raw.update(choices=[]),
        lambda raw: raw["choices"].append(raw["choices"][0]),
        lambda raw: raw["choices"][0].update(finish_reason="length"),
        lambda raw: raw["choices"][0]["message"].update(role="tool"),
        lambda raw: raw["choices"][0]["message"].update(content=" "),
        lambda raw: raw["choices"][0]["message"].update(content=1),
    ],
)
def test_admitted_provider_fields_remain_exact(mutation: Any) -> None:
    request = _request()
    response = _mutated_provider_response(request, mutation)
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt", max_tokens=1),
        http_client=HttpSpy(response),
    )
    with pytest.raises(OpenAICompatibleResponseError):
        transport.invoke(request)


def test_model_content_cannot_claim_provider_response_id() -> None:
    request = _request()
    payload = {**_model_payload(request), "provider_response_id": "model-forged"}
    response = _provider_response(request, content=json.dumps(payload))
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt", max_tokens=1),
        http_client=HttpSpy(response),
    )
    with pytest.raises(OpenAICompatibleResponseError):
        transport.invoke(request)


class _Boundary:
    def begin(self) -> InvocationStart:
        return InvocationStart(attempt_id="openai-compatible-attempt-1", started_at=NOW)

    def end_at(self, invocation: InvocationStart) -> datetime:
        return invocation.started_at + timedelta(seconds=1)


def test_real_record_provider_atomically_journals_then_replays_without_http() -> None:
    request = _request()
    http = HttpSpy(_provider_response(request))
    transport = OpenAICompatibleChatTransport(
        profile=openai_chat_profile(model="gpt-requested", max_tokens=321),
        http_client=http,
    )
    policy = ExactModelIdentityPolicy(
        policy_id=request.model_identity_policy_id,
        model_identity="gpt-returned",
        model_revision="api-model-id:gpt-returned",
    )

    with LLMDecisionJournal() as journal:
        recorded = RecordedLLMDecisionProvider(
            transport=transport,
            journal=journal,
            model_policy=policy,
            invocation_boundary=_Boundary(),
        ).decide(request)
        attempts = journal.list_attempts()
        decisions = journal.list_decisions()
        replayed = ReplayLLMDecisionProvider(
            journal=journal,
            model_policy=policy,
        ).decide(request)

        assert recorded.provider_response_id == "chatcmpl-authoritative"
        assert attempts[0].provider_response_id == "chatcmpl-authoritative"
        assert decisions == (recorded,)
        assert replayed == recorded
        assert journal.list_attempts() == attempts
        assert len(http.calls) == 1
