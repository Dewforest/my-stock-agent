from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import ROUND_UP, Decimal, getcontext, localcontext

import pytest
from pydantic import ValidationError

from stock_agent.backtest import (
    OrderPlanSource,
    OrderPlanStatus,
    plan_orders,
    record_submission,
)
from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, Side, StrategyIntent
from stock_agent.execution import Fill, FillStatus
from stock_agent.risk import (
    RiskDecision,
    RiskDecisionStatus,
    RiskReductionTarget,
)
from stock_agent.strategies import MarketSnapshot

SESSION = date(2026, 7, 28)
AS_OF = datetime(2026, 7, 28, 20, tzinfo=UTC)


def bar(symbol: str = "AAPL", close: str = "100") -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        market=Market.US,
        session_date=SESSION,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1"),
        available_at=AS_OF,
    )


def intent(
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    target: str = "0.2",
) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="strategy-1",
        symbol=symbol,
        market=Market.US,
        side=side,
        target_weight=Decimal(target),
        confidence=100,
        as_of=AS_OF,
        thesis="test",
        invalidation="test",
    )


def decision(value: StrategyIntent, target: str | None = None) -> RiskDecision:
    return RiskDecision(
        original_intent=value,
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal(target) if target is not None else value.target_weight,
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


def planning(
    *decisions: RiskDecision,
    snapshot: MarketSnapshot | None = None,
    held: PortfolioSnapshot | None = None,
    reduction: RiskReductionTarget | None = None,
) -> tuple:
    return plan_orders(
        run_id="run-1",
        decision_session=SESSION,
        strategy_id="strategy-1",
        market_snapshot=snapshot or MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(),)),
        portfolio=held or portfolio(),
        risk_decisions=decisions,
        portfolio_reduction=reduction,
    )


def test_buy_positive_delta_creates_ready_order_with_hand_calculated_quantity() -> None:
    snapshot = MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(),))
    portfolio = PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("1000"),
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        positions=(),
        as_of=AS_OF,
    )

    plans = plan_orders(
        run_id="run-1",
        decision_session=SESSION,
        strategy_id="strategy-1",
        market_snapshot=snapshot,
        portfolio=portfolio,
        risk_decisions=(decision(intent()),),
        portfolio_reduction=None,
    )

    assert len(plans) == 1
    assert plans[0].status is OrderPlanStatus.READY
    assert plans[0].raw_quantity == Decimal("2")
    assert plans[0].submitted_quantity == Decimal("2.000000000000")
    assert plans[0].order is not None
    assert plans[0].order.side is Side.BUY
    assert plans[0].order.quantity == Decimal("2.000000000000")


@pytest.mark.parametrize(
    ("risk_decision", "expected_reason"),
    [
        (
            RiskDecision(
                original_intent=intent(),
                status=RiskDecisionStatus.REJECTED,
                approved_target_weight=None,
                rule_ids=("BLOCK",),
                reasons=("blocked",),
            ),
            "risk decision rejected: blocked",
        ),
        (decision(intent(side=Side.HOLD, target="0")), "HOLD intent has no order"),
    ],
)
def test_rejected_and_hold_decisions_are_audited_without_orders(
    risk_decision: RiskDecision, expected_reason: str
) -> None:
    plan = planning(risk_decision)[0]

    expected_status = (
        OrderPlanStatus.REJECTED
        if risk_decision.status is RiskDecisionStatus.REJECTED
        else OrderPlanStatus.SKIPPED
    )
    assert plan.status is expected_status
    assert plan.order is None
    assert plan.reason == expected_reason


def test_zero_delta_is_skipped() -> None:
    held = portfolio(
        cash="800",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("2"),
                average_cost=Decimal("90"),
                market_value=Decimal("200"),
            ),
        ),
    )

    plan = planning(decision(intent()), held=held)[0]

    assert plan.status is OrderPlanStatus.SKIPPED
    assert plan.raw_quantity == Decimal("0")
    assert plan.reason == "target delta is zero"


