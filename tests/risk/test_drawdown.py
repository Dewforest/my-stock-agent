from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest
from pydantic import ConfigDict

from stock_agent.domain import Market, PortfolioSnapshot, Position, Side, StrategyIntent
from stock_agent.risk import RiskContext, RiskDecisionStatus, RiskEngine

AS_OF = datetime(2026, 7, 28, 12, tzinfo=UTC)


def portfolio(
    *,
    market: Market = Market.US,
    cash: str = "1000",
    nav: str = "1000",
    peak_nav: str = "1000",
    positions: tuple[Position, ...] = (),
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id="account-1",
        market=market,
        cash=Decimal(cash),
        nav=Decimal(nav),
        peak_nav=Decimal(peak_nav),
        positions=positions,
        as_of=AS_OF,
    )


def context(snapshot: PortfolioSnapshot | None = None) -> RiskContext:
    return RiskContext(
        portfolio=snapshot or portfolio(),
        instruments=(),
        day_start_available_cash=Decimal("1000"),
        new_position_notional_committed_today=Decimal("0"),
    )


def intent(
    side: Side = Side.BUY,
    *,
    market: Market = Market.US,
    target_weight: str = "0.4",
) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="momentum-v1",
        symbol="AAPL" if market is Market.US else "600519",
        market=market,
        side=side,
        target_weight=Decimal(target_weight),
        confidence=80,
        as_of=AS_OF,
        thesis="Earnings momentum",
        invalidation="Guidance cut",
    )


def test_full_cash_sell_is_approved_without_changing_direction() -> None:
    original = intent(Side.SELL, target_weight="0")

    decision = RiskEngine().evaluate(original, context())

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.original_intent is original
    assert decision.original_intent.side is Side.SELL
    assert decision.approved_target_weight == Decimal("0")
    assert decision.risk_reduction is None


def test_market_mismatch_is_rejected_before_other_rules() -> None:
    original = intent(market=Market.CN)

    decision = RiskEngine().evaluate(original, context())

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == ("MARKET_MISMATCH",)
    assert len(decision.reasons) == 1
    assert decision.risk_reduction is None


@pytest.mark.parametrize(("bad_intent", "bad_context"), [(object(), None), (None, object())])
def test_evaluate_requires_exact_input_types(
    bad_intent: object | None, bad_context: object | None
) -> None:
    with pytest.raises(TypeError):
        RiskEngine().evaluate(
            bad_intent if bad_intent is not None else intent(),  # type: ignore[arg-type]
            bad_context if bad_context is not None else context(),  # type: ignore[arg-type]
        )


def test_evaluate_rejects_mutable_input_subclasses() -> None:
    class MutableIntent(StrategyIntent):
        model_config = ConfigDict(frozen=False)

    class MutableContext(RiskContext):
        model_config = ConfigDict(frozen=False)

    base_context = context()
    mutable_intent = MutableIntent(**intent().model_dump())
    mutable_context = MutableContext(
        portfolio=base_context.portfolio,
        instruments=base_context.instruments,
        day_start_available_cash=base_context.day_start_available_cash,
        new_position_notional_committed_today=base_context.new_position_notional_committed_today,
    )

    with pytest.raises(TypeError):
        RiskEngine().evaluate(mutable_intent, context())
    with pytest.raises(TypeError):
        RiskEngine().evaluate(intent(), mutable_context)


