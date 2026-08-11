import ast
import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Context,
    Decimal,
    DecimalException,
    getcontext,
    localcontext,
    setcontext,
)

import pytest
from pydantic import ValidationError

import stock_agent.strategies as strategies
import stock_agent.strategies.fixture as fixture_module
from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, Side, StrategyIntent
from stock_agent.strategies import (
    MarketSnapshot,
    MovingAverageFixtureStrategy,
    Strategy,
    StrategyContext,
)

AS_OF = datetime(2026, 7, 29, 20, tzinfo=UTC)


def make_bar(
    symbol: str,
    day: int,
    close: str,
    *,
    available_at: datetime | None = None,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "100",
) -> Bar:
    close_value = Decimal(close)
    open_value = Decimal(open_ if open_ is not None else close)
    high_value = Decimal(high) if high is not None else max(open_value, close_value)
    low_value = Decimal(low) if low is not None else min(open_value, close_value)
    session_date = date(2026, 7, day)
    return Bar(
        symbol=symbol,
        market=Market.US,
        session_date=session_date,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=Decimal(volume),
        available_at=available_at
        or datetime(2026, 7, day, 16, tzinfo=UTC),
    )


def make_position(symbol: str, market_value: str) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal("1"),
        average_cost=Decimal("1"),
        market_value=Decimal(market_value),
    )


def make_context(
    bars: tuple[Bar, ...],
    *,
    positions: tuple[Position, ...] = (),
    cash: str = "1000",
    peak_nav: str | None = None,
    as_of: datetime = AS_OF,
    config_version: str = "1",
) -> StrategyContext:
    cash_value = Decimal(cash)
    nav = cash_value + sum((item.market_value for item in positions), Decimal(0))
    peak = Decimal(peak_nav) if peak_nav is not None else nav
    return StrategyContext(
        market_snapshot=MarketSnapshot(as_of=as_of, market=Market.US, bars=bars),
        portfolio=PortfolioSnapshot(
            account_id="account-1",
            market=Market.US,
            cash=cash_value,
            nav=nav,
            peak_nav=peak,
            positions=positions,
            as_of=as_of,
        ),
        strategy_config_version=config_version,
    )


def make_context_with_exact_portfolio(
    bars: tuple[Bar, ...], *, market_value: str, cash: str, nav: str
) -> StrategyContext:
    with localcontext(Context(prec=max(map(len, (market_value, cash, nav))) + 2)):
        position = make_position("AAPL", market_value)
        portfolio = PortfolioSnapshot(
            account_id="account-1",
            market=Market.US,
            cash=Decimal(cash),
            nav=Decimal(nav),
            peak_nav=Decimal(nav),
            positions=(position,),
            as_of=AS_OF,
        )
        return StrategyContext(
            market_snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=bars),
            portfolio=portfolio,
            strategy_config_version="1",
        )


def trend_bars(symbol: str, closes: tuple[str, ...]) -> tuple[Bar, ...]:
    return tuple(make_bar(symbol, 26 + index, close) for index, close in enumerate(closes))


def decimal_context_signature() -> tuple[object, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        tuple(context.flags.items()),
        tuple(context.traps.items()),
    )


def evaluate_one(
    closes: tuple[str, ...],
    *,
    position_value: str | None = None,
    cash: str = "1000",
) -> StrategyIntent:
    positions = () if position_value is None else (make_position("AAPL", position_value),)
    context = make_context(trend_bars("AAPL", closes), positions=positions, cash=cash)
    return MovingAverageFixtureStrategy().evaluate(context)[0]


def test_public_identity_protocol_slots_and_frozen_configuration() -> None:
    strategy = MovingAverageFixtureStrategy()

    assert strategies.__all__ == [
        "MarketSnapshot",
        "MovingAverageFixtureStrategy",
        "Strategy",
        "StrategyContext",
    ]
    assert strategies.MovingAverageFixtureStrategy is MovingAverageFixtureStrategy
    assert isinstance(strategy, Strategy)
    assert strategy.strategy_id == "fixture-moving-average"
    assert strategy.config_version == "1"
    assert strategy.short_window == 2
    assert strategy.long_window == 3
    assert strategy.buy_target == Decimal("0.10")
    assert not hasattr(strategy, "__dict__")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        strategy.short_window = 4