def test_reduce_negative_delta_becomes_sell() -> None:
    held = portfolio(
        cash="700",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("3"),
                average_cost=Decimal("80"),
                market_value=Decimal("300"),
            ),
        ),
    )

    plan = planning(decision(intent(side=Side.REDUCE)), held=held)[0]

    assert plan.status is OrderPlanStatus.READY
    assert plan.raw_quantity == Decimal("1")
    assert plan.order is not None and plan.order.side is Side.SELL


def test_sell_uses_exact_held_quantity_and_no_holding_skips() -> None:
    held = portfolio(
        cash="700",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("3.1234567890129"),
                average_cost=Decimal("80"),
                market_value=Decimal("300"),
            ),
        ),
    )

    ready = planning(decision(intent(side=Side.SELL, target="0")), held=held)[0]
    skipped = planning(decision(intent(side=Side.SELL, target="0")))[0]

    assert ready.raw_quantity == Decimal("3.1234567890129")
    assert ready.submitted_quantity == Decimal("3.1234567890129")
    assert ready.order is not None and ready.order.quantity == Decimal("3.1234567890129")
    assert skipped.status is OrderPlanStatus.SKIPPED


def test_sell_rejects_nonzero_approved_target_even_for_a_valid_decision() -> None:
    held = portfolio(
        cash="700",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("3"),
                average_cost=Decimal("80"),
                market_value=Decimal("300"),
            ),
        ),
    )
    approved = decision(intent(side=Side.SELL, target="0"), target="0.1")

    plan = planning(approved, held=held)[0]

    assert plan.status is OrderPlanStatus.REJECTED
    assert plan.reason == "SELL intent requires zero approved target weight"
    assert plan.order is None


@pytest.mark.parametrize("quantity", [Decimal("1E26"), Decimal("1E100")])
def test_sell_rejects_held_quantity_outside_planning_range(quantity: Decimal) -> None:
    held = portfolio(
        cash="0",
        nav=str(quantity),
        positions=(
            Position(
                symbol="AAPL",
                quantity=quantity,
                average_cost=Decimal("1"),
                market_value=quantity,
            ),
        ),
    )

    plan = planning(decision(intent(side=Side.SELL, target="0")), held=held)[0]

    assert plan.status is OrderPlanStatus.REJECTED
    assert plan.reason == "quantity exceeds supported planning range"
    assert plan.order is None


@pytest.mark.parametrize(
    "risk_decision",
    [decision(intent(side=Side.REDUCE), target="0.2")],
)
def test_side_delta_mismatches_are_rejected_without_reversing_side(
    risk_decision: RiskDecision,
) -> None:
    plan = planning(risk_decision)[0]

    assert plan.status is OrderPlanStatus.REJECTED
    assert plan.order is None


def test_buy_negative_delta_is_rejected() -> None:
    held = portfolio(
        cash="700",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("3"),
                average_cost=Decimal("80"),
                market_value=Decimal("300"),
            ),
        ),
    )

    plan = planning(decision(intent(side=Side.BUY), target="0.2"), held=held)[0]

    assert plan.status is OrderPlanStatus.REJECTED
    assert plan.reason == "BUY requires a positive target delta"
    assert plan.order is None


def test_quantity_is_floored_to_twelve_places_and_dust_is_skipped() -> None:
    exact_nav = "150.1234567890129"
    floored = planning(
        decision(intent(target="1")),
        snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(close="1"),)),
        held=portfolio(cash=exact_nav, nav=exact_nav),
    )[0]
    dust = planning(
        decision(intent(target="0.000000000001")),
        snapshot=MarketSnapshot(
            as_of=AS_OF,
            market=Market.US,
            bars=(bar(close="10000000000000"),),
        ),
        held=portfolio(cash="1", nav="1"),
    )[0]

    assert floored.raw_quantity == Decimal(exact_nav)
    assert floored.submitted_quantity == Decimal("150.123456789012")
    assert dust.status is OrderPlanStatus.SKIPPED
    assert dust.raw_quantity == Decimal("1E-25")


def test_missing_current_close_is_a_stable_fatal_error() -> None:
    with pytest.raises(ValueError, match="missing current close for symbols: AAPL"):
        planning(
            decision(intent()),
            snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=()),
        )


