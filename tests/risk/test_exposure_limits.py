from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, Inexact, localcontext

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
    return Instrument(
        symbol=symbol,
        market=Market.US,
        currency=Currency.USD,
        sector=sector,
    )


def position(symbol: str, market_value: str) -> Position:
    value = Decimal(market_value)
    return Position(
        symbol=symbol,
        quantity=Decimal("1"),
        average_cost=value or Decimal("1"),
        market_value=value,
    )


def portfolio(positions: tuple[Position, ...] = (), nav: str = "1000") -> PortfolioSnapshot:
    nav_value = Decimal(nav)
    invested = sum((item.market_value for item in positions), start=Decimal(0))
    return PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=nav_value - invested,
        nav=nav_value,
        peak_nav=nav_value,
        positions=positions,
        as_of=AS_OF,
    )


def context(
    *,
    snapshot: PortfolioSnapshot | None = None,
    instruments: tuple[Instrument, ...] | None = None,
) -> RiskContext:
    return RiskContext(
        portfolio=snapshot or portfolio(),
        instruments=(instrument("AAPL"),) if instruments is None else instruments,
        day_start_available_cash=Decimal("1000"),
        new_position_notional_committed_today=Decimal("0"),
    )


def buy_intent(target: str = "0.40", symbol: str = "AAPL") -> StrategyIntent:
    return StrategyIntent(
        strategy_id="momentum-v1",
        symbol=symbol,
        market=Market.US,
        side=Side.BUY,
        target_weight=Decimal(target),
        confidence=80,
        as_of=AS_OF,
        thesis="Earnings momentum",
        invalidation="Guidance cut",
    )


def sell_intent(symbol: str = "AAPL") -> StrategyIntent:
    return StrategyIntent(
        **buy_intent(target="0", symbol=symbol).model_dump(exclude={"side"}),
        side=Side.SELL,
    )


def test_buy_target_is_clamped_to_fifteen_percent() -> None:
    original = buy_intent()

    decision = RiskEngine().evaluate(original, context())

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ("SINGLE_STOCK_MAX_15",)
    assert len(decision.reasons) == 1
    assert decision.original_intent is original
    assert decision.original_intent.side is Side.BUY


def test_other_same_sector_holdings_leave_only_ten_percent_room() -> None:
    snapshot = portfolio((position("MSFT", "200"),))
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("MSFT")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.12"), risk_context)

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == ("SECTOR_EXPOSURE_MAX_30",)
    assert len(decision.reasons) == 1


def test_missing_buy_metadata_rejects_with_sorted_symbols() -> None:
    snapshot = portfolio((position("ZZZ", "100"), position("BBB", "100")))
    risk_context = context(snapshot=snapshot, instruments=(instrument("ZZZ"),))

    decision = RiskEngine().evaluate(buy_intent(target="0.10"), risk_context)

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == ("MISSING_INSTRUMENT_METADATA",)
    assert decision.reasons == ("missing instrument metadata for symbols: AAPL, BBB",)


def test_existing_target_symbol_is_excluded_from_sector_exposure() -> None:
    snapshot = portfolio((position("AAPL", "250"), position("MSFT", "50")))
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("MSFT")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.15"), risk_context)

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ()


def test_sector_comparison_uses_unicode_casefold_and_strip() -> None:
    snapshot = portfolio((position("SAP", "250"),))
    risk_context = context(
        snapshot=snapshot,
        instruments=(
            instrument("AAPL", " STRASSE "),
            instrument("SAP", "Straße"),
        ),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.10"), risk_context)

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.05")
    assert decision.rule_ids == ("SECTOR_EXPOSURE_MAX_30",)


def test_unrelated_sector_does_not_consume_sector_room() -> None:
    snapshot = portfolio((position("XOM", "900"),), nav="1000")
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("XOM", "Energy")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.15"), risk_context)

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.rule_ids == ()


def test_same_sector_at_thirty_percent_rejects_buy() -> None:
    snapshot = portfolio((position("MSFT", "300"),))
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("MSFT")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.10"), risk_context)

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == ("SECTOR_EXPOSURE_MAX_30",)


def test_sell_does_not_require_instrument_metadata() -> None:
    decision = RiskEngine().evaluate(sell_intent(), context(instruments=()))

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal(0)
    assert decision.rule_ids == ()


def ten_position_context() -> RiskContext:
    positions = tuple(position(f"P{index}", "10") for index in range(10))
    instruments = (
        *(instrument(f"P{index}", "Sector") for index in range(10)),
        instrument("NEW", "Different"),
    )
    return context(snapshot=portfolio(positions), instruments=instruments)


def test_opening_eleventh_holding_is_rejected() -> None:
    decision = RiskEngine().evaluate(
        buy_intent(target="0.10", symbol="NEW"),
        ten_position_context(),
    )

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.rule_ids == ("HOLDING_COUNT_MAX_10",)


def test_increasing_one_of_ten_existing_holdings_proceeds() -> None:
    decision = RiskEngine().evaluate(
        buy_intent(target="0.10", symbol="P0"),
        ten_position_context(),
    )

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == ()


def test_raw_zero_buy_target_is_rejected_without_becoming_hold() -> None:
    original = buy_intent(target="0")

    decision = RiskEngine().evaluate(original, context())

    assert decision.status is RiskDecisionStatus.REJECTED
    assert decision.approved_target_weight is None
    assert decision.original_intent is original
    assert decision.original_intent.side is Side.BUY
    assert decision.rule_ids == ("ZERO_TARGET_BUY_BLOCK",)


def test_stock_and_sector_limits_compose_in_order() -> None:
    snapshot = portfolio((position("MSFT", "200"),))
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("MSFT")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.40"), risk_context)

    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.10")
    assert decision.rule_ids == (
        "SINGLE_STOCK_MAX_15",
        "SECTOR_EXPOSURE_MAX_30",
    )
    assert decision.reasons == (
        "buy target exceeds single-stock maximum of fifteen percent",
        "buy target exceeds sector exposure maximum of thirty percent",
    )


def test_equal_stock_and_sector_limits_are_not_binding() -> None:
    snapshot = portfolio((position("MSFT", "150"),))
    risk_context = context(
        snapshot=snapshot,
        instruments=(instrument("AAPL"), instrument("MSFT")),
    )

    decision = RiskEngine().evaluate(buy_intent(target="0.15"), risk_context)

    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ()
    assert decision.reasons == ()


def high_precision_sector_context() -> RiskContext:
    with localcontext() as construction:
        construction.prec = 200
        positions = (
            position("AAPL", "1"),
            position("SAME1", "100.0000000000000000000000000000000000000001"),
            position("SAME2", "100.0000000000000000000000000000000000000001"),
        )
        snapshot = portfolio(positions)
    return context(
        snapshot=snapshot,
        instruments=(
            instrument("AAPL"),
            instrument("SAME1"),
            instrument("SAME2"),
        ),
    )


def test_sector_arithmetic_is_hostile_context_safe_deterministic_and_pure() -> None:
    original = buy_intent(target="0.12")
    risk_context = high_precision_sector_context()
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
    assert first["approved_target_weight"] == "0.0999999999999999999999999999999999999999998"
    assert original.model_dump(mode="json") == intent_before
    assert risk_context.model_dump(mode="json") == context_before
