from datetime import UTC, datetime
from decimal import (
    MAX_EMAX,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Decimal,
    Inexact,
    localcontext,
)

import pytest

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
DAILY_RULE = "DAILY_NEW_POSITION_CASH_MAX_30"


def instrument(symbol: str = "AAPL", sector: str = "Technology") -> Instrument:
    return Instrument(
        symbol=symbol,
        market=Market.US,
        currency=Currency.USD,
        sector=sector,
    )


def position(symbol: str, market_value: str = "100") -> Position:
    value = Decimal(market_value)
    return Position(
        symbol=symbol,
        quantity=Decimal("1"),
        average_cost=value,
        market_value=value,
    )


def portfolio(
    *,
    nav: str = "1000",
    cash: str = "1000",
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
    day_start_cash: str = "1000",
    committed: str = "0",
    nav: str = "1000",
    cash: str = "1000",
    positions: tuple[Position, ...] = (),
    instruments: tuple[Instrument, ...] | None = None,
) -> RiskContext:
    return RiskContext(
        portfolio=portfolio(nav=nav, cash=cash, positions=positions),
        instruments=(instrument(),) if instruments is None else instruments,
        day_start_available_cash=Decimal(day_start_cash),
        new_position_notional_committed_today=Decimal(committed),
    )


def intent(*, target: str = "0.15", side: Side = Side.BUY) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="momentum-v1",
        symbol="AAPL",
        market=Market.US,
        side=side,
        target_weight=Decimal(target),
        confidence=80,
        as_of=AS_OF,
        thesis="Earnings momentum",
        invalidation="Guidance cut",
    )


def test_new_symbol_is_clamped_to_remaining_daily_cash_budget() -> None:
    decision = RiskEngine().evaluate(
        intent(),
        context(day_start_cash="1000", committed="200", nav="1000"),
    )

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == (DAILY_RULE,)
    assert decision.reasons == (
        "buy target exceeds remaining daily new-position cash budget of thirty percent",
    )


@pytest.mark.parametrize("committed", ["300", "301"])
def test_consumed_daily_budget_rejects_new_symbol(committed: str) -> None:
    decision = RiskEngine().evaluate(intent(), context(committed=committed))

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == (DAILY_RULE,)


def test_zero_day_start_cash_rejects_new_symbol() -> None:
    decision = RiskEngine().evaluate(intent(), context(day_start_cash="0"))

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == (DAILY_RULE,)


