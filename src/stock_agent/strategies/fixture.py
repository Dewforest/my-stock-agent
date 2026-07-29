import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

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
        with localcontext(_arithmetic_context()):
            return self._evaluate(context)

    def _evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        validated = StrategyContext.model_validate(context)
        if validated.strategy_config_version != self.config_version:
            raise ValueError("strategy config version does not match the fixture config")

        bars_by_symbol: defaultdict[str, list[Bar]] = defaultdict(list)
        for item in validated.market_snapshot.bars:
            bars_by_symbol[item.symbol].append(item)
        positions = {item.symbol: item for item in validated.portfolio.positions}

        intents: list[StrategyIntent] = []
        for symbol in sorted(bars_by_symbol):
            symbol_bars = bars_by_symbol[symbol]
            used_bars = symbol_bars[-self.long_window :]
            held = symbol in positions
            current_weight = self._current_weight(
                positions[symbol].market_value if held else Decimal(0),
                validated.portfolio.nav,
            )
            side, target_weight = self._decision(used_bars, held, current_weight)
            intents.append(
                StrategyIntent(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    market=validated.market_snapshot.market,
                    side=side,
                    target_weight=target_weight,
                    confidence=100,
                    as_of=validated.market_snapshot.as_of,
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
        return _arithmetic_context().divide(market_value, nav)

    def _decision(
        self,
        used_bars: list[Bar],
        held: bool,
        current_weight: Decimal,
    ) -> tuple[Side, Decimal]:
        if len(used_bars) < self.long_window:
            return Side.HOLD, current_weight

        arithmetic = _arithmetic_context()
        long_total = _sum_closes(arithmetic, used_bars)
        short_total = _sum_closes(arithmetic, used_bars[-self.short_window :])
        long_average = arithmetic.divide(long_total, Decimal(self.long_window))
        short_average = arithmetic.divide(short_total, Decimal(self.short_window))

        if short_average > long_average:
            if current_weight < self.buy_target:
                side = Side.BUY
            elif current_weight == self.buy_target:
                side = Side.HOLD
            else:
                side = Side.REDUCE
            return side, self.buy_target
        if short_average < long_average:
            return (Side.SELL, Decimal(0)) if held else (Side.HOLD, Decimal(0))
        return Side.HOLD, current_weight


def _arithmetic_context() -> Context:
    return Context(prec=50, rounding=ROUND_HALF_EVEN)


def _sum_closes(arithmetic: Context, bars: list[Bar]) -> Decimal:
    total = Decimal(0)
    for item in bars:
        total = arithmetic.add(total, item.close)
    return total


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
