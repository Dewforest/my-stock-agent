import ast
import inspect
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ConfigDict, ValidationError

import stock_agent.strategies as strategies
import stock_agent.strategies.protocol as protocol_module
from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, StrategyIntent
from stock_agent.strategies import MarketSnapshot, Strategy, StrategyContext

AS_OF = datetime(2026, 7, 28, 20, tzinfo=UTC)


def bar(
    symbol: str = "AAPL",
    session_date: date = date(2026, 7, 28),
    available_at: datetime = datetime(2026, 7, 28, 16, tzinfo=UTC),
    market: Market = Market.US,
) -> Bar:
    return Bar(
        symbol=symbol,
        market=market,
        session_date=session_date,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1000"),
        available_at=available_at,
    )


def snapshot_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {"as_of": AS_OF, "market": Market.US, "bars": ()}
    values.update(overrides)
    return values


def position() -> Position:
    return Position(
        symbol="AAPL",
        quantity=Decimal("1"),
        average_cost=Decimal("100"),
        market_value=Decimal("100"),
    )


def portfolio(
    *,
    as_of: datetime = AS_OF,
    market: Market = Market.US,
    positions: tuple[Position, ...] = (),
) -> PortfolioSnapshot:
    positions_value = sum((item.market_value for item in positions), Decimal(0))
    return PortfolioSnapshot(
        account_id="account-1",
        market=market,
        cash=Decimal("1000"),
        nav=Decimal("1000") + positions_value,
        peak_nav=Decimal("1000") + positions_value,
        positions=positions,
        as_of=as_of,
    )


def context_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "market_snapshot": MarketSnapshot(**snapshot_values()),
        "portfolio": portfolio(),
        "strategy_config_version": "v1",
    }
    values.update(overrides)
    return values


def test_public_protocol_exports_are_available() -> None:
    assert strategies.__all__ == [
        "BoundedLLMStrategyA",
        "MarketSnapshot",
        "MovingAverageFixtureStrategy",
        "Strategy",
        "StrategyContext",
    ]
    assert strategies.MarketSnapshot is MarketSnapshot
    assert strategies.StrategyContext is StrategyContext
    assert strategies.Strategy is Strategy


def test_boundary_models_have_only_the_approved_fields() -> None:
    assert tuple(MarketSnapshot.model_fields) == ("as_of", "market", "bars")
    assert tuple(StrategyContext.model_fields) == (
        "market_snapshot",
        "portfolio",
        "strategy_config_version",
    )
    forbidden = {
        "store",
        "execution",
        "order",
        "fill",
        "ledger",
        "network",
        "callback",
    }
    assert forbidden.isdisjoint(StrategyContext.model_fields)


def test_protocol_module_imports_no_authority_modules() -> None:
    tree = ast.parse(inspect.getsource(protocol_module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(
        name.startswith(
            (
                "stock_agent.account",
                "stock_agent.execution",
                "stock_agent.data",
                "stock_agent.risk",
            )
        )
        for name in imported
    )


def test_market_snapshot_accepts_empty_and_sorted_populated_tuples() -> None:
    first = bar("AAPL", date(2026, 7, 27), datetime(2026, 7, 27, 16, tzinfo=UTC))
    second = bar("AAPL", date(2026, 7, 28), datetime(2026, 7, 28, 16, tzinfo=UTC))
    third = bar("MSFT", date(2026, 7, 28), datetime(2026, 7, 28, 16, tzinfo=UTC))

    empty = MarketSnapshot(**snapshot_values())
    populated = MarketSnapshot(**snapshot_values(bars=(first, second, third)))

    assert empty.bars == ()
    assert populated.bars == (first, second, third)


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 7, 28, 16, tzinfo=UTC),
        datetime(2026, 7, 29, 9, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_market_snapshot_accepts_as_of_equal_to_or_later_than_bar(as_of: datetime) -> None:
    item = bar(available_at=datetime(2026, 7, 28, 16, tzinfo=UTC))
    assert MarketSnapshot(**snapshot_values(as_of=as_of, bars=(item,))).as_of == as_of


def test_market_snapshot_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(as_of=datetime(2026, 7, 28, 20)))


@pytest.mark.parametrize("market", ["US", 1])
def test_market_snapshot_requires_an_exact_market(market: object) -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(market=market))


def test_market_snapshot_rejects_cross_market_bars() -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=(bar(market=Market.CN),)))


def test_market_snapshot_rejects_future_bars() -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(
            **snapshot_values(
                bars=(bar(available_at=AS_OF + timedelta(microseconds=1)),),
            )
        )


def test_market_snapshot_rejects_duplicate_symbol_session_pairs() -> None:
    original = bar(available_at=datetime(2026, 7, 28, 16, tzinfo=UTC))
    revision = bar(available_at=datetime(2026, 7, 28, 17, tzinfo=UTC))
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=(original, revision)))