def test_evaluate_requires_exact_revalidated_context_and_matching_config() -> None:
    strategy = MovingAverageFixtureStrategy()
    valid = make_context(trend_bars("AAPL", ("1", "2", "3")))

    with pytest.raises(TypeError):
        strategy.evaluate(object())  # type: ignore[arg-type]

    class ContextSubclass(StrategyContext):
        pass

    subclass = ContextSubclass(
        market_snapshot=valid.market_snapshot,
        portfolio=valid.portfolio,
        strategy_config_version=valid.strategy_config_version,
    )
    with pytest.raises(TypeError):
        strategy.evaluate(subclass)

    with pytest.raises(ValueError, match="config"):
        strategy.evaluate(make_context(valid.market_snapshot.bars, config_version="2"))

    polluted = make_context(valid.market_snapshot.bars)
    object.__setattr__(polluted.market_snapshot.bars[0], "close", [])
    with pytest.raises(ValidationError):
        strategy.evaluate(polluted)


@pytest.mark.parametrize(
    ("held_value", "expected_weight"),
    [
        (None, "0"),
        ("100", "0.090909090909090909090909090909090909090909090909090"),
    ],
)
def test_fewer_than_three_bars_holds_current_weight(
    held_value: str | None, expected_weight: str
) -> None:
    intent = evaluate_one(("1", "2"), position_value=held_value)

    assert intent.side is Side.HOLD
    assert intent.target_weight == Decimal(expected_weight)


@pytest.mark.parametrize(
    ("position_value", "cash", "expected_side", "expected_weight"),
    [
        (None, "1000", Side.BUY, "0.10"),
        ("100", "900", Side.HOLD, "0.10"),
        ("200", "800", Side.REDUCE, "0.10"),
    ],
)
def test_rising_average_targets_ten_percent(
    position_value: str | None,
    cash: str,
    expected_side: Side,
    expected_weight: str,
) -> None:
    intent = evaluate_one(("1", "2", "3"), position_value=position_value, cash=cash)

    assert intent.side is expected_side
    assert intent.target_weight == Decimal(expected_weight)


@pytest.mark.parametrize(
    ("market_value", "cash", "nav", "expected_side"),
    [
        (
            "1" + "0" * 60,
            "9" + "0" * 59 + "1",
            "1" + "0" * 60 + "1",
            Side.BUY,
        ),
        (
            "1" + "0" * 59 + "1",
            "8" + "9" * 60,
            "1" + "0" * 61,
            Side.REDUCE,
        ),
        ("1" + "0" * 60, "9" + "0" * 60, "1" + "0" * 61, Side.HOLD),
    ],
)
def test_rising_direction_uses_exact_ten_percent_comparison(
    market_value: str, cash: str, nav: str, expected_side: Side
) -> None:
    context = make_context_with_exact_portfolio(
        trend_bars("AAPL", ("1", "2", "3")),
        market_value=market_value,
        cash=cash,
        nav=nav,
    )

    intent = MovingAverageFixtureStrategy().evaluate(context)[0]

    assert intent.side is expected_side
    assert intent.target_weight == Decimal("0.10")


@pytest.mark.parametrize(
    "closes",
    [
        (f"1E{MIN_EMIN}", f"1E{MAX_EMAX}", f"1E{MIN_EMIN}"),
        (f"1E{MAX_EMAX}", f"9E{MAX_EMAX}", f"9E{MAX_EMAX}"),
    ],
)
def test_extreme_trend_arithmetic_has_stable_wrapped_failure(
    closes: tuple[str, ...],
) -> None:
    ambient_before = decimal_context_signature()

    with pytest.raises(ValueError, match=r"^fixture arithmetic failed$") as raised:
        MovingAverageFixtureStrategy().evaluate(make_context(trend_bars("AAPL", closes)))

    assert isinstance(raised.value.__cause__, DecimalException)
    assert decimal_context_signature() == ambient_before


def test_same_maximum_exponent_trend_is_classified_when_exactly_representable() -> None:
    closes = tuple(f"{coefficient}E{MAX_EMAX}" for coefficient in ("1", "2", "3"))

    intent = MovingAverageFixtureStrategy().evaluate(make_context(trend_bars("AAPL", closes)))[0]

    assert intent.side is Side.BUY


