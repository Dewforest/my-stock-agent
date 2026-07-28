from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import MAX_EMAX, ROUND_UP, Decimal, Inexact, localcontext

import pytest
from pydantic import ConfigDict

from stock_agent.domain import (
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Position,
    Side,
    StrategyIntent,
)
from stock_agent.risk import RiskContext, RiskDecisionStatus, RiskEngine

AS_OF = datetime(2026, 7, 28, 12, tzinfo=UTC)


def instrument(symbol: str, sector: str = "Technology") -> Instrument:
    return Instrument(symbol=symbol, market=Market.US, currency=Currency.USD, sector=sector)


def position(symbol: str, market_value: str = "10") -> Position:
    value = Decimal(market_value)
    return Position(
        symbol=symbol,
        quantity=Decimal("1"),
        average_cost=value or Decimal("1"),
        market_value=value,
    )


def portfolio(
    *,
    cash: str = "1000",
    nav: str = "1000",
    positions: tuple[Position, ...] = (),
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal(cash),
        nav=Decimal(nav),
        peak_nav=Decimal(nav),
        positions=positions,
        as_of=AS_OF,
    )


def context(
    *,
    instruments: tuple[Instrument, ...] = (),
    snapshot: PortfolioSnapshot | None = None,
    day_start_cash: str = "1000",
    committed: str = "0",
) -> RiskContext:
    return RiskContext(
        portfolio=snapshot or portfolio(),
        instruments=instruments,
        day_start_available_cash=Decimal(day_start_cash),
        new_position_notional_committed_today=Decimal(committed),
    )


def intent(symbol: str = "AAPL", target: str = "0.10", side: Side = Side.BUY) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="momentum-v1",
        symbol=symbol,
        market=Market.US,
        side=side,
        target_weight=Decimal(target),
        confidence=80,
        as_of=AS_OF,
        thesis="Earnings momentum",
        invalidation="Guidance cut",
    )


def test_evaluate_many_empty_and_single_item_match_evaluate() -> None:
    engine = RiskEngine()
    risk_context = context(instruments=(instrument("AAPL"),))
    original = intent()

    assert engine.evaluate_many((), risk_context) == ()
    assert engine.evaluate_many((original,), risk_context) == (
        engine.evaluate(original, risk_context),
    )


def test_evaluate_many_requires_exact_immutable_unique_inputs_without_mutation() -> None:
    class IntentTuple(tuple[StrategyIntent, ...]):
        pass

    class MutableIntent(StrategyIntent):
        model_config = ConfigDict(frozen=False)

    class MutableContext(RiskContext):
        model_config = ConfigDict(frozen=False)

    original = intent()
    risk_context = context(instruments=(instrument("AAPL"),))
    mutable_intent = MutableIntent(**original.model_dump())
    mutable_context = MutableContext(
        portfolio=risk_context.portfolio,
        instruments=risk_context.instruments,
        day_start_available_cash=risk_context.day_start_available_cash,
        new_position_notional_committed_today=risk_context.new_position_notional_committed_today,
    )
    intent_before = original.model_dump(mode="json")
    context_before = risk_context.model_dump(mode="json")

    with pytest.raises(TypeError):
        RiskEngine().evaluate_many([original], risk_context)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RiskEngine().evaluate_many(IntentTuple((original,)), risk_context)
    with pytest.raises(TypeError):
        RiskEngine().evaluate_many((mutable_intent,), risk_context)
    with pytest.raises(TypeError):
        RiskEngine().evaluate_many((original,), mutable_context)
    with pytest.raises(ValueError):
        RiskEngine().evaluate_many((original, intent(symbol="AAPL")), risk_context)

    assert original.model_dump(mode="json") == intent_before
    assert risk_context.model_dump(mode="json") == context_before