def test_market_snapshot_rejects_unsorted_bars() -> None:
    earlier = bar("AAPL", date(2026, 7, 27), datetime(2026, 7, 27, 16, tzinfo=UTC))
    later = bar("MSFT", date(2026, 7, 28), datetime(2026, 7, 28, 16, tzinfo=UTC))
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=(later, earlier)))


@pytest.mark.parametrize("bars", [[bar()], {bar()}])
def test_market_snapshot_rejects_non_tuple_bar_collections(bars: object) -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=bars))


def test_market_snapshot_rejects_tuple_subclasses() -> None:
    class TupleSubclass(tuple[Bar, ...]):
        pass

    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=TupleSubclass((bar(),))))


def test_market_snapshot_rejects_bar_subclasses() -> None:
    class MutableBar(Bar):
        model_config = ConfigDict(frozen=False)

    item = MutableBar(**bar().model_dump())
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=(item,)))


def test_market_snapshot_cannot_be_built_from_copy_updated_bar() -> None:
    with pytest.raises(TypeError):
        MarketSnapshot(**snapshot_values(bars=(bar().model_copy(update={"volume": []}),)))


@pytest.mark.parametrize("field", ["volume", "available_at"])
def test_market_snapshot_revalidates_constructed_bars(field: str) -> None:
    values = {name: getattr(bar(), name) for name in Bar.model_fields}
    values[field] = []
    polluted = Bar.model_construct(**values)

    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(bars=(polluted,)))


def test_market_snapshot_is_frozen_transitively_and_forbids_extras() -> None:
    item = bar()
    snapshot = MarketSnapshot(**snapshot_values(bars=(item,)))

    with pytest.raises(ValidationError):
        snapshot.market = Market.CN
    with pytest.raises(ValidationError):
        snapshot.bars += (item,)
    with pytest.raises(ValidationError):
        snapshot.bars[0].close = Decimal("1")
    with pytest.raises(ValidationError):
        MarketSnapshot(**snapshot_values(unknown=True))


def test_market_snapshot_forbids_copy_updates() -> None:
    snapshot = MarketSnapshot(**snapshot_values())
    with pytest.raises(TypeError):
        snapshot.model_copy(update={"market": Market.CN})


def test_boundary_models_allow_empty_copy_updates() -> None:
    boundary_models = (
        MarketSnapshot(**snapshot_values()),
        StrategyContext(**context_values()),
    )
    for model in boundary_models:
        assert model.model_copy(update={}) == model
        with pytest.warns(DeprecationWarning):
            assert model.copy(update={}) == model


def test_boundary_models_reject_nonempty_deprecated_copy_updates() -> None:
    boundary_models = (
        MarketSnapshot(**snapshot_values()),
        StrategyContext(**context_values()),
    )
    for model in boundary_models:
        field = next(iter(type(model).model_fields))
        for value in (getattr(model, field), []):
            with pytest.raises(TypeError):
                model.copy(update={field: value})


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 7, 28, 9, tzinfo=UTC),
        datetime(2026, 7, 29, 21, tzinfo=UTC),
    ],
)
def test_strategy_context_accepts_matching_early_and_late_instants(as_of: datetime) -> None:
    market_snapshot = MarketSnapshot(**snapshot_values(as_of=as_of))
    context = StrategyContext(
        market_snapshot=market_snapshot,
        portfolio=portfolio(as_of=as_of),
        strategy_config_version="  version-1  ",
    )

    assert context.market_snapshot == market_snapshot
    assert context.market_snapshot is not market_snapshot
    assert context.portfolio.as_of == as_of
    assert context.strategy_config_version == "version-1"


def test_strategy_context_rejects_market_mismatch() -> None:
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=portfolio(market=Market.CN)))


def test_strategy_context_rejects_any_unequal_as_of() -> None:
    with pytest.raises(ValidationError):
        StrategyContext(
            **context_values(portfolio=portfolio(as_of=AS_OF + timedelta(microseconds=1)))
        )


@pytest.mark.parametrize("version", ["", "   ", 1, None])
def test_strategy_context_rejects_invalid_config_versions(version: object) -> None:
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(strategy_config_version=version))


def test_strategy_context_rejects_string_subclasses() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(ValidationError):
        StrategyContext(**context_values(strategy_config_version=StringSubclass("v1")))


def test_strategy_context_rejects_snapshot_subclasses() -> None:
    class MutableMarketSnapshot(MarketSnapshot):
        model_config = ConfigDict(frozen=False)

    nested = MutableMarketSnapshot(**snapshot_values())
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(market_snapshot=nested))


def test_strategy_context_rejects_portfolio_subclasses() -> None:
    class MutablePortfolioSnapshot(PortfolioSnapshot):
        model_config = ConfigDict(frozen=False)

    nested = MutablePortfolioSnapshot(**portfolio().model_dump())
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=nested))


def test_strategy_context_cannot_be_built_from_copy_updated_portfolio() -> None:
    with pytest.raises(TypeError):
        StrategyContext(
            **context_values(portfolio=portfolio().model_copy(update={"account_id": []}))
        )