@pytest.mark.parametrize(
    ("position_value", "expected_side", "expected_weight"),
    [(None, Side.HOLD, "0"), ("100", Side.SELL, "0")],
)
def test_falling_average_sells_only_a_held_symbol(
    position_value: str | None, expected_side: Side, expected_weight: str
) -> None:
    intent = evaluate_one(("3", "2", "1"), position_value=position_value)

    assert intent.side is expected_side
    assert intent.target_weight == Decimal(expected_weight)


def test_equal_averages_hold_current_weight() -> None:
    intent = evaluate_one(("2", "2", "2"), position_value="100")

    assert intent.side is Side.HOLD
    assert intent.target_weight == Decimal(
        "0.090909090909090909090909090909090909090909090909090"
    )


def test_zero_nav_defines_zero_weight_even_for_a_zero_value_position() -> None:
    intent = evaluate_one(("1", "2", "3"), position_value="0", cash="0")

    assert intent.side is Side.BUY
    assert intent.target_weight == Decimal("0.10")


def test_only_final_three_bars_control_trend_and_evidence() -> None:
    bars = trend_bars("AAPL", ("1000", "1", "2", "3"))
    strategy = MovingAverageFixtureStrategy()

    long_history = strategy.evaluate(make_context(bars))[0]
    final_history = strategy.evaluate(make_context(bars[-3:]))[0]

    assert long_history.side is Side.BUY
    assert long_history == final_history
    assert len(long_history.evidence_ids) == 3


def test_multiple_symbols_emit_exact_intents_in_lexical_order() -> None:
    bars = trend_bars("AAPL", ("1", "2", "3")) + trend_bars("MSFT", ("3", "2", "1"))
    intents = MovingAverageFixtureStrategy().evaluate(make_context(bars))

    assert type(intents) is tuple
    assert all(type(intent) is StrategyIntent for intent in intents)
    assert tuple(intent.symbol for intent in intents) == ("AAPL", "MSFT")
    assert tuple(intent.side for intent in intents) == (Side.BUY, Side.HOLD)
    assert all(intent.strategy_id == "fixture-moving-average" for intent in intents)
    assert all(intent.market is Market.US for intent in intents)
    assert all(intent.as_of == AS_OF for intent in intents)


def test_evidence_matches_independent_literal_canonical_json_sha256() -> None:
    available_at = datetime(
        2026,
        7,
        26,
        16,
        0,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )
    item = make_bar(
        "AAPL",
        26,
        "1.50",
        available_at=available_at,
        open_="1.20",
        high="2.00",
        low="1.00",
        volume="100.0",
    )
    intent = MovingAverageFixtureStrategy().evaluate(
        make_context((item,), as_of=datetime(2026, 7, 29, 4, tzinfo=UTC))
    )[0]
    canonical_payload = (
        b'["US","AAPL","2026-07-26","12e-1","2e0","1e0","15e-1",'
        b'"1e2","2026-07-26T08:00:00.123456Z"]'
    )
    expected = f"bar-sha256:{hashlib.sha256(canonical_payload).hexdigest()}"

    assert expected == "bar-sha256:481d5c3e8ceff71f4fc65db6cede2fdc79727edf011373e03be1e84ce8284b4e"
    assert intent.evidence_ids == (expected,)
    assert intent.evidence_ids[0].startswith("bar-sha256:")
    assert len(intent.evidence_ids[0]) == len("bar-sha256:") + 64
    assert all(character in "0123456789abcdef" for character in intent.evidence_ids[0][-64:])


def test_evidence_normalizes_equal_decimals_and_changes_with_revision_content() -> None:
    available_at = datetime(2026, 7, 26, 16, tzinfo=UTC)
    original = make_bar(
        "AAPL", 26, "1.50", available_at=available_at, open_="1.20", high="2.0", low="1.0"
    )
    equal_decimals = make_bar(
        "AAPL",
        26,
        "1.5000",
        available_at=available_at,
        open_="1.2000",
        high="2.000",
        low="1.000",
        volume="100.000",
    )
    changed_content = make_bar(
        "AAPL", 26, "1.60", available_at=available_at, open_="1.20", high="2.0", low="1.0"
    )
    strategy = MovingAverageFixtureStrategy()

    original_id = strategy.evaluate(make_context((original,)))[0].evidence_ids
    equal_id = strategy.evaluate(make_context((equal_decimals,)))[0].evidence_ids
    changed_id = strategy.evaluate(make_context((changed_content,)))[0].evidence_ids

    assert original_id == equal_id
    assert changed_id != original_id