def reduction_target() -> RiskReductionTarget:
    return RiskReductionTarget(
        current_gross_exposure=Decimal("0.8"),
        target_gross_exposure=Decimal("0.4"),
    )


def held_two() -> PortfolioSnapshot:
    return portfolio(
        cash="400",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("2"),
                average_cost=Decimal("90"),
                market_value=Decimal("200"),
            ),
            Position(
                symbol="MSFT",
                quantity=Decimal("4"),
                average_cost=Decimal("90"),
                market_value=Decimal("400"),
            ),
        ),
    )


def reduced_decision(
    value: StrategyIntent,
    reduction: RiskReductionTarget,
    *,
    target: str | None = None,
    rejected: bool = False,
) -> RiskDecision:
    return RiskDecision(
        original_intent=value,
        status=RiskDecisionStatus.REJECTED if rejected else RiskDecisionStatus.APPROVED,
        approved_target_weight=None if rejected else Decimal(target or str(value.target_weight)),
        rule_ids=("BLOCK",) if rejected else (),
        reasons=("blocked",) if rejected else (),
        risk_reduction=reduction,
    )


def test_empty_intent_reduction_halves_each_current_weight_in_symbol_order() -> None:
    reduction = reduction_target()
    plans = planning(
        snapshot=MarketSnapshot(
            as_of=AS_OF,
            market=Market.US,
            bars=(bar("AAPL"), bar("MSFT")),
        ),
        held=held_two(),
        reduction=reduction,
    )

    assert tuple(plan.symbol for plan in plans) == ("AAPL", "MSFT")
    assert tuple(plan.target_weight for plan in plans) == (
        Decimal("0.100000000000"),
        Decimal("0.200000000000"),
    )
    assert tuple(plan.raw_quantity for plan in plans) == (Decimal("1"), Decimal("2"))
    assert all(plan.source is OrderPlanSource.RISK_REDUCTION for plan in plans)
    assert all(plan.order is not None and plan.order.side is Side.SELL for plan in plans)


def test_reduction_preserves_lower_strategy_target_and_overrides_rejected_decision() -> None:
    reduction = reduction_target()
    aapl = reduced_decision(
        intent(side=Side.REDUCE, target="0.05"), reduction, target="0.05"
    )
    msft = reduced_decision(
        intent("MSFT", side=Side.BUY, target="0.9"), reduction, rejected=True
    )

    plans = planning(
        aapl,
        msft,
        snapshot=MarketSnapshot(
            as_of=AS_OF,
            market=Market.US,
            bars=(bar("AAPL"), bar("MSFT")),
        ),
        held=held_two(),
        reduction=reduction,
    )

    assert tuple(plan.symbol for plan in plans) == ("AAPL", "MSFT")
    assert plans[0].target_weight == Decimal("0.05")
    assert plans[0].raw_quantity == Decimal("1.5")
    assert plans[1].status is OrderPlanStatus.READY
    assert plans[1].source is OrderPlanSource.RISK_REDUCTION
    assert len({plan.symbol for plan in plans}) == len(plans)


def test_reduction_order_follows_decisions_then_absent_holdings_and_leaves_new_symbols() -> None:
    reduction = reduction_target()
    msft = reduced_decision(intent("MSFT", side=Side.REDUCE, target="0.1"), reduction)
    new = reduced_decision(intent("NVDA", side=Side.BUY, target="0.1"), reduction)

    plans = planning(
        msft,
        new,
        snapshot=MarketSnapshot(
            as_of=AS_OF,
            market=Market.US,
            bars=(bar("AAPL"), bar("MSFT"), bar("NVDA")),
        ),
        held=held_two(),
        reduction=reduction,
    )

    assert tuple(plan.symbol for plan in plans) == ("MSFT", "NVDA", "AAPL")
    assert plans[0].source is OrderPlanSource.RISK_REDUCTION
    assert plans[1].source is OrderPlanSource.STRATEGY
    assert plans[2].source is OrderPlanSource.RISK_REDUCTION