def test_pending_new_buys_reserve_holding_slots_but_calls_are_isolated() -> None:
    held = tuple(position(f"P{index}") for index in range(9))
    instruments = tuple(
        [instrument(item.symbol, f"Held {index}") for index, item in enumerate(held)]
        + [instrument("NEW1", "New 1"), instrument("NEW2", "New 2")]
    )
    risk_context = context(
        snapshot=portfolio(cash="910", positions=held),
        instruments=instruments,
        day_start_cash="10000",
    )
    first = intent("NEW1", "0.05")
    second = intent("NEW2", "0.05")

    decisions = RiskEngine().evaluate_many((first, second), risk_context)

    assert decisions[0].status is RiskDecisionStatus.APPROVED
    assert decisions[1].status is RiskDecisionStatus.REJECTED
    assert decisions[1].rule_ids == ("HOLDING_COUNT_MAX_10",)
    assert RiskEngine().evaluate(first, risk_context).status is RiskDecisionStatus.APPROVED
    assert RiskEngine().evaluate(second, risk_context).status is RiskDecisionStatus.APPROVED


def test_pending_buys_accumulate_sector_weight_and_replace_existing_snapshot_weight() -> None:
    held = (position("MSFT", "200"), position("XOM", "700"))
    instruments = (
        instrument("MSFT"),
        instrument("XOM", "Energy"),
        instrument("AAPL"),
        instrument("GOOG"),
        instrument("META"),
    )
    risk_context = context(
        snapshot=portfolio(cash="100", positions=held),
        instruments=instruments,
        day_start_cash="10000",
    )

    decisions = RiskEngine().evaluate_many(
        (intent("AAPL", "0.05"), intent("GOOG", "0.05"), intent("META", "0.05")),
        risk_context,
    )

    assert [decision.status for decision in decisions] == [
        RiskDecisionStatus.APPROVED,
        RiskDecisionStatus.APPROVED,
        RiskDecisionStatus.REJECTED,
    ]
    assert decisions[2].rule_ids == ("SECTOR_MAX_30",)

    replacing_context = context(
        snapshot=portfolio(
            cash="50", positions=(position("AAPL", "250"), position("XOM", "700"))
        ),
        instruments=(instrument("AAPL"), instrument("GOOG"), instrument("XOM", "Energy")),
        day_start_cash="10000",
    )
    replacing = RiskEngine().evaluate_many(
        (intent("AAPL", "0.05"), intent("GOOG", "0.15")), replacing_context
    )
    assert [decision.status for decision in replacing] == [
        RiskDecisionStatus.APPROVED,
        RiskDecisionStatus.APPROVED,
    ]


def test_daily_projection_commits_approved_weight_and_rejections_do_not_commit() -> None:
    risk_context = context(
        instruments=(instrument("AAPL", "Tech"), instrument("XOM", "Energy")),
        snapshot=portfolio(cash="1000"),
        day_start_cash="500",
    )
    decisions = RiskEngine().evaluate_many(
        (intent("AAPL", "0.40"), intent("XOM", "0.40")), risk_context
    )

    assert decisions[0].status is RiskDecisionStatus.CLAMPED
    assert decisions[0].approved_target_weight == Decimal("0.15")
    assert decisions[0].rule_ids == ("SINGLE_STOCK_MAX_15",)
    assert decisions[1].status is RiskDecisionStatus.REJECTED
    assert decisions[1].rule_ids == (
        "SINGLE_STOCK_MAX_15",
        "DAILY_NEW_POSITION_CASH_MAX_30",
    )

    cumulative = context(
        instruments=(instrument("AAPL", "Tech"), instrument("XOM", "Energy")),
        committed="100",
        day_start_cash="500",
    )
    cumulative_decisions = RiskEngine().evaluate_many(
        (intent("AAPL", "0.05"), intent("XOM", "0.05")), cumulative
    )
    assert cumulative_decisions[0].status is RiskDecisionStatus.APPROVED
    assert cumulative_decisions[1].status is RiskDecisionStatus.REJECTED
    assert cumulative_decisions[1].rule_ids == ("DAILY_NEW_POSITION_CASH_MAX_30",)

    rejected_first = context(
        instruments=(instrument("XOM", "Energy"),),
        day_start_cash="500",
    )
    rejected_decisions = RiskEngine().evaluate_many(
        (intent("MISSING", "0.15"), intent("XOM", "0.15")), rejected_first
    )
    assert rejected_decisions[0].status is RiskDecisionStatus.REJECTED
    assert rejected_decisions[1].status is RiskDecisionStatus.APPROVED