def test_legal_point_in_time_revisions_change_only_later_snapshot() -> None:
    early_as_of = datetime(2026, 7, 28, 16, tzinfo=UTC)
    later_as_of = datetime(2026, 7, 29, 16, tzinfo=UTC)
    first_two = (
        make_bar("AAPL", 26, "3"),
        make_bar("AAPL", 27, "2"),
    )
    early_last = make_bar("AAPL", 28, "1", available_at=early_as_of)
    revised_last = make_bar("AAPL", 28, "5", available_at=later_as_of)
    strategy = MovingAverageFixtureStrategy()

    early = strategy.evaluate(make_context((*first_two, early_last), as_of=early_as_of))[0]
    later = strategy.evaluate(make_context((*first_two, revised_last), as_of=later_as_of))[0]

    assert early.side is Side.HOLD
    assert later.side is Side.BUY
    assert early.evidence_ids[:2] == later.evidence_ids[:2]
    assert early.evidence_ids[2] != later.evidence_ids[2]
    assert early.as_of == early_as_of
    assert later.as_of == later_as_of


def test_future_revision_is_rejected_before_evaluation() -> None:
    future = make_bar("AAPL", 28, "4", available_at=AS_OF + timedelta(microseconds=1))

    with pytest.raises(ValidationError):
        make_context((future,))


def test_repeat_concurrency_hostile_decimal_and_inputs_are_unchanged() -> None:
    bars = trend_bars("AAPL", ("1", "2", "3"))
    context = make_context(bars, positions=(make_position("AAPL", "100"),))
    strategy = MovingAverageFixtureStrategy()
    context_before = context.model_dump_json()
    strategy_before = repr(strategy)
    ambient_before = getcontext().copy()
    hostile = ambient_before.copy()
    hostile.prec = 2
    hostile.Emax = 9
    hostile.Emin = -9
    hostile.rounding = "ROUND_DOWN"

    try:
        setcontext(hostile)
        hostile_before = decimal_context_signature()
        expected = tuple(intent.model_dump_json() for intent in strategy.evaluate(context))
        repeated = [
            tuple(intent.model_dump_json() for intent in strategy.evaluate(context))
            for _ in range(500)
        ]
        with ThreadPoolExecutor(max_workers=16) as executor:
            concurrent = list(
                executor.map(
                    lambda _: tuple(
                        intent.model_dump_json() for intent in strategy.evaluate(context)
                    ),
                    range(500),
                )
            )
        assert all(result == expected for result in repeated)
        assert all(result == expected for result in concurrent)
        assert decimal_context_signature() == hostile_before
    finally:
        setcontext(ambient_before)

    assert context.model_dump_json() == context_before
    assert repr(strategy) == strategy_before
    assert decimal_context_signature() == (
        ambient_before.prec,
        ambient_before.rounding,
        ambient_before.Emin,
        ambient_before.Emax,
        ambient_before.capitals,
        ambient_before.clamp,
        tuple(ambient_before.flags.items()),
        tuple(ambient_before.traps.items()),
    )


def test_fixture_imports_no_authority_and_exposes_no_authority_methods() -> None:
    tree = ast.parse(inspect.getsource(fixture_module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    forbidden_modules = (
        "stock_agent.account",
        "stock_agent.execution",
        "stock_agent.data",
        "stock_agent.risk",
    )
    assert not any(name.startswith(forbidden_modules) for name in imported)

    strategy = MovingAverageFixtureStrategy()
    forbidden_authority = {
        "store",
        "execute",
        "place_order",
        "order",
        "fill",
        "ledger",
        "write",
        "risk",
    }
    assert forbidden_authority.isdisjoint(dir(strategy))


def test_intent_content_ignores_hostile_environment_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    context = make_context(trend_bars("AAPL", ("1", "2", "3")))
    strategy = MovingAverageFixtureStrategy()
    before = tuple(intent.model_dump_json() for intent in strategy.evaluate(context))

    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("FIXTURE_BUY_TARGET", "0.99")
    monkeypatch.setenv("STRATEGY_CONFIG_VERSION", "hostile")
    after = tuple(intent.model_dump_json() for intent in strategy.evaluate(context))

    assert after == before
