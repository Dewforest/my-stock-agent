import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    FloatOperation,
    Inexact,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)
from itertools import zip_longest

from stock_agent.domain import Bar, Side, StrategyIntent
from stock_agent.strategies.protocol import StrategyContext


@dataclass(frozen=True, slots=True)
class MovingAverageFixtureStrategy:
    strategy_id: str = field(default="fixture-moving-average", init=False)
    config_version: str = field(default="1", init=False)
    short_window: int = field(default=2, init=False)
    long_window: int = field(default=3, init=False)
    buy_target: Decimal = field(default=Decimal("0.10"), init=False)

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        if type(context) is not StrategyContext:
            raise TypeError("context must be an exact StrategyContext")
        try:
            with localcontext(_validation_context(context)):
                validated = StrategyContext.model_validate(context)
            return self._evaluate(validated)
        except DecimalException as error:
            raise ValueError("fixture arithmetic failed") from error

    def _evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        if context.strategy_config_version != self.config_version:
            raise ValueError("strategy config version does not match the fixture config")

        bars_by_symbol: defaultdict[str, list[Bar]] = defaultdict(list)
        for item in context.market_snapshot.bars:
            bars_by_symbol[item.symbol].append(item)
        positions = {item.symbol: item for item in context.portfolio.positions}

        intents: list[StrategyIntent] = []
        for symbol in sorted(bars_by_symbol):
            symbol_bars = bars_by_symbol[symbol]
            used_bars = symbol_bars[-self.long_window :]
            held = symbol in positions
            market_value = positions[symbol].market_value if held else Decimal(0)
            current_weight = self._current_weight(
                market_value,
                context.portfolio.nav,
            )
            weight_direction = _weight_direction(market_value, context.portfolio.nav)
            side, target_weight = self._decision(
                used_bars, held, current_weight, weight_direction
            )
            intents.append(
                StrategyIntent(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    market=context.market_snapshot.market,
                    side=side,
                    target_weight=target_weight,
                    confidence=100,
                    as_of=context.market_snapshot.as_of,
                    thesis="Deterministic 2/3 moving-average fixture signal",
                    invalidation="A later point-in-time snapshot changes the fixture signal",
                    evidence_ids=tuple(_bar_evidence_id(item) for item in used_bars),
                )
            )
        return tuple(intents)

    @staticmethod
    def _current_weight(market_value: Decimal, nav: Decimal) -> Decimal:
        if nav.is_zero():
            return Decimal(0)
        return _weight_context(market_value, nav).divide(market_value, nav)

    def _decision(
        self,
        used_bars: list[Bar],
        held: bool,
        current_weight: Decimal,
        weight_direction: int,
    ) -> tuple[Side, Decimal]:
        if len(used_bars) < self.long_window:
            return Side.HOLD, current_weight

        trend_direction = _trend_direction(used_bars)
        if trend_direction > 0:
            if weight_direction < 0:
                side = Side.BUY
            elif weight_direction == 0:
                side = Side.HOLD
            else:
                side = Side.REDUCE
            return side, self.buy_target
        if trend_direction < 0:
            return (Side.SELL, Decimal(0)) if held else (Side.HOLD, Decimal(0))
        return Side.HOLD, current_weight


def _validation_context(context: StrategyContext) -> Context:
    portfolio_values = (
        context.portfolio.cash,
        context.portfolio.nav,
        context.portfolio.peak_nav,
        *(position.market_value for position in context.portfolio.positions),
    )
    precision = min(
        MAX_PREC,
        max(50, max(len(value.as_tuple().digits) for value in portfolio_values) + 2),
    )
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )


def _weight_context(market_value: Decimal, nav: Decimal) -> Context:
    precision = min(
        MAX_PREC,
        max(50, len(market_value.as_tuple().digits) + len(nav.as_tuple().digits) + 2),
    )
    context = Context(
        prec=precision,
        rounding=ROUND_FLOOR,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )
    for signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
        context.traps[signal] = False
    return context


def _trend_direction(bars: list[Bar]) -> int:
    precision = min(
        MAX_PREC,
        max(50, max(len(item.close.as_tuple().digits) for item in bars) + 2),
    )
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )
    for signal in (Inexact, Rounded, Overflow):
        context.traps[signal] = True
    recent_total = context.add(bars[-2].close, bars[-1].close)
    doubled_oldest = context.multiply(Decimal(2), bars[-3].close)
    return (recent_total > doubled_oldest) - (recent_total < doubled_oldest)


def _weight_direction(market_value: Decimal, nav: Decimal) -> int:
    if nav.is_zero():
        return -1
    return _compare_tenfold_to_decimal(market_value, nav)


def _compare_tenfold_to_decimal(left: Decimal, right: Decimal) -> int:
    if not left.is_finite() or not right.is_finite() or left < 0 or right < 0:
        raise ValueError("fixture comparison values must be finite and nonnegative")

    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    left_digits = tuple(left_tuple.digits)
    right_digits = tuple(right_tuple.digits)
    if not any(left_digits) or not any(right_digits):
        return (any(left_digits) > any(right_digits)) - (any(left_digits) < any(right_digits))

    left_exponent = int(left_tuple.exponent) + 1
    right_exponent = int(right_tuple.exponent)
    left_adjusted = left_exponent + len(left_digits) - 1
    right_adjusted = right_exponent + len(right_digits) - 1
    if left_adjusted != right_adjusted:
        return (left_adjusted > right_adjusted) - (left_adjusted < right_adjusted)

    for left_digit, right_digit in zip_longest(left_digits, right_digits, fillvalue=0):
        if left_digit != right_digit:
            return (left_digit > right_digit) - (left_digit < right_digit)
    return 0


def _bar_evidence_id(item: Bar) -> str:
    payload = [
        item.market.value,
        item.symbol,
        item.session_date.isoformat(),
        _canonical_decimal(item.open),
        _canonical_decimal(item.high),
        _canonical_decimal(item.low),
        _canonical_decimal(item.close),
        _canonical_decimal(item.volume),
        item.available_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"bar-sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    decimal_tuple = value.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = int(decimal_tuple.exponent)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    sign = "-" if decimal_tuple.sign else ""
    return f"{sign}{coefficient}e{exponent}"
