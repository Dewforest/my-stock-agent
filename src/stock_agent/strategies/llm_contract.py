from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_agent.audit import canonical_datetime, canonical_decimal, tagged_sha256
from stock_agent.domain import Market, PortfolioSnapshot, Side

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1),
]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PercentageInt = Annotated[int, Field(strict=True, ge=0, le=100)]
FiniteDecimal = Annotated[Decimal, Field(strict=True, allow_inf_nan=False)]
FiniteNonNegativeDecimal = Annotated[
    Decimal,
    Field(strict=True, allow_inf_nan=False, ge=0),
]
FinitePositiveDecimal = Annotated[
    Decimal,
    Field(strict=True, allow_inf_nan=False, gt=0),
]
FinitePositiveUnitDecimal = Annotated[
    Decimal,
    Field(strict=True, allow_inf_nan=False, gt=0, le=1),
]
FiniteUnitDecimal = Annotated[
    Decimal,
    Field(strict=True, allow_inf_nan=False, ge=0, le=1),
]
PromptDigest = Annotated[
    str,
    StringConstraints(pattern=r"^prompt-sha256:[0-9a-f]{64}$"),
]
BarEvidenceId = Annotated[
    str,
    StringConstraints(pattern=r"^bar-sha256:[0-9a-f]{64}$"),
]
PortfolioSnapshotId = Annotated[
    str,
    StringConstraints(pattern=r"^portfolio-snapshot-sha256:[0-9a-f]{64}$"),
]
CandidateId = Annotated[
    str,
    StringConstraints(pattern=r"^strategy-a-candidate-sha256:[0-9a-f]{64}$"),
]
RequestFingerprint = Annotated[
    str,
    StringConstraints(pattern=r"^llm-decision-request-sha256:[0-9a-f]{64}$"),
]
ResponseDigest = Annotated[
    str,
    StringConstraints(pattern=r"^llm-decision-response-sha256:[0-9a-f]{64}$"),
]
DecisionId = Annotated[
    str,
    StringConstraints(pattern=r"^llm-decision-sha256:[0-9a-f]{64}$"),
]


class DecisionPhase(StrEnum):
    POST_CLOSE = "POST_CLOSE"


class StrategyARegime(StrEnum):
    INSUFFICIENT = "INSUFFICIENT"
    OFFENSIVE = "OFFENSIVE"
    NEUTRAL = "NEUTRAL"
    DEFENSIVE = "DEFENSIVE"


class StrategyADataQuality(StrEnum):
    COMPLETE = "COMPLETE"
    INSUFFICIENT = "INSUFFICIENT"


class LLMInvocationMode(StrEnum):
    RECORD = "RECORD"
    REPLAY = "REPLAY"


class LLMDecisionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    TRANSPORT = "TRANSPORT"
    SCHEMA = "SCHEMA"
    ENVELOPE = "ENVELOPE"
    IDENTITY_POLICY = "IDENTITY_POLICY"
    CONFLICT = "CONFLICT"
    AUDIT_PERSISTENCE = "AUDIT_PERSISTENCE"


class _ImmutableLLMModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    def __init_subclass__(cls) -> None:
        if cls.__bases__ != (_ImmutableLLMModel,):
            raise TypeError(f"{cls.__name__} does not support subclasses")
        super().__init_subclass__()

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("bounded LLM models do not support copy updates")
        return super().copy(
            include=include,
            exclude=exclude,
            update=None if update is None else dict(update),
            deep=deep,
        )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("bounded LLM models do not support copy updates")
        return super().model_copy(update=update, deep=deep)


