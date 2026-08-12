from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Never

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
from stock_agent.strategies.llm_http import (
    BoundedBearerHttpsClient,
    EnvironmentBearerTokenSource,
    LLMHTTPError,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    InvocationStart,
    LLMProviderError,
    RawLLMResponse,
    RecordedLLMDecisionProvider,
)
from stock_agent.strategies.openai_compatible import (
    PROMPT_TEMPLATE_DIGEST,
    PROMPT_TEMPLATE_ID,
    OpenAICompatibleChatTransport,
    OpenAICompatibleResponseError,
    deepseek_chat_profile,
    openai_chat_profile,
)

_FAILURE = "live LLM transport verification failed"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, "live LLM transport argument error\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Opt-in live verification of the bounded OpenAI-compatible LLM transport."
    )
    parser.add_argument("--provider", choices=("openai", "deepseek"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--expected-returned-model")
    parser.add_argument(
        "--env-var",
        required=True,
        help="Name of the bearer-token environment variable",
    )
    parser.add_argument("--max-tokens", required=True, type=int)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform exactly one live invocation (otherwise only validates construction)",
    )
    return parser


def build_transport(arguments: argparse.Namespace) -> OpenAICompatibleChatTransport:
    profile_factory = (
        openai_chat_profile if arguments.provider == "openai" else deepseek_chat_profile
    )
    profile = profile_factory(model=arguments.model, max_tokens=arguments.max_tokens)
    token_source = EnvironmentBearerTokenSource(arguments.env_var)
    client = BoundedBearerHttpsClient(
        token_source=token_source,
        allowed_endpoints=((profile.host, profile.target),),
    )
    return OpenAICompatibleChatTransport(profile=profile, http_client=client)


class _LiveInvocationBoundary:
    def begin(self) -> InvocationStart:
        return InvocationStart(
            attempt_id=f"live-llm-smoke-{uuid.uuid4().hex}",
            started_at=datetime.now(UTC),
        )

    def end_at(self, invocation: InvocationStart) -> datetime:
        ended = datetime.now(UTC)
        return max(ended, invocation.started_at)


class _DiagnosticFailure(Exception):
    pass


class _DiagnosticTransport:
    def __init__(self, transport: object) -> None:
        self._transport = transport
        self.failure_category: str | None = None
        self.returned_model: str | None = None

    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse:
        try:
            response = self._transport.invoke(request)  # type: ignore[attr-defined]
            self.returned_model = _safe_model_identifier(response.model_identity)
            return response  # type: ignore[no-any-return]
        except LLMHTTPError as error:
            self.failure_category = f"http_{error.code.value}"
        except OpenAICompatibleResponseError:
            self.failure_category = "provider_response"
        except TimeoutError:
            self.failure_category = "timeout"
        except Exception:
            self.failure_category = "transport"
        raise _DiagnosticFailure from None


def _safe_model_identifier(value: object) -> str | None:
    if (
        type(value) is str
        and 1 <= len(value) <= 256
        and value.isascii()
        and not any(character.isspace() or ord(character) < 33 for character in value)
    ):
        return value
    return None


def _request(model: str) -> LLMDecisionRequest:
    now = datetime.now(UTC)
    candidate_values = {
        "schema_version": "strategy-a-candidate/v1",
        "strategy_id": "strategy-a-live-transport-smoke",
        "config_version": "live-transport-smoke/v1",
        "market": Market.US,
        "symbol": "IBM",
        "as_of": now,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "regime": StrategyARegime.NEUTRAL,
        "data_quality": StrategyADataQuality.COMPLETE,
        "short_window": 2,
        "long_window": 3,
        "volume_window": 2,
        "volume_confirmation_threshold": Decimal("1"),
        "short_sum": Decimal("2"),
        "long_sum": Decimal("3"),
        "latest_close": Decimal("1"),
        "prior_volume_sum": Decimal("2"),
        "latest_volume": Decimal("1"),
        "portfolio_snapshot_id": "portfolio-snapshot-sha256:" + "0" * 64,
        "action_targets": (
            StrategyAActionTarget(action=Side.HOLD, target_weight=Decimal("0")),
        ),
        "reason_codes": ("live-transport-smoke",),
        "evidence_ids": ("bar-sha256:" + "0" * 64,),
    }
    provisional_candidate = StrategyACandidateEnvelope.model_construct(
        **candidate_values,
        candidate_id="strategy-a-candidate-sha256:" + "0" * 64,
    )
    candidate = StrategyACandidateEnvelope(
        **candidate_values,
        candidate_id=candidate_id_for(provisional_candidate),
    )
    request_values = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": candidate.strategy_id,
        "config_version": candidate.config_version,
        "market": candidate.market,
        "as_of": candidate.as_of,
        "decision_phase": candidate.decision_phase,
        "model_identity_policy_id": f"live-api-model-id:{model}",
        "prompt_template_id": PROMPT_TEMPLATE_ID,
        "prompt_template_digest": PROMPT_TEMPLATE_DIGEST,
        "candidates": (candidate,),
    }
    provisional_request = LLMDecisionRequest.model_construct(
        **request_values,
        request_fingerprint="llm-decision-request-sha256:" + "0" * 64,
    )
    return LLMDecisionRequest(
        **request_values,
        request_fingerprint=request_fingerprint_for(provisional_request),
    )


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        transport = build_transport(arguments)
    except (TypeError, ValueError):
        print(_FAILURE, file=sys.stderr)
        return 2
    if not arguments.execute:
        _emit(
            {
                "execute": False,
                "max_tokens": arguments.max_tokens,
                "model": arguments.model,
                "provider": arguments.provider,
                "status": "ready",
            }
        )
        return 0

    request = _request(arguments.model)
    diagnostic_transport = _DiagnosticTransport(transport)
    expected_model = arguments.expected_returned_model or arguments.model
    if _safe_model_identifier(expected_model) is None:
        print(_FAILURE, file=sys.stderr)
        return 2
    policy = ExactModelIdentityPolicy(
        policy_id=request.model_identity_policy_id,
        model_identity=expected_model,
        model_revision=f"api-model-id:{expected_model}",
    )
    try:
        with LLMDecisionJournal() as journal:
            decision = RecordedLLMDecisionProvider(
                transport=diagnostic_transport,
                journal=journal,
                model_policy=policy,
                invocation_boundary=_LiveInvocationBoundary(),
            ).decide(request)
            _emit(
                {
                    "decision_id": decision.decision_id,
                    "model_identity": decision.model_identity,
                    "provider_response_id": decision.provider_response_id,
                    "request_fingerprint": decision.request_fingerprint,
                    "status": "success",
                }
            )
    except LLMProviderError as error:
        category = diagnostic_transport.failure_category or error.code.value.lower()
        detail = ""
        if category == "identity_policy" and diagnostic_transport.returned_model is not None:
            detail = " returned_model=" + json.dumps(diagnostic_transport.returned_model)
        print(f"{_FAILURE}: {category}{detail}", file=sys.stderr)
        return 1
    except Exception:
        print(f"{_FAILURE}: internal", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
