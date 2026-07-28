from collections.abc import Mapping
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
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
    ValidationError,
    field_validator,
    model_validator,
)

from stock_agent.domain import Instrument, PortfolioSnapshot, Position, Side, StrategyIntent

_MARKET_MISMATCH = "MARKET_MISMATCH"
_ZERO_NAV_BUY_BLOCK = "ZERO_NAV_BUY_BLOCK"
_DRAWDOWN_BUY_BLOCK_15 = "DRAWDOWN_BUY_BLOCK_15"
_DRAWDOWN_RISK_REDUCTION_20 = "DRAWDOWN_RISK_REDUCTION_20"
_TWO = Decimal(2)
_FIFTEEN_PERCENT_LOSS_MULTIPLIER = Decimal(20)
_FIFTEEN_PERCENT_PEAK_MULTIPLIER = Decimal(3)
_TWENTY_PERCENT_LOSS_MULTIPLIER = Decimal(5)


def _arithmetic_context_for(*values: Decimal) -> Context:
    if any(not value.is_finite() for value in values):
        raise ValueError("arithmetic values must be finite")
    tuples = [value.as_tuple() for value in values]
    exponents: list[int] = []
    for value_tuple in tuples:
        if not isinstance(value_tuple.exponent, int):
            raise ValueError("arithmetic values must be finite")
        exponents.append(value_tuple.exponent)
    highest_adjusted = max((value.adjusted() for value in values), default=0)
    lowest_exponent = min(exponents, default=0)
    span = highest_adjusted - lowest_exponent + 1
    coefficient_digits = sum(max(1, len(value_tuple.digits)) for value_tuple in tuples)
    context = Context(
        prec=max(128, span + 32, coefficient_digits + 32),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    for signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
        context.traps[signal] = False
    return context


def _drawdown_thresholds(peak_nav: Decimal, nav: Decimal) -> tuple[bool, bool]:
    if peak_nav == 0:
        return False, False
    arithmetic = _arithmetic_context_for(
        peak_nav,
        nav,
        _FIFTEEN_PERCENT_LOSS_MULTIPLIER,
        _FIFTEEN_PERCENT_PEAK_MULTIPLIER,
        _TWENTY_PERCENT_LOSS_MULTIPLIER,
    )
    loss = arithmetic.subtract(peak_nav, nav)
    at_fifteen_percent = arithmetic.multiply(
        loss, _FIFTEEN_PERCENT_LOSS_MULTIPLIER
    ) >= arithmetic.multiply(peak_nav, _FIFTEEN_PERCENT_PEAK_MULTIPLIER)
    at_twenty_percent = arithmetic.multiply(
        loss, _TWENTY_PERCENT_LOSS_MULTIPLIER
    ) >= peak_nav
    return at_fifteen_percent, at_twenty_percent


def _gross_exposure(nav: Decimal, cash: Decimal) -> Decimal:
    arithmetic = _arithmetic_context_for(nav, cash)
    invested = arithmetic.subtract(nav, cash)
    if invested < 0 or invested > nav:
        raise ValueError("invested value must be between zero and NAV")
    if nav == 0:
        return Decimal(0)
    return arithmetic.divide(invested, nav)


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
        portfolio = context.portfolio
        try:
            at_fifteen_percent, at_twenty_percent = _drawdown_thresholds(
                portfolio.peak_nav, portfolio.nav
            )
            current_gross = _gross_exposure(portfolio.nav, portfolio.cash)
            target_context = _arithmetic_context_for(current_gross, _TWO)
            target_gross = target_context.divide(current_gross, _TWO)
        except DecimalException as error:
            raise ValueError("risk arithmetic failed") from error

        rule_ids: list[str] = []
        reasons: list[str] = []
        reduction = None
        if at_twenty_percent:
            try:
                reduction = RiskReductionTarget(
                    current_gross_exposure=current_gross,
                    target_gross_exposure=target_gross,
                    review_required=True,
                )
            except (DecimalException, ValidationError) as error:
                raise ValueError("risk arithmetic failed") from error
            rule_ids.append(_DRAWDOWN_RISK_REDUCTION_20)
            reasons.append("drawdown of twenty percent or more requires gross exposure review")

        if intent.side is Side.BUY and at_fifteen_percent:
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