def test_existing_symbol_is_exempt_when_daily_budget_is_overconsumed() -> None:
    held = position("AAPL")
    decision = RiskEngine().evaluate(
        intent(),
        context(
            day_start_cash="0",
            committed="301",
            cash="900",
            positions=(held,),
        ),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ()


def test_current_cash_does_not_change_daily_budget_cap() -> None:
    full_cash = RiskEngine().evaluate(intent(), context(committed="200", cash="1000"))
    unrelated = position("XOM", "500")
    lower_cash = RiskEngine().evaluate(
        intent(),
        context(
            committed="200",
            cash="500",
            positions=(unrelated,),
            instruments=(instrument(), instrument("XOM", "Energy")),
        ),
    )

    assert full_cash.approved_target_weight == lower_cash.approved_target_weight == Decimal("0.10")
    assert full_cash.rule_ids == lower_cash.rule_ids == (DAILY_RULE,)


def test_zero_nav_rule_takes_priority_over_daily_budget() -> None:
    decision = RiskEngine().evaluate(
        intent(),
        context(day_start_cash="0", committed="301", nav="0", cash="0"),
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.rule_ids == ("ZERO_NAV_BUY_BLOCK",)


def test_stock_sector_and_daily_caps_compose_in_binding_order() -> None:
    same_sector = position("MSFT", "180")
    decision = RiskEngine().evaluate(
        intent(target="0.40"),
        context(
            committed="200",
            cash="820",
            positions=(same_sector,),
            instruments=(instrument(), instrument("MSFT")),
        ),
    )

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == (
        "SINGLE_STOCK_MAX_15",
        "SECTOR_EXPOSURE_MAX_30",
        DAILY_RULE,
    )


def test_equal_daily_cap_is_not_binding() -> None:
    decision = RiskEngine().evaluate(intent(target="0.10"), context(committed="200"))

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.10")
    assert DAILY_RULE not in decision.rule_ids


def test_zero_daily_cap_preserves_earlier_binding_rules_and_rejects() -> None:
    same_sector = position("MSFT", "200")
    decision = RiskEngine().evaluate(
        intent(target="0.40"),
        context(
            committed="300",
            cash="800",
            positions=(same_sector,),
            instruments=(instrument(), instrument("MSFT")),
        ),
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == (
        "SINGLE_STOCK_MAX_15",
        "SECTOR_EXPOSURE_MAX_30",
        DAILY_RULE,
    )


def test_recurring_daily_ratio_is_rounded_down_conservatively() -> None:
    with localcontext() as old_arithmetic:
        old_arithmetic.prec = 128
        old_arithmetic.rounding = ROUND_HALF_EVEN
        old_ratio = Decimal(1) / Decimal(11)
    with localcontext() as exact_check:
        exact_check.prec = 256
        assert old_ratio * Decimal(11) > Decimal(1)

    decision = RiskEngine().evaluate(
        intent(target=str(old_ratio)),
        context(day_start_cash="10", committed="2", nav="11", cash="11"),
    )

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.rule_ids == (DAILY_RULE,)
    assert decision.approved_target_weight is not None
    assert decision.approved_target_weight < old_ratio
    with localcontext() as exact_check:
        exact_check.prec = 256
        assert decision.approved_target_weight * Decimal(11) <= Decimal(1)


def test_cross_magnitude_nonbinding_daily_budget_is_approved() -> None:
    with localcontext() as construction:
        construction.Emax = MAX_EMAX
        day_start_cash = f"9E+{MAX_EMAX}"
        risk_context = context(
            day_start_cash=day_start_cash,
            committed="0",
            nav=".01",
            cash=".01",
        )

    decision = RiskEngine().evaluate(intent(), risk_context)

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ()


def test_exact_daily_ratio_is_not_mistakenly_clamped() -> None:
    decision = RiskEngine().evaluate(
        intent(target="0.10"),
        context(day_start_cash="10", committed="2", nav="10", cash="10"),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == ()


def max_emax_context(*, committed_coefficient: int) -> RiskContext:
    with localcontext() as construction:
        construction.Emax = MAX_EMAX
        nav = f"1E+{MAX_EMAX}"
        committed = f"{committed_coefficient}E+{MAX_EMAX - 1}"
        return context(
            day_start_cash=nav,
            committed=committed,
            nav=nav,
            cash=nav,
        )


def test_max_emax_daily_budget_ratio_is_nonbinding_at_stock_cap() -> None:
    decision = RiskEngine().evaluate(intent(), max_emax_context(committed_coefficient=0))

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ()


def test_max_emax_same_exponent_remaining_budget_clamps_to_ten_percent() -> None:
    decision = RiskEngine().evaluate(intent(), max_emax_context(committed_coefficient=2))

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == (DAILY_RULE,)


def test_daily_budget_is_hostile_context_safe_deterministic_and_pure() -> None:
    original = intent(target="0.12")
    risk_context = context(day_start_cash="10", committed="2", nav="11", cash="11")
    intent_before = original.model_dump(mode="json")
    context_before = risk_context.model_dump(mode="json")
    expected = RiskEngine().evaluate(original, risk_context).model_dump(mode="json")

    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_UP
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        first = RiskEngine().evaluate(original, risk_context).model_dump(mode="json")
        second = RiskEngine().evaluate(original, risk_context).model_dump(mode="json")

    assert first == second == expected
    assert original.model_dump(mode="json") == intent_before
    assert risk_context.model_dump(mode="json") == context_before


@pytest.mark.parametrize(
    ("side", "target"),
    [(Side.SELL, "0"), (Side.HOLD, "0.4"), (Side.REDUCE, "0.2")],
)
def test_non_buy_intents_are_exempt_from_daily_budget(side: Side, target: str) -> None:
    decision = RiskEngine().evaluate(
        intent(side=side, target=target),
        context(day_start_cash="0", committed="1000"),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal(target)
    assert DAILY_RULE not in decision.rule_ids