def test_buy_at_exactly_fifteen_percent_drawdown_is_rejected() -> None:
    original = intent()
    decision = RiskEngine().evaluate(
        original,
        context(portfolio(cash="850", nav="850", peak_nav="1000")),
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == ("DRAWDOWN_BUY_BLOCK_15",)
    assert len(decision.reasons) == 1
    assert decision.risk_reduction is None


def test_buy_below_fifteen_percent_drawdown_is_approved() -> None:
    original = intent(target_weight="0.375")
    decision = RiskEngine().evaluate(
        original,
        context(portfolio(cash="850.01", nav="850.01", peak_nav="1000")),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight is original.target_weight
    assert decision.rule_ids == ()
    assert decision.reasons == ()


def test_sell_at_fifteen_percent_drawdown_remains_permitted() -> None:
    original = intent(Side.SELL, target_weight="0")
    decision = RiskEngine().evaluate(
        original,
        context(portfolio(cash="850", nav="850", peak_nav="1000")),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.original_intent.side is Side.SELL
    assert decision.approved_target_weight == Decimal("0")
    assert decision.rule_ids == ()
    assert decision.reasons == ()


def invested_portfolio_at_twenty_percent_drawdown() -> PortfolioSnapshot:
    holding = Position(
        symbol="AAPL",
        quantity=Decimal("4.8"),
        average_cost=Decimal("100"),
        market_value=Decimal("480"),
    )
    return portfolio(cash="320", nav="800", peak_nav="1000", positions=(holding,))


def test_exact_twenty_percent_drawdown_halves_current_gross_exposure() -> None:
    decision = RiskEngine().evaluate(
        intent(Side.HOLD, target_weight="0.4"),
        context(invested_portfolio_at_twenty_percent_drawdown()),
    )

    assert decision.risk_reduction is not None
    assert decision.risk_reduction.current_gross_exposure == Decimal("0.6")
    assert decision.risk_reduction.target_gross_exposure == Decimal("0.3")
    assert decision.risk_reduction.review_required is True
    assert decision.rule_ids == ("DRAWDOWN_RISK_REDUCTION_20",)
    assert len(decision.reasons) == 1


def test_buy_at_twenty_percent_drawdown_is_rejected_with_ordered_rules_and_directive() -> None:
    decision = RiskEngine().evaluate(
        intent(),
        context(invested_portfolio_at_twenty_percent_drawdown()),
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == (
        "DRAWDOWN_RISK_REDUCTION_20",
        "DRAWDOWN_BUY_BLOCK_15",
    )
    assert len(decision.reasons) == 2
    assert decision.risk_reduction is not None
    assert decision.risk_reduction.target_gross_exposure == Decimal("0.3")


@pytest.mark.parametrize(
    ("side", "target"),
    [(Side.SELL, "0"), (Side.HOLD, "0.4"), (Side.REDUCE, "0.2")],
)
def test_non_buy_at_twenty_percent_drawdown_preserves_direction_and_target(
    side: Side, target: str
) -> None:
    original = intent(side, target_weight=target)
    decision = RiskEngine().evaluate(
        original,
        context(invested_portfolio_at_twenty_percent_drawdown()),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.original_intent is original
    assert decision.original_intent.side is side
    assert decision.approved_target_weight is original.target_weight
    assert decision.rule_ids == ("DRAWDOWN_RISK_REDUCTION_20",)
    assert decision.risk_reduction is not None


def test_twenty_percent_drawdown_with_zero_positions_has_zero_reduction_target() -> None:
    decision = RiskEngine().evaluate(
        intent(Side.HOLD, target_weight="0"),
        context(portfolio(cash="800", nav="800", peak_nav="1000")),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.risk_reduction is not None
    assert decision.risk_reduction.current_gross_exposure == Decimal("0")
    assert decision.risk_reduction.target_gross_exposure == Decimal("0")


def test_zero_peak_and_nav_blocks_only_buy_with_zero_nav_rule() -> None:
    empty = context(portfolio(cash="0", nav="0", peak_nav="0"))

    buy_decision = RiskEngine().evaluate(intent(), empty)
    sell_decision = RiskEngine().evaluate(intent(Side.SELL, target_weight="0"), empty)

    assert buy_decision.status is RiskDecisionStatus.REJECTED
    assert buy_decision.approved_target_weight is None
    assert buy_decision.rule_ids == ("ZERO_NAV_BUY_BLOCK",)
    assert len(buy_decision.reasons) == 1
    assert buy_decision.risk_reduction is None
    assert sell_decision.status is RiskDecisionStatus.APPROVED
    assert sell_decision.approved_target_weight == Decimal("0")
    assert sell_decision.rule_ids == ()


def test_evaluate_ignores_hostile_ambient_decimal_context() -> None:
    original = intent(Side.HOLD, target_weight="0.4")
    risk_context = context(invested_portfolio_at_twenty_percent_drawdown())
    expected = RiskEngine().evaluate(original, risk_context).model_dump(mode="json")

    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_UP
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        actual = RiskEngine().evaluate(original, risk_context).model_dump(mode="json")

    assert actual == expected


def test_evaluate_is_deterministic_and_does_not_mutate_inputs() -> None:
    original = intent()
    risk_context = context(invested_portfolio_at_twenty_percent_drawdown())
    intent_before = original.model_dump(mode="json")
    context_before = risk_context.model_dump(mode="json")

    first = RiskEngine().evaluate(original, risk_context)
    second = RiskEngine().evaluate(original, risk_context)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert original.model_dump(mode="json") == intent_before
    assert risk_context.model_dump(mode="json") == context_before
    assert first.original_intent is original
