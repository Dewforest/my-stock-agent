from collections.abc import Mapping
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_CEILING,
    ROUND_FLOOR,
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
_MISSING_INSTRUMENT_METADATA = "MISSING_INSTRUMENT_METADATA"
_HOLDING_COUNT_MAX_10 = "HOLDING_COUNT_MAX_10"
_SINGLE_STOCK_MAX_15 = "SINGLE_STOCK_MAX_15"
_SECTOR_EXPOSURE_MAX_30 = "SECTOR_EXPOSURE_MAX_30"
_DAILY_NEW_POSITION_CASH_MAX_30 = "DAILY_NEW_POSITION_CASH_MAX_30"
_ZERO_TARGET_BUY_BLOCK = "ZERO_TARGET_BUY_BLOCK"
_TWO = Decimal(2)
_SINGLE_STOCK_LIMIT = Decimal("0.15")
_SECTOR_EXPOSURE_LIMIT = Decimal("0.30")
_DAILY_NEW_POSITION_CASH_LIMIT = Decimal("0.30")
_FIFTEEN_PERCENT_LOSS_FACTOR = 20
_FIFTEEN_PERCENT_PEAK_FACTOR = 3
_TWENTY_PERCENT_LOSS_FACTOR = 5


def _arithmetic_context_for(*values: Decimal) -> Context:
    if any(not value.is_finite() for value in values):
        raise ValueError("arithmetic values must be finite")
    tuples = [value.as_tuple() for value in values]
    nonzero_exponents: list[int] = []
    for value, value_tuple in zip(values, tuples, strict=True):
        if not isinstance(value_tuple.exponent, int):
            raise ValueError("arithmetic values must be finite")
        if value:
            nonzero_exponents.append(value_tuple.exponent)
    highest_adjusted = max((value.adjusted() for value in values if value), default=0)
    lowest_exponent = min(nonzero_exponents, default=0)
    span = highest_adjusted - lowest_exponent + 1
    coefficient_digits = sum(max(1, len(value_tuple.digits)) for value_tuple in tuples)
    context = Context(
        prec=min(MAX_PREC, max(128, span + 32, coefficient_digits + 32)),
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


def _compare_scaled_decimals(
    left: Decimal, left_factor: int, right: Decimal, right_factor: int
) -> int:
    if left < 0 or right < 0 or left_factor < 0 or right_factor < 0:
        raise ValueError("scaled comparison values must be nonnegative")

    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    if not isinstance(left_tuple.exponent, int) or not isinstance(right_tuple.exponent, int):
        raise ValueError("scaled comparison values must be finite")

    left_coefficient = int("".join(map(str, left_tuple.digits))) * left_factor
    right_coefficient = int("".join(map(str, right_tuple.digits))) * right_factor
    if left_coefficient == 0 or right_coefficient == 0:
        return (left_coefficient > right_coefficient) - (
            left_coefficient < right_coefficient
        )

    left_adjusted = left_tuple.exponent + len(str(left_coefficient)) - 1
    right_adjusted = right_tuple.exponent + len(str(right_coefficient)) - 1
    if left_adjusted != right_adjusted:
        return (left_adjusted > right_adjusted) - (left_adjusted < right_adjusted)

    common_exponent = min(left_tuple.exponent, right_tuple.exponent)
    left_integer = left_coefficient * 10 ** (left_tuple.exponent - common_exponent)
    right_integer = right_coefficient * 10 ** (right_tuple.exponent - common_exponent)
    return (left_integer > right_integer) - (left_integer < right_integer)


def _drawdown_thresholds(peak_nav: Decimal, nav: Decimal) -> tuple[bool, bool]:
    if peak_nav == 0:
        return False, False
    arithmetic = _arithmetic_context_for(peak_nav, nav)
    loss = arithmetic.subtract(peak_nav, nav)
    at_fifteen_percent = (
        _compare_scaled_decimals(
            loss, _FIFTEEN_PERCENT_LOSS_FACTOR, peak_nav, _FIFTEEN_PERCENT_PEAK_FACTOR
        )
        >= 0
    )
    at_twenty_percent = (
        _compare_scaled_decimals(loss, _TWENTY_PERCENT_LOSS_FACTOR, peak_nav, 1) >= 0
    )
    return at_fifteen_percent, at_twenty_percent


def _gross_exposure(nav: Decimal, cash: Decimal) -> Decimal:
    arithmetic = _arithmetic_context_for(nav, cash)
    invested = arithmetic.subtract(nav, cash)
    if invested < 0 or invested > nav:
        raise ValueError("invested value must be between zero and NAV")
    if nav == 0:
        return Decimal(0)
    return arithmetic.divide(invested, nav)


def _ratio_rounded_down(numerator: Decimal, denominator: Decimal) -> Decimal:
    if numerator <= 0:
        return Decimal(0)
    numerator_tuple = numerator.as_tuple()
    denominator_tuple = denominator.as_tuple()
    arithmetic = Context(
        prec=max(128, len(numerator_tuple.digits) + len(denominator_tuple.digits) + 32),
        rounding=ROUND_FLOOR,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    for signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
        arithmetic.traps[signal] = False
    return arithmetic.divide(numerator, denominator)


def _sector_room(
    intent: StrategyIntent,
    portfolio: PortfolioSnapshot,
    instruments_by_symbol: Mapping[str, Instrument],
) -> Decimal:
    intent_sector = instruments_by_symbol[intent.symbol].sector.strip().casefold()
    same_sector_values = tuple(
        position.market_value
        for position in portfolio.positions
        if position.symbol != intent.symbol
        and instruments_by_symbol[position.symbol].sector.strip().casefold() == intent_sector
    )
    if not same_sector_values:
        return _SECTOR_EXPOSURE_LIMIT
    arithmetic = _arithmetic_context_for(portfolio.nav, *same_sector_values)
    same_sector_value = Decimal(0)
    for market_value in same_sector_values:
        same_sector_value = arithmetic.add(same_sector_value, market_value)
    ratio_context = arithmetic.copy()
    ratio_context.rounding = ROUND_CEILING
    other_sector_weight = ratio_context.divide(same_sector_value, portfolio.nav)
    weight_context = _arithmetic_context_for(
        _SECTOR_EXPOSURE_LIMIT, other_sector_weight
    )
    return weight_context.subtract(_SECTOR_EXPOSURE_LIMIT, other_sector_weight)


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


def _evaluate_buy_exposure(intent: StrategyIntent, context: RiskContext) -> RiskDecision:
    portfolio = context.portfolio
    instruments_by_symbol = {
        instrument.symbol: instrument for instrument in context.instruments
    }
    required_symbols = {intent.symbol, *(position.symbol for position in portfolio.positions)}
    missing_symbols = sorted(required_symbols - instruments_by_symbol.keys())
    if missing_symbols:
        return RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.REJECTED,
            approved_target_weight=None,
            rule_ids=(_MISSING_INSTRUMENT_METADATA,),
            reasons=(f"missing instrument metadata for symbols: {', '.join(missing_symbols)}",),
        )

    position_symbols = {position.symbol for position in portfolio.positions}
    if intent.symbol not in position_symbols and len(position_symbols) >= 10:
        return RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.REJECTED,
            approved_target_weight=None,
            rule_ids=(_HOLDING_COUNT_MAX_10,),
            reasons=("buy would exceed maximum holding count of ten",),
        )

    approved_target = intent.target_weight
    rule_ids: list[str] = []
    reasons: list[str] = []
    if approved_target > _SINGLE_STOCK_LIMIT:
        approved_target = _SINGLE_STOCK_LIMIT
        rule_ids.append(_SINGLE_STOCK_MAX_15)
        reasons.append("buy target exceeds single-stock maximum of fifteen percent")

    sector_room = _sector_room(intent, portfolio, instruments_by_symbol)
    if sector_room < approved_target:
        approved_target = max(Decimal(0), sector_room)
        rule_ids.append(_SECTOR_EXPOSURE_MAX_30)
        reasons.append("buy target exceeds sector exposure maximum of thirty percent")

    if intent.symbol not in position_symbols:
        budget_context = _arithmetic_context_for(context.day_start_available_cash)
        daily_budget = budget_context.multiply(
            context.day_start_available_cash, _DAILY_NEW_POSITION_CASH_LIMIT
        )
        amount_context = _arithmetic_context_for(
            daily_budget, context.new_position_notional_committed_today
        )
        remaining = max(
            Decimal(0),
            amount_context.subtract(
                daily_budget, context.new_position_notional_committed_today
            ),
        )
        daily_target_cap = _ratio_rounded_down(remaining, portfolio.nav)
        if daily_target_cap < approved_target:
            approved_target = daily_target_cap
            rule_ids.append(_DAILY_NEW_POSITION_CASH_MAX_30)
            reasons.append(
                "buy target exceeds remaining daily new-position cash budget of thirty percent"
            )

    if approved_target == 0:
        if not rule_ids:
            rule_ids.append(_ZERO_TARGET_BUY_BLOCK)
            reasons.append("buy target must be greater than zero")
        return RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.REJECTED,
            approved_target_weight=None,
            rule_ids=tuple(rule_ids),
            reasons=tuple(reasons),
        )

    return RiskDecision(
        original_intent=intent,
        status=RiskDecisionStatus.CLAMPED if rule_ids else RiskDecisionStatus.APPROVED,
        approved_target_weight=approved_target,
        rule_ids=tuple(rule_ids),
        reasons=tuple(reasons),
    )


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
        if intent.side is Side.BUY:
            try:
                return _evaluate_buy_exposure(intent, context)
            except (DecimalException, ValidationError) as error:
                raise ValueError("risk arithmetic failed") from error
        return RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.APPROVED,
            approved_target_weight=intent.target_weight,
            rule_ids=tuple(rule_ids),
            reasons=tuple(reasons),
            risk_reduction=reduction,
        )