class StrategyAConfig(_ImmutableLLMModel):
    config_version: NonBlankText
    short_window: PositiveInt
    long_window: PositiveInt
    volume_window: PositiveInt
    volume_confirmation_threshold: FiniteNonNegativeDecimal
    offensive_target_weight: FinitePositiveUnitDecimal
    neutral_target_weight: FiniteUnitDecimal
    model_identity_policy_id: NonBlankText
    prompt_template_id: NonBlankText
    prompt_template_digest: PromptDigest

    @model_validator(mode="after")
    def windows_and_weights_are_consistent(self) -> Self:
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be less than long_window")
        if self.neutral_target_weight > self.offensive_target_weight:
            raise ValueError("neutral target cannot exceed offensive target")
        return self

    @property
    def required_history(self) -> int:
        return max(self.long_window, self.volume_window + 1)


class StrategyAActionTarget(_ImmutableLLMModel):
    action: Side
    target_weight: FiniteUnitDecimal


class StrategyACandidateEnvelope(_ImmutableLLMModel):
    schema_version: Literal["strategy-a-candidate/v1"]
    strategy_id: NonBlankText
    config_version: NonBlankText
    market: Market
    symbol: Symbol
    as_of: AwareDatetime
    decision_phase: DecisionPhase
    regime: StrategyARegime
    data_quality: StrategyADataQuality
    short_window: PositiveInt
    long_window: PositiveInt
    volume_window: PositiveInt
    volume_confirmation_threshold: FiniteNonNegativeDecimal
    short_sum: FiniteNonNegativeDecimal
    long_sum: FiniteNonNegativeDecimal
    latest_close: FinitePositiveDecimal
    prior_volume_sum: FiniteNonNegativeDecimal
    latest_volume: FiniteNonNegativeDecimal
    portfolio_snapshot_id: PortfolioSnapshotId
    action_targets: tuple[StrategyAActionTarget, ...]
    reason_codes: tuple[NonBlankText, ...]
    evidence_ids: tuple[BarEvidenceId, ...]
    candidate_id: CandidateId

    @field_validator("action_targets", "reason_codes", "evidence_ids", mode="before")
    @classmethod
    def collections_are_exact_tuples(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("candidate collections must be exact tuples")
        return value

    @model_validator(mode="after")
    def collections_are_canonical(self) -> Self:
        actions = tuple(item.action for item in self.action_targets)
        action_order = {side: index for index, side in enumerate(Side)}
        if not actions or actions != tuple(sorted(actions, key=action_order.__getitem__)):
            raise ValueError("action targets must be nonempty and action-sorted")
        if len(actions) != len(set(actions)):
            raise ValueError("action targets must be unique")
        if self.reason_codes != tuple(sorted(self.reason_codes)) or len(
            self.reason_codes
        ) != len(set(self.reason_codes)):
            raise ValueError("reason codes must be sorted and unique")
        if self.evidence_ids != tuple(sorted(self.evidence_ids)) or len(
            self.evidence_ids
        ) != len(set(self.evidence_ids)):
            raise ValueError("evidence IDs must be sorted and unique")
        if (self.regime is StrategyARegime.INSUFFICIENT) != (
            self.data_quality is StrategyADataQuality.INSUFFICIENT
        ):
            raise ValueError("insufficient regime and data quality must agree")
        if self.candidate_id != candidate_id_for(self):
            raise ValueError("candidate_id does not match canonical content")
        return self


class LLMDecisionRequest(_ImmutableLLMModel):
    schema_version: Literal["llm-decision-request/v1"]
    strategy_id: NonBlankText
    config_version: NonBlankText
    market: Market
    as_of: AwareDatetime
    decision_phase: DecisionPhase
    model_identity_policy_id: NonBlankText
    prompt_template_id: NonBlankText
    prompt_template_digest: PromptDigest
    candidates: tuple[StrategyACandidateEnvelope, ...]
    request_fingerprint: RequestFingerprint

    @field_validator("candidates", mode="before")
    @classmethod
    def candidates_are_exact_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("request candidates must be an exact tuple")
        return value

    @model_validator(mode="after")
    def candidates_match_request(self) -> Self:
        symbols = tuple(item.symbol for item in self.candidates)
        if not symbols or symbols != tuple(sorted(symbols)) or len(symbols) != len(
            set(symbols)
        ):
            raise ValueError("request candidates must be nonempty, symbol-sorted, and unique")
        if any(
            item.strategy_id != self.strategy_id
            or item.config_version != self.config_version
            or item.market is not self.market
            or item.as_of != self.as_of
            or item.decision_phase is not self.decision_phase
            for item in self.candidates
        ):
            raise ValueError("request candidates must match request identity")
        if self.request_fingerprint != request_fingerprint_for(self):
            raise ValueError("request_fingerprint does not match canonical content")
        return self


class LLMDecisionSelection(_ImmutableLLMModel):
    symbol: Symbol
    action: Side
    confidence: PercentageInt
    thesis: NonBlankText
    invalidation: NonBlankText


class LLMDecisionResponse(_ImmutableLLMModel):
    schema_version: Literal["llm-decision-response/v1"]
    request_fingerprint: RequestFingerprint
    selections: tuple[LLMDecisionSelection, ...]
    provider_response_id: NonBlankText | None = None

    @field_validator("selections", mode="before")
    @classmethod
    def selections_are_exact_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("response selections must be an exact tuple")
        return value

    @model_validator(mode="after")
    def selections_are_canonical(self) -> Self:
        symbols = tuple(item.symbol for item in self.selections)
        if not symbols or symbols != tuple(sorted(symbols)) or len(symbols) != len(
            set(symbols)
        ):
            raise ValueError("response selections must be nonempty, symbol-sorted, and unique")
        return self


class LLMDecisionRecord(_ImmutableLLMModel):
    decision_id: DecisionId
    request_fingerprint: RequestFingerprint
    response_digest: ResponseDigest
    selections: tuple[LLMDecisionSelection, ...]
    config_version: NonBlankText
    model_identity_policy_id: NonBlankText
    prompt_template_id: NonBlankText
    prompt_template_digest: PromptDigest
    model_identity: NonBlankText
    model_revision: NonBlankText
    provider_response_id: NonBlankText | None = None
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @field_validator("selections", mode="before")
    @classmethod
    def selections_are_exact_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("record selections must be an exact tuple")
        return value

    @model_validator(mode="after")
    def record_is_consistent(self) -> Self:
        symbols = tuple(item.symbol for item in self.selections)
        if not symbols or symbols != tuple(sorted(symbols)) or len(symbols) != len(
            set(symbols)
        ):
            raise ValueError("record selections must be nonempty, symbol-sorted, and unique")
        if self.ended_at < self.started_at:
            raise ValueError("decision end cannot precede start")
        expected_response_digest = _response_digest_fields(
            self.request_fingerprint,
            self.selections,
        )
        if self.response_digest != expected_response_digest:
            raise ValueError("response_digest does not match recorded selections")
        if self.decision_id != decision_id_for(
            self.request_fingerprint, self.response_digest
        ):
            raise ValueError("decision_id does not match canonical digests")
        return self


class LLMInvocationAttempt(_ImmutableLLMModel):
    attempt_id: NonBlankText
    request_fingerprint: RequestFingerprint
    status: LLMDecisionStatus
    response_digest: ResponseDigest | None = None
    decision_id: DecisionId | None = None
    provider_response_id: NonBlankText | None = None
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("attempt end cannot precede start")
        if self.status is LLMDecisionStatus.SUCCESS:
            if self.response_digest is None or self.decision_id is None:
                raise ValueError("successful attempts require response and decision IDs")
            if self.decision_id != decision_id_for(
                self.request_fingerprint, self.response_digest
            ):
                raise ValueError("successful attempt decision_id must match canonical digests")
        elif self.decision_id is not None:
            raise ValueError("failed attempts cannot claim a canonical decision")
        return self


class LLMRunAttestation(_ImmutableLLMModel):
    attestation_id: NonBlankText
    execution_label: NonBlankText
    invocation_mode: LLMInvocationMode
    decision_ids: tuple[DecisionId, ...]
    occurred_at: AwareDatetime

    @field_validator("decision_ids", mode="before")
    @classmethod
    def decision_ids_are_exact_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("attested decision IDs must be an exact tuple")
        return value

    @model_validator(mode="after")
    def decision_ids_are_canonical(self) -> Self:
        if not self.decision_ids or self.decision_ids != tuple(sorted(self.decision_ids)):
            raise ValueError("attested decision IDs must be nonempty and sorted")
        if len(self.decision_ids) != len(set(self.decision_ids)):
            raise ValueError("attested decision IDs must be unique")
        return self


def portfolio_snapshot_id_for(snapshot: PortfolioSnapshot) -> str:
    if type(snapshot) is not PortfolioSnapshot:
        raise TypeError("portfolio identity requires an exact PortfolioSnapshot")
    if type(snapshot.positions) is not tuple:
        raise ValueError("portfolio positions must be an exact tuple")
    snapshot = PortfolioSnapshot.model_validate(snapshot)
    positions = tuple(sorted(snapshot.positions, key=lambda item: item.symbol))
    return tagged_sha256(
        "portfolio-snapshot",
        (
            snapshot.account_id,
            snapshot.market.value,
            canonical_decimal(snapshot.cash),
            canonical_decimal(snapshot.nav),
            canonical_decimal(snapshot.peak_nav),
            canonical_datetime(snapshot.as_of),
            [
                [
                    item.symbol,
                    canonical_decimal(item.quantity),
                    canonical_decimal(item.average_cost),
                    canonical_decimal(item.market_value),
                ]
                for item in positions
            ],
        ),
    )


def candidate_id_for(candidate: StrategyACandidateEnvelope) -> str:
    return tagged_sha256(
        "strategy-a-candidate",
        (
            candidate.schema_version,
            candidate.strategy_id,
            candidate.config_version,
            candidate.market.value,
            candidate.symbol,
            canonical_datetime(candidate.as_of),
            candidate.decision_phase.value,
            candidate.regime.value,
            candidate.data_quality.value,
            candidate.short_window,
            candidate.long_window,
            candidate.volume_window,
            canonical_decimal(candidate.volume_confirmation_threshold),
            canonical_decimal(candidate.short_sum),
            canonical_decimal(candidate.long_sum),
            canonical_decimal(candidate.latest_close),
            canonical_decimal(candidate.prior_volume_sum),
            canonical_decimal(candidate.latest_volume),
            candidate.portfolio_snapshot_id,
            [
                [item.action.value, canonical_decimal(item.target_weight)]
                for item in candidate.action_targets
            ],
            list(candidate.reason_codes),
            list(candidate.evidence_ids),
        ),
    )


def request_fingerprint_for(request: LLMDecisionRequest) -> str:
    return tagged_sha256(
        "llm-decision-request",
        (
            request.schema_version,
            request.strategy_id,
            request.config_version,
            request.market.value,
            canonical_datetime(request.as_of),
            request.decision_phase.value,
            request.model_identity_policy_id,
            request.prompt_template_id,
            request.prompt_template_digest,
            [item.candidate_id for item in request.candidates],
        ),
    )


def response_digest_for(response: LLMDecisionResponse) -> str:
    return _response_digest_fields(response.request_fingerprint, response.selections)


def _response_digest_fields(
    request_fingerprint: str,
    selections: tuple[LLMDecisionSelection, ...],
) -> str:
    return tagged_sha256(
        "llm-decision-response",
        (
            "llm-decision-response/v1",
            request_fingerprint,
            [
                [
                    item.symbol,
                    item.action.value,
                    item.confidence,
                    item.thesis,
                    item.invalidation,
                ]
                for item in selections
            ],
        ),
    )


def decision_id_for(request_fingerprint: str, response_digest: str) -> str:
    return tagged_sha256("llm-decision", (request_fingerprint, response_digest))
