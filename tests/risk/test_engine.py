from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, Inexact, getcontext, localcontext

import pytest
from pydantic import ConfigDict, ValidationError

from stock_agent.domain import (
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Position,
    Side,
    StrategyIntent,
)
from stock_agent.risk import RiskContext, RiskEngine, RiskReductionTarget

AS_OF = datetime(2026, 7, 29, 12, tzinfo=UTC)
AAPL = Instrument(
    symbol="AAPL",
    market=Market.US,
    currency=Currency.USD,
    sector="Technology",
)


def portfolio(*, cash: str, nav: str, peak_nav: str = "1000") -> PortfolioSnapshot:
    invested = Decimal(nav) - Decimal(cash)
    positions = (
        Position(
            symbol="AAPL",
            quantity=Decimal("1"),
            average_cost=invested,
            market_value=invested,
        ),
    ) if invested else ()
    return PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal(cash),
        nav=Decimal(nav),
        peak_nav=Decimal(peak_nav),
        positions=positions,
        as_of=AS_OF,
    )


def context(snapshot: PortfolioSnapshot) -> RiskContext:
    return RiskContext(
        portfolio=snapshot,
        instruments=(AAPL,),
        day_start_available_cash=Decimal("1000"),
        new_position_notional_committed_today=Decimal("0"),
    )


def intent(side: Side = Side.HOLD, target: str = "0.4") -> StrategyIntent:
    return StrategyIntent(
        strategy_id="fixture-v1",
        symbol="AAPL",
        market=Market.US,
        side=side,
        target_weight=Decimal(target),
        confidence=80,
        as_of=AS_OF,
        thesis="Fixture thesis",
        invalidation="Fixture invalidation",
    )


def test_assess_portfolio_requires_exact_risk_context() -> None:
    class MutableContext(RiskContext):
        model_config = ConfigDict(frozen=False)

    valid = context(portfolio(cash="320", nav="800"))
    subclass = MutableContext(
        portfolio=valid.portfolio,
        instruments=valid.instruments,
        day_start_available_cash=valid.day_start_available_cash,
        new_position_notional_committed_today=valid.new_position_notional_committed_today,
    )

    for invalid in (object(), subclass):
        with pytest.raises(TypeError, match="context must be exactly RiskContext"):
            RiskEngine().assess_portfolio(invalid)  # type: ignore[arg-type]


def test_assess_portfolio_fully_revalidates_polluted_exact_instances() -> None:
    valid = context(portfolio(cash="320", nav="800"))
    constructed = RiskContext.model_construct(
        portfolio=valid.portfolio,
        instruments=valid.instruments,
        day_start_available_cash=Decimal("1000"),
        new_position_notional_committed_today=Decimal("-1"),
    )
    copied = copy(valid)
    object.__setattr__(copied, "day_start_available_cash", Decimal("-1"))
    nested = copy(valid)
    polluted_portfolio = copy(valid.portfolio)
    object.__setattr__(polluted_portfolio, "cash", Decimal("-1"))
    object.__setattr__(nested, "portfolio", polluted_portfolio)

    for polluted in (constructed, copied, nested):
        with pytest.raises(ValidationError):
            RiskEngine().assess_portfolio(polluted)


def test_assess_portfolio_does_not_mutate_or_replace_valid_context_data() -> None:
    risk_context = context(portfolio(cash="320", nav="800"))
    before = risk_context.model_dump(mode="json")

    RiskEngine().assess_portfolio(risk_context)

    assert risk_context.model_dump(mode="json") == before


@pytest.mark.parametrize(
    ("cash", "nav", "expected_present"),
    [
        ("320.01", "800.01", False),
        ("320", "800", True),
        ("319.99", "799.99", True),
    ],
)
def test_assess_portfolio_uses_inclusive_twenty_percent_boundary(
    cash: str,
    nav: str,
    expected_present: bool,
) -> None:
    assessment = RiskEngine().assess_portfolio(context(portfolio(cash=cash, nav=nav)))

    if not expected_present:
        assert assessment is None
    else:
        assert type(assessment) is RiskReductionTarget
        assert assessment.review_required is True
        with localcontext() as exact_check:
            exact_check.prec = 500
            assert assessment.target_gross_exposure * 2 == assessment.current_gross_exposure
        if nav == "800":
            assert assessment.current_gross_exposure == Decimal("0.6")
            assert assessment.target_gross_exposure == Decimal("0.3")
        else:
            assert assessment.current_gross_exposure > Decimal("0.6")


def test_assess_portfolio_accepts_empty_and_zero_nav_portfolios() -> None:
    empty_at_twenty = context(portfolio(cash="800", nav="800"))
    zero_nav = context(portfolio(cash="0", nav="0", peak_nav="1000"))

    empty_assessment = RiskEngine().assess_portfolio(empty_at_twenty)
    zero_assessment = RiskEngine().assess_portfolio(zero_nav)

    assert empty_assessment == RiskReductionTarget(
        current_gross_exposure=Decimal("0"),
        target_gross_exposure=Decimal("0"),
    )
    assert zero_assessment == empty_assessment


def test_decision_reductions_exactly_agree_with_portfolio_assessment() -> None:
    engine = RiskEngine()
    risk_context = context(portfolio(cash="320", nav="800"))
    assessment = engine.assess_portfolio(risk_context)
    intents = (
        intent(Side.BUY),
        intent(Side.SELL, "0"),
        intent(Side.REDUCE, "0.2"),
        intent(),
    )

    for original in intents:
        assert engine.evaluate(original, risk_context).risk_reduction == assessment
    for original in intents:
        assert engine.evaluate_many((original,), risk_context)[0].risk_reduction == assessment


def test_empty_batch_remains_empty_while_portfolio_is_assessed() -> None:
    engine = RiskEngine()
    risk_context = context(portfolio(cash="320", nav="800"))

    assert engine.evaluate_many((), risk_context) == ()
    assert engine.assess_portfolio(risk_context) is not None


def test_assess_portfolio_is_hostile_decimal_safe_deterministic_and_thread_safe() -> None:
    engine = RiskEngine()
    risk_context = context(portfolio(cash="319.99", nav="799.99"))
    expected = engine.assess_portfolio(risk_context)
    ambient_before = getcontext().copy()

    with localcontext() as hostile:
        hostile.prec = 1
        hostile.rounding = ROUND_UP
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        hostile_before = hostile.copy()
        repeated = tuple(engine.assess_portfolio(risk_context) for _ in range(500))
        assert repr(getcontext()) == repr(hostile_before)

    with ThreadPoolExecutor(max_workers=8) as executor:
        concurrent = tuple(
            executor.map(lambda _: engine.assess_portfolio(risk_context), range(500))
        )

    assert repr(getcontext()) == repr(ambient_before)
    assert all(result == expected for result in repeated)
    assert all(result == expected for result in concurrent)
