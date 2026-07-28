from collections.abc import Mapping
from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
)
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    InstanceOf,
    field_validator,
    model_validator,
)

from stock_agent.domain import Instrument, PortfolioSnapshot, Position, Side, StrategyIntent

_MARKET_MISMATCH = "MARKET_MISMATCH"
_ZERO_NAV_BUY_BLOCK = "ZERO_NAV_BUY_BLOCK"
_DRAWDOWN_BUY_BLOCK_15 = "DRAWDOWN_BUY_BLOCK_15"
_DRAWDOWN_RISK_REDUCTION_20 = "DRAWDOWN_RISK_REDUCTION_20"
_DRAWDOWN_BUY_THRESHOLD = Decimal("0.15")
_DRAWDOWN_REDUCTION_THRESHOLD = Decimal("0.20")
_TWO = Decimal(2)
_DECIMAL_CONTEXT = Context(
    prec=128,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999999999999999,
    Emax=999999999999999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
# Context() receives every signal explicitly through these two exhaustive groups.
for _signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
    _DECIMAL_CONTEXT.traps[_signal] = False


def _finite_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    return value


def _exact_true(value: object) -> bool:
    if not (type(value) is bool and value is True):
        raise ValueError("value must be exactly True")
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

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update:
            raise TypeError("immutable risk models do not support copy updates")
        return super().model_copy(update=None, deep=deep)


class RiskDecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    CLAMPED = "CLAMPED"
    REJECTED = "REJECTED"


class RiskContext(_ImmutableModel):
    portfolio: InstanceOf[PortfolioSnapshot]
    instruments: tuple[InstanceOf[Instrument], ...]
    day_start_available_cash: StrictRiskDecimal
    new_position_notional_committed_today: StrictRiskDecimal

    @field_validator("portfolio", mode="before")
    @classmethod
    def portfolio_has_exact_type(cls, value: object) -> object:
        if type(value) is not PortfolioSnapshot:
            raise ValueError("portfolio must be a PortfolioSnapshot")
        if type(value.positions) is not tuple:
            raise ValueError("portfolio positions must be a tuple")
        if any(type(item) is not Position for item in value.positions):
            raise ValueError("portfolio positions must contain Position values")
        return value

    @field_validator("instruments", mode="before")
    @classmethod
    def instruments_are_a_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("instruments must be a tuple")
        if any(type(item) is not Instrument for item in value):
            raise ValueError("instruments must contain Instrument values")
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
    review_required: Annotated[Literal[True], BeforeValidator(_exact_true)] = True


class RiskDecision(_ImmutableModel):
    original_intent: InstanceOf[StrategyIntent]
    status: RiskDecisionStatus
    approved_target_weight: StrictUnitRiskDecimal | None
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    risk_reduction: RiskReductionTarget | None = None

    @field_validator("risk_reduction", mode="before")
    @classmethod
    def risk_reduction_has_exact_type(cls, value: object) -> object:
        if value is not None and type(value) is not RiskReductionTarget:
            raise ValueError("risk_reduction must be a RiskReductionTarget")
        return value

    @field_validator("original_intent", mode="before")
    @classmethod
    def original_intent_has_exact_type(cls, value: object) -> object:
        if type(value) is not StrategyIntent:
            raise ValueError("original_intent must be a StrategyIntent")
        return value

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

    def evaluate(self, intent: StrategyIntent, context: RiskContext) -> RiskDecision:
        if type(intent) is not StrategyIntent:
            raise TypeError("intent must be exactly StrategyIntent")
        if type(context) is not RiskContext:
            raise TypeError("context must be exactly RiskContext")
        if intent.market != context.portfolio.market:
            return RiskDecision(
                original_intent=intent,
                status=RiskDecisionStatus.REJECTED,
                approved_target_weight=None,
                rule_ids=(_MARKET_MISMATCH,),
                reasons=("intent market does not match portfolio market",),
            )
        decimal_context = _DECIMAL_CONTEXT.copy()
        portfolio = context.portfolio
        try:
            drawdown = (
                Decimal(0)
                if portfolio.peak_nav == 0
                else decimal_context.divide(
                    decimal_context.subtract(portfolio.peak_nav, portfolio.nav),
                    portfolio.peak_nav,
                )
            )
            total_position_value = Decimal(0)
            for position in portfolio.positions:
                total_position_value = decimal_context.add(
                    total_position_value, position.market_value
                )
            current_gross = (
                Decimal(0)
                if portfolio.nav == 0
                else decimal_context.divide(total_position_value, portfolio.nav)
            )
            target_gross = decimal_context.divide(current_gross, _TWO)
        except DecimalException:
            raise ValueError("risk arithmetic failed") from None

        rule_ids: list[str] = []
        reasons: list[str] = []
        reduction = None
        if drawdown >= _DRAWDOWN_REDUCTION_THRESHOLD:
            reduction = RiskReductionTarget(
                current_gross_exposure=current_gross,
                target_gross_exposure=target_gross,
                review_required=True,
            )
            rule_ids.append(_DRAWDOWN_RISK_REDUCTION_20)
            reasons.append("drawdown of twenty percent or more requires gross exposure review")

        if intent.side is Side.BUY and drawdown >= _DRAWDOWN_BUY_THRESHOLD:
            rule_ids.append(_DRAWDOWN_BUY_BLOCK_15)
            reasons.append("buy blocked at drawdown of fifteen percent or more")
            return RiskDecision(
                original_intent=intent,
                status=RiskDecisionStatus.REJECTED,
                approved_target_weight=None,
                rule_ids=tuple(rule_ids),
                reasons=tuple(reasons),
                risk_reduction=reduction,
            )
        if intent.side is Side.BUY and portfolio.nav == 0:
            return RiskDecision(
                original_intent=intent,
                status=RiskDecisionStatus.REJECTED,
                approved_target_weight=None,
                rule_ids=(_ZERO_NAV_BUY_BLOCK,),
                reasons=("buy blocked because portfolio NAV is zero",),
            )
        return RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.APPROVED,
            approved_target_weight=intent.target_weight,
            rule_ids=tuple(rule_ids),
            reasons=tuple(reasons),
            risk_reduction=reduction,
        )