def test_zero_nav_reduction_targets_zero_without_dividing() -> None:
    reduction = reduction_target()
    zero_nav = portfolio(
        cash="0",
        nav="0",
        positions=(
            Position(
                symbol="AAPL",
                quantity=Decimal("2"),
                average_cost=Decimal("90"),
                market_value=Decimal("0"),
            ),
        ),
    )

    plan = planning(held=zero_nav, reduction=reduction)[0]

    assert plan.target_weight == Decimal("0")
    assert plan.status is OrderPlanStatus.SKIPPED


def submission_for(
    plan: object,
    *,
    status: FillStatus = FillStatus.PENDING,
    requested: str | None = None,
    symbol: str | None = None,
) -> Fill:
    assert hasattr(plan, "order") and plan.order is not None
    order = plan.order
    quantity = Decimal(requested) if requested is not None else order.quantity
    return Fill(
        status=status,
        order_id=order.order_id,
        account_id=order.account_id,
        symbol=symbol or order.symbol,
        market=order.market,
        side=order.side,
        requested_quantity=quantity,
        filled_quantity=Decimal("0"),
        price=None,
        fees=Decimal("0"),
        session_date=None,
        reason="market blocked" if status is FillStatus.REJECTED else None,
    )


@pytest.mark.parametrize("status", [FillStatus.PENDING, FillStatus.REJECTED])
def test_record_submission_audits_pending_and_immediate_rejection(status: FillStatus) -> None:
    ready = planning(decision(intent()))[0]
    submission = submission_for(ready, status=status)

    recorded = record_submission(ready, submission)

    assert recorded.status is OrderPlanStatus.SUBMITTED
    assert recorded.submission == submission
    assert recorded.effective_quantity == submission.requested_quantity
    assert recorded.raw_quantity == ready.raw_quantity
    assert recorded.submitted_quantity == ready.submitted_quantity
    assert recorded.order == ready.order
    assert recorded.source is ready.source
    assert recorded.target_weight == ready.target_weight


def test_record_submission_preserves_cn_normalized_effective_quantity() -> None:
    ready = planning(
        decision(intent(target="1")),
        snapshot=MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(close="1"),)),
        held=portfolio(cash="150.123", nav="150.123"),
    )[0]
    submission = submission_for(ready, requested="100")

    recorded = record_submission(ready, submission)

    assert ready.submitted_quantity == Decimal("150.123000000000")
    assert recorded.effective_quantity == Decimal("100")


def test_record_submission_rejects_nonready_identity_mismatch_and_pollution() -> None:
    ready = planning(decision(intent()))[0]
    skipped = planning(decision(intent(side=Side.HOLD, target="0")))[0]
    wrong_identity = submission_for(ready, symbol="MSFT")
    valid = submission_for(ready)
    polluted = Fill.model_construct(
        **{name: getattr(valid, name) for name in Fill.model_fields if name != "fees"},
        fees=[],
    )

    with pytest.raises(TypeError, match="READY"):
        record_submission(skipped, valid)
    with pytest.raises(ValidationError, match="submission identity"):
        record_submission(ready, wrong_identity)
    with pytest.raises(ValidationError):
        record_submission(ready, polluted)


def test_order_ids_are_deterministic_canonical_and_sensitive_to_fields() -> None:
    kwargs = {
        "run_id": "run-1",
        "decision_session": SESSION,
        "strategy_id": "strategy-1",
        "market_snapshot": MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(),)),
        "portfolio": portfolio(),
        "risk_decisions": (decision(intent()),),
        "portfolio_reduction": None,
    }
    first = plan_orders(**kwargs)[0]
    repeated = plan_orders(**kwargs)[0]
    changed = plan_orders(**{**kwargs, "run_id": "run-2"})[0]

    assert first.order is not None and repeated.order is not None and changed.order is not None
    assert first.order.order_id == repeated.order.order_id
    assert first.order.order_id != changed.order.order_id
    prefix, digest = first.order.order_id.split(":")
    assert prefix == "order-sha256"
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