def test_pending_sell_and_reduce_do_not_release_slot_or_sector_and_preserve_direction() -> None:
    held = tuple(position(f"P{index}", "30") for index in range(10))
    instruments = tuple(
        [instrument(item.symbol, "Technology") for item in held]
        + [instrument("NEW", "Energy")]
    )
    full_context = context(
        snapshot=portfolio(cash="700", positions=held),
        instruments=instruments,
        day_start_cash="10000",
    )
    sell = intent("P0", "0", Side.SELL)
    reduce = intent("P1", "0.01", Side.REDUCE)
    decisions = RiskEngine().evaluate_many(
        (sell, reduce, intent("NEW", "0.05")), full_context
    )

    assert decisions[0].original_intent.side is Side.SELL
    assert decisions[1].original_intent.side is Side.REDUCE
    assert decisions[0].approved_target_weight == Decimal("0")
    assert decisions[1].approved_target_weight == Decimal("0.01")
    assert decisions[2].rule_ids == ("HOLDING_COUNT_MAX_10",)

    sector_holding = position("P0", "300")
    sector_context = context(
        snapshot=portfolio(cash="700", positions=(sector_holding,)),
        instruments=(instrument("P0", "Technology"), instrument("NEW", "Technology")),
        day_start_cash="10000",
    )
    sector_decisions = RiskEngine().evaluate_many(
        (sell, intent("NEW", "0.05")), sector_context
    )
    assert sector_decisions[1].status is RiskDecisionStatus.REJECTED
    assert sector_decisions[1].rule_ids == ("SECTOR_MAX_30",)


def test_batch_is_hostile_context_safe_max_emax_deterministic_thread_safe_and_pure() -> None:
    with localcontext() as construction:
        construction.Emax = MAX_EMAX
        maximum = f"1E+{MAX_EMAX}"
        snapshot = portfolio(cash=maximum, nav=maximum)
        risk_context = context(
            snapshot=snapshot,
            instruments=(instrument("AAPL", "Tech"), instrument("XOM", "Energy")),
            day_start_cash=maximum,
        )
    intents = (intent("AAPL", "0.15"), intent("XOM", "0.15"))
    intents_before = tuple(item.model_dump(mode="json") for item in intents)
    context_before = risk_context.model_dump(mode="json")
    expected = RiskEngine().evaluate_many(intents, risk_context)

    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_UP
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        repeated = RiskEngine().evaluate_many(intents, risk_context)

    with ThreadPoolExecutor(max_workers=4) as executor:
        threaded = tuple(
            executor.map(
                lambda _: RiskEngine().evaluate_many(intents, risk_context), range(8)
            )
        )

    assert repeated == expected
    assert all(result == expected for result in threaded)
    assert tuple(item.model_dump(mode="json") for item in intents) == intents_before
    assert risk_context.model_dump(mode="json") == context_before


def test_empty_portfolio_reserves_only_first_ten_positive_buy_decisions() -> None:
    intents = tuple(intent(f"NEW{index}", "0.01") for index in range(11))
    instruments = tuple(
        instrument(item.symbol, f"Sector {index}")
        for index, item in enumerate(intents)
    )
    decisions = RiskEngine().evaluate_many(
        intents,
        context(instruments=instruments, day_start_cash="100000"),
    )

    assert all(decision.approved_target_weight == Decimal("0.01") for decision in decisions[:10])
    assert decisions[10].status is RiskDecisionStatus.REJECTED
    assert decisions[10].rule_ids == ("HOLDING_COUNT_MAX_10",)
