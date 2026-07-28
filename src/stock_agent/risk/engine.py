from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    InstanceOf,
    field_validator,
    model_validator,
)

from stock_agent.domain import Instrument, PortfolioSnapshot, StrategyIntent


def _finite_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    return value


StrictRiskDecimal = Annotated[
    Decimal,
    BeforeValidator(_finite_decimal),
    Field(ge=0),
]
StrictUnitRiskDecimal = Annotated[
    Decimal,
    BeforeValidator(_finite_decimal),
    Field(ge=0, le=1),
]


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskDecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    CLAMPED = "CLAMPED"
    REJECTED = "REJECTED"


class RiskContext(_ImmutableModel):
    portfolio: InstanceOf[PortfolioSnapshot]
    instruments: tuple[InstanceOf[Instrument], ...]
    day_start_available_cash: StrictRiskDecimal
    new_position_notional_committed_today: StrictRiskDecimal

    @field_validator("instruments", mode="before")
    @classmethod
    def instruments_are_a_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("instruments must be a tuple")
        return value

    @model_validator(mode="after")
    def instruments_are_consistent(self) -> Self:
        symbols = [instrument.symbol for instrument in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("instrument symbols must be unique")
        if any(instrument.market != self.portfolio.market for instrument in self.instruments):
            raise ValueError("instrument markets must match the portfolio market")
        return self


class RiskReductionTarget(_ImmutableModel):
    current_gross_exposure: StrictUnitRiskDecimal
    target_gross_exposure: StrictUnitRiskDecimal
    review_required: Literal[True] = True


class RiskDecision(_ImmutableModel):
    original_intent: InstanceOf[StrategyIntent]
    status: RiskDecisionStatus
    approved_target_weight: StrictUnitRiskDecimal | None
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    risk_reduction: RiskReductionTarget | None = None

    @field_validator("rule_ids", "reasons", mode="before")
    @classmethod
    def text_collections_are_tuples(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("rule_ids and reasons must be tuples")
        return value

    @field_validator("rule_ids", "reasons")
    @classmethod
    def text_items_are_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(item.strip() for item in value)
        if any(not item for item in stripped):
            raise ValueError("rule_ids and reasons must contain nonempty text")
        return stripped

    @field_validator("rule_ids")
    @classmethod
    def rule_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("rule_ids must be unique")
        return value

    @model_validator(mode="after")
    def state_is_consistent(self) -> Self:
        if len(self.rule_ids) != len(self.reasons):
            raise ValueError("rule_ids and reasons must have equal lengths")
        if self.status is RiskDecisionStatus.REJECTED:
            if self.approved_target_weight is not None:
                raise ValueError("REJECTED decisions must not approve a target")
        elif self.approved_target_weight is None:
            raise ValueError("APPROVED and CLAMPED decisions must approve a target")
        return self


class RiskEngine:
    __slots__ = ()