def test_strategy_context_rejects_non_tuple_positions() -> None:
    nested = portfolio()
    object.__setattr__(nested, "positions", [])
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=nested))


def test_strategy_context_rejects_position_tuple_subclasses() -> None:
    class TupleSubclass(tuple[Position, ...]):
        pass

    nested = portfolio()
    object.__setattr__(nested, "positions", TupleSubclass(()))
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=nested))


def test_strategy_context_rejects_position_subclasses() -> None:
    class MutablePosition(Position):
        model_config = ConfigDict(frozen=False)

    nested_position = MutablePosition(**position().model_dump())
    original = portfolio()
    values = {name: getattr(original, name) for name in PortfolioSnapshot.model_fields}
    values["positions"] = (nested_position,)
    values["nav"] = values["peak_nav"] = Decimal("1100")
    nested = PortfolioSnapshot.model_construct(**values)
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=nested))


@pytest.mark.parametrize("subclass_first", [False, True])
def test_strategy_context_rejects_mixed_exact_and_subclass_positions(
    subclass_first: bool,
) -> None:
    class MutablePosition(Position):
        model_config = ConfigDict(frozen=False)

    exact = Position(
        symbol="MSFT",
        quantity=Decimal("1"),
        average_cost=Decimal("100"),
        market_value=Decimal("100"),
    )
    mutable = MutablePosition(**position().model_dump())
    positions = (mutable, exact) if subclass_first else (exact, mutable)
    original = portfolio()
    values = {name: getattr(original, name) for name in PortfolioSnapshot.model_fields}
    values["positions"] = positions
    values["nav"] = values["peak_nav"] = Decimal("1200")
    polluted = PortfolioSnapshot.model_construct(**values)

    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=polluted))


def test_strategy_context_cannot_be_built_from_copy_updated_position() -> None:
    with pytest.raises(TypeError):
        StrategyContext(
            **context_values(
                portfolio=portfolio(positions=(position().model_copy(update={"average_cost": []}),))
            )
        )


def test_portfolio_snapshot_revalidates_constructed_positions() -> None:
    values = {name: getattr(position(), name) for name in Position.model_fields}
    values["average_cost"] = []
    polluted = Position.model_construct(**values)

    with pytest.raises(ValidationError):
        portfolio(positions=(polluted,))


@pytest.mark.parametrize(
    ("field", "value"),
    [("account_id", []), ("positions", [])],
)
def test_strategy_context_revalidates_constructed_portfolios(
    field: str, value: object
) -> None:
    nested = portfolio()
    values = {name: getattr(nested, name) for name in PortfolioSnapshot.model_fields}
    values[field] = value
    polluted = PortfolioSnapshot.model_construct(**values)

    with pytest.raises(ValidationError):
        StrategyContext(**context_values(portfolio=polluted))


def test_strategy_context_revalidates_constructed_market_snapshot_collections() -> None:
    nested = MarketSnapshot(**snapshot_values())
    values = {name: getattr(nested, name) for name in MarketSnapshot.model_fields}
    values["bars"] = []
    polluted = MarketSnapshot.model_construct(**values)

    with pytest.raises(ValidationError):
        StrategyContext(**context_values(market_snapshot=polluted))


def test_strategy_context_revalidates_constructed_market_snapshot_bars() -> None:
    item = bar()
    bar_values = {name: getattr(item, name) for name in Bar.model_fields}
    bar_values["volume"] = []
    polluted_bar = Bar.model_construct(**bar_values)
    polluted_snapshot = MarketSnapshot.model_construct(
        **snapshot_values(bars=(polluted_bar,))
    )

    with pytest.raises(ValidationError):
        StrategyContext(**context_values(market_snapshot=polluted_snapshot))


def test_strategy_context_is_frozen_transitively_and_forbids_extras() -> None:
    nested_position = position()
    context = StrategyContext(
        **context_values(portfolio=portfolio(positions=(nested_position,)))
    )

    with pytest.raises(ValidationError):
        context.strategy_config_version = "v2"
    with pytest.raises(ValidationError):
        context.portfolio.positions += (nested_position,)
    with pytest.raises(ValidationError):
        context.portfolio.positions[0].market_value = Decimal("1")
    with pytest.raises(ValidationError):
        StrategyContext(**context_values(unknown=True))


def test_strategy_context_forbids_copy_updates() -> None:
    context = StrategyContext(**context_values())
    with pytest.raises(TypeError):
        context.model_copy(update={"strategy_config_version": "v2"})


def test_runtime_protocol_accepts_an_object_with_all_structural_members() -> None:
    class ConformingStrategy:
        strategy_id = "fixture"
        config_version = "v1"

        def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
            return ()

    assert isinstance(ConformingStrategy(), Strategy)


def test_runtime_protocol_rejects_an_object_missing_required_behavior() -> None:
    class MissingEvaluate:
        strategy_id = "fixture"
        config_version = "v1"

    assert not isinstance(MissingEvaluate(), Strategy)