def test_planning_is_repeatable_concurrent_and_does_not_mutate_decimal_context() -> None:
    approved = decision(intent(target="1"))
    snapshot = MarketSnapshot(
        as_of=AS_OF,
        market=Market.US,
        bars=(bar(close="1"),),
    )
    held = portfolio(cash="150.1234567890129", nav="150.1234567890129")
    before = repr(getcontext())

    def run() -> str:
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            plan = planning(approved, snapshot=snapshot, held=held)[0]
            assert plan.order is not None
            return plan.order.order_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = tuple(executor.map(lambda _: run(), range(32)))

    assert len(set(ids)) == 1
    assert repr(getcontext()) == before


def test_planner_rejects_entry_mismatches_before_planning() -> None:
    snapshot = MarketSnapshot(as_of=AS_OF, market=Market.US, bars=(bar(),))
    approved = decision(intent())
    kwargs = {
        "run_id": "run-1",
        "decision_session": SESSION,
        "strategy_id": "strategy-1",
        "market_snapshot": snapshot,
        "portfolio": portfolio(),
        "risk_decisions": (approved,),
        "portfolio_reduction": None,
    }

    with pytest.raises(TypeError, match="plain date"):
        plan_orders(**{**kwargs, "decision_session": datetime(2026, 7, 28)})
    with pytest.raises(TypeError, match="exact tuple"):
        plan_orders(**{**kwargs, "risk_decisions": [approved]})
    with pytest.raises(ValueError, match="strategy_id"):
        plan_orders(**{**kwargs, "strategy_id": "other"})
    with pytest.raises(ValueError, match="unique"):
        plan_orders(**{**kwargs, "risk_decisions": (approved, approved)})
    with pytest.raises(ValueError, match="reduction"):
        plan_orders(**{**kwargs, "portfolio_reduction": reduction_target()})


def test_planner_revalidates_polluted_nested_intent_before_planning() -> None:
    clean = intent()
    polluted_intent = StrategyIntent.model_construct(
        **{
            name: getattr(clean, name)
            for name in StrategyIntent.model_fields
            if name != "side"
        },
        side="BUY",
    )
    polluted_decision = RiskDecision.model_construct(
        original_intent=polluted_intent,
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal("0.2"),
        rule_ids=(),
        reasons=(),
        risk_reduction=None,
    )

    with pytest.raises((ValidationError, ValueError)):
        planning(polluted_decision)


def test_planner_uses_rebuilt_nested_intent_for_planning() -> None:
    clean = intent()
    unnormalized_intent = StrategyIntent.model_construct(
        **{
            name: ("aapl" if name == "symbol" else getattr(clean, name))
            for name in StrategyIntent.model_fields
        }
    )
    shallow_decision = RiskDecision.model_construct(
        original_intent=unnormalized_intent,
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal("0.2"),
        rule_ids=(),
        reasons=(),
        risk_reduction=None,
    )

    plan = planning(shallow_decision)[0]

    assert plan.status is OrderPlanStatus.READY
    assert plan.symbol == "AAPL"
    assert plan.order is not None and plan.order.symbol == "AAPL"


def test_planner_revalidates_missing_nested_intent_field_before_planning() -> None:
    clean = intent()
    incomplete_intent = StrategyIntent.model_construct(
        **{
            name: getattr(clean, name)
            for name in StrategyIntent.model_fields
            if name != "symbol"
        }
    )
    polluted_decision = RiskDecision.model_construct(
        original_intent=incomplete_intent,
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal("0.2"),
        rule_ids=(),
        reasons=(),
        risk_reduction=None,
    )

    with pytest.raises((ValidationError, ValueError)):
        planning(polluted_decision)


def test_planner_revalidates_polluted_nested_reduction_before_agreement_check() -> None:
    clean_reduction = reduction_target()
    polluted_reduction = RiskReductionTarget.model_construct(
        current_gross_exposure="0.8",
        target_gross_exposure=clean_reduction.target_gross_exposure,
        review_required=True,
    )
    polluted_decision = RiskDecision.model_construct(
        original_intent=intent(),
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal("0.2"),
        rule_ids=(),
        reasons=(),
        risk_reduction=polluted_reduction,
    )

    with pytest.raises((ValidationError, ValueError)):
        planning(polluted_decision, reduction=clean_reduction)
