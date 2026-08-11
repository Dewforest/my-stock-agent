from datetime import date
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Protocol

import pytest

import stock_agent.execution as execution_module
import stock_agent.execution.rules as rules_module
from stock_agent.account import AcquisitionLot
from stock_agent.domain import Market, Side
from stock_agent.execution import ChinaAShareRules, MarketRuleSet, USCashEquityRules
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.market import TradingCalendar


@pytest.fixture
def cn_calendar() -> TradingCalendar:
    return TradingCalendar(
        Market.CN,
        [date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29)],
    )


def test_market_rule_set_is_runtime_checkable_protocol(
    cn_calendar: TradingCalendar,
) -> None:
    assert issubclass(MarketRuleSet, Protocol)
    assert getattr(MarketRuleSet, "_is_runtime_protocol", False)
    assert isinstance(ChinaAShareRules(cn_calendar), MarketRuleSet)
    assert isinstance(USCashEquityRules(), MarketRuleSet)


def test_rule_sets_expose_read_only_market_and_session_state_requirement(
    cn_calendar: TradingCalendar,
) -> None:
    china = ChinaAShareRules(cn_calendar)
    us = USCashEquityRules()

    assert china.market is Market.CN
    assert china.requires_session_state is True
    assert us.market is Market.US
    assert us.requires_session_state is False
    with pytest.raises(AttributeError):
        china.market = Market.US  # type: ignore[misc]
    with pytest.raises(AttributeError):
        us.requires_session_state = True  # type: ignore[misc]


def test_china_constructor_requires_cn_trading_calendar() -> None:
    cn_calendar = TradingCalendar(Market.CN, [date(2026, 7, 28)])
    us_calendar = TradingCalendar(Market.US, [date(2026, 7, 28)])

    assert ChinaAShareRules(calendar=cn_calendar).market is Market.CN
    with pytest.raises(ValueError):
        ChinaAShareRules(us_calendar)
    with pytest.raises(TypeError):
        ChinaAShareRules(object())  # type: ignore[arg-type]


def test_china_normalizes_buy_lots_but_not_sell_quantity(
    cn_calendar: TradingCalendar,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    sell_quantity = Decimal("99.125")

    assert rules.normalize_quantity(side=Side.BUY, quantity=Decimal("250")) == Decimal("200")
    assert rules.normalize_quantity(side=Side.BUY, quantity=Decimal("99")) == Decimal("0")
    assert rules.normalize_quantity(side=Side.SELL, quantity=sell_quantity) is sell_quantity


@pytest.mark.parametrize("side", [Side.HOLD, Side.REDUCE])
def test_rule_sets_reject_non_execution_sides(
    cn_calendar: TradingCalendar,
    side: Side,
) -> None:
    for rules in (ChinaAShareRules(cn_calendar), USCashEquityRules()):
        with pytest.raises(ValueError):
            rules.normalize_quantity(side=side, quantity=Decimal("100"))


def test_us_never_rounds_execution_quantities() -> None:
    rules = USCashEquityRules()
    for side in (Side.BUY, Side.SELL):
        quantity = Decimal("99.125")
        assert rules.normalize_quantity(side=side, quantity=quantity) is quantity


@pytest.mark.parametrize("invalid", [1, 1.0, "1"])
def test_normalize_quantity_requires_decimal(
    cn_calendar: TradingCalendar,
    invalid: object,
) -> None:
    for rules in (ChinaAShareRules(cn_calendar), USCashEquityRules()):
        with pytest.raises(TypeError):
            rules.normalize_quantity(side=Side.BUY, quantity=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("1E26")],
)
def test_normalize_quantity_rejects_unsupported_decimals(
    cn_calendar: TradingCalendar,
    invalid: Decimal,
) -> None:
    for rules in (ChinaAShareRules(cn_calendar), USCashEquityRules()):
        with pytest.raises(ValueError):
            rules.normalize_quantity(side=Side.BUY, quantity=invalid)


def _lot(symbol: str, acquired_session: date, quantity: str) -> AcquisitionLot:
    return AcquisitionLot(
        symbol=symbol,
        acquired_session=acquired_session,
        quantity=Decimal(quantity),
        cost_basis=Decimal("10"),
    )


def test_china_sellable_quantity_uses_acquisition_sessions_not_aggregate_position(
    cn_calendar: TradingCalendar,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    d1 = date(2026, 7, 27)
    d2 = date(2026, 7, 28)
    partially_settled = (_lot("600000", d1, "100"), _lot("600000", d2, "100"))
    fully_settled = (_lot("600000", d1, "100"), _lot("600000", d1, "100"))

    assert sum(lot.quantity for lot in partially_settled) == Decimal("200")
    assert sum(lot.quantity for lot in fully_settled) == Decimal("200")
    assert rules.sellable_quantity(
        symbol="600000", session_date=d2, acquisition_lots=partially_settled
    ) == Decimal("100")
    assert rules.sellable_quantity(
        symbol="600000", session_date=d2, acquisition_lots=fully_settled
    ) == Decimal("200")


def test_sellable_quantity_canonicalizes_symbol_and_ignores_other_symbols(
    cn_calendar: TradingCalendar,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    lots = (
        _lot("sz000001", date(2026, 7, 27), "125.5"),
        _lot("600000", date(2026, 7, 27), "999"),
    )

    assert rules.sellable_quantity(
        symbol="  SZ000001 ", session_date=date(2026, 7, 28), acquisition_lots=lots
    ) == Decimal("125.5")


def test_us_all_matching_lots_are_sellable_including_same_day() -> None:
    session = date(2026, 7, 28)
    lots = (
        _lot("aapl", date(2026, 7, 27), "0.5"),
        _lot("AAPL", session, "1.25"),
        _lot("MSFT", session, "100"),
    )

    assert USCashEquityRules().sellable_quantity(
        symbol=" aapl ", session_date=session, acquisition_lots=lots
    ) == Decimal("1.75")


def test_sellable_quantity_does_not_modify_acquisition_lots(
    cn_calendar: TradingCalendar,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    lots = (_lot("600000", date(2026, 7, 27), "100"),)
    before = tuple(lot.model_dump() for lot in lots)

    rules.sellable_quantity(
        symbol="600000", session_date=date(2026, 7, 28), acquisition_lots=lots
    )

    assert tuple(lot.model_dump() for lot in lots) == before


def test_sellable_sum_is_independent_of_ambient_decimal_context() -> None:
    session = date(2026, 7, 28)
    lots = (
        _lot("AAPL", session, "9999999999999999999999999.123456789012"),
        _lot("AAPL", session, "0.876543210988"),
    )

    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_CEILING
        for signal in context.traps:
            context.traps[signal] = False
        result = USCashEquityRules().sellable_quantity(
            symbol="AAPL", session_date=session, acquisition_lots=lots
        )

    assert result == Decimal("10000000000000000000000000.000000000000")


@pytest.mark.parametrize("invalid", ["", "   ", 600000])
def test_sellable_quantity_rejects_invalid_symbols(
    cn_calendar: TradingCalendar,
    invalid: object,
) -> None:
    for rules in (ChinaAShareRules(cn_calendar), USCashEquityRules()):
        with pytest.raises((TypeError, ValueError)):
            rules.sellable_quantity(
                symbol=invalid,  # type: ignore[arg-type]
                session_date=date(2026, 7, 28),
                acquisition_lots=(),
            )


def test_china_sellable_quantity_requires_explicit_calendar_session(
    cn_calendar: TradingCalendar,
) -> None:
    with pytest.raises(ValueError):
        ChinaAShareRules(cn_calendar).sellable_quantity(
            symbol="600000", session_date=date(2026, 7, 30), acquisition_lots=()
        )


@pytest.mark.parametrize("invalid", ["2026-07-28", object()])
def test_sellable_quantity_requires_plain_date(
    cn_calendar: TradingCalendar,
    invalid: object,
) -> None:
    for rules in (ChinaAShareRules(cn_calendar), USCashEquityRules()):
        with pytest.raises((TypeError, ValueError)):
            rules.sellable_quantity(
                symbol="600000",
                session_date=invalid,  # type: ignore[arg-type]
                acquisition_lots=(),
            )


def test_sellable_quantity_requires_tuple_of_acquisition_lots(
    cn_calendar: TradingCalendar,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    with pytest.raises(TypeError):
        rules.sellable_quantity(
            symbol="600000",
            session_date=date(2026, 7, 28),
            acquisition_lots=[],  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        rules.sellable_quantity(
            symbol="600000",
            session_date=date(2026, 7, 28),
            acquisition_lots=(object(),),  # type: ignore[arg-type]
        )


def _state(
    *,
    symbol: str = "600000",
    session_date: date = date(2026, 7, 28),
    suspended: bool = False,
    limit: CnPriceLimitState = CnPriceLimitState.NONE,
) -> CnSessionState:
    return CnSessionState(
        symbol=symbol,
        session_date=session_date,
        suspended=suspended,
        price_limit_state=limit,
    )


def _china_reason(
    rules: ChinaAShareRules,
    *,
    side: Side,
    quantity: Decimal = Decimal("100"),
    state: CnSessionState | None = None,
    lots: tuple[AcquisitionLot, ...] = (),
) -> str | None:
    return rules.execution_block_reason(
        side=side,
        symbol="600000",
        session_date=date(2026, 7, 28),
        quantity=quantity,
        state=_state() if state is None else state,
        acquisition_lots=lots,
    )


@pytest.mark.parametrize(
    ("side", "state", "fragment"),
    [
        (Side.BUY, _state(suspended=True), "suspended"),
        (Side.SELL, _state(suspended=True), "suspended"),
        (Side.BUY, _state(limit=CnPriceLimitState.LIMIT_UP), "limit up"),
        (Side.SELL, _state(limit=CnPriceLimitState.LIMIT_DOWN), "limit down"),
    ],
)
def test_china_explicit_state_matrix_blocks_execution_before_settlement_check(
    cn_calendar: TradingCalendar,
    side: Side,
    state: CnSessionState,
    fragment: str,
) -> None:
    reason = _china_reason(ChinaAShareRules(cn_calendar), side=side, state=state)

    assert reason is not None
    assert fragment in reason.lower()


@pytest.mark.parametrize(
    ("side", "limit"),
    [
        (Side.BUY, CnPriceLimitState.NONE),
        (Side.BUY, CnPriceLimitState.LIMIT_DOWN),
        (Side.SELL, CnPriceLimitState.LIMIT_UP),
    ],
)
def test_china_state_matrix_allows_unconstrained_direction_when_settled(
    cn_calendar: TradingCalendar,
    side: Side,
    limit: CnPriceLimitState,
) -> None:
    lots = (_lot("600000", date(2026, 7, 27), "100"),)

    assert (
        _china_reason(
            ChinaAShareRules(cn_calendar), side=side, state=_state(limit=limit), lots=lots
        )
        is None
    )


def test_china_sell_blocks_quantity_above_settled_lots(
    cn_calendar: TradingCalendar,
) -> None:
    lots = (
        _lot("600000", date(2026, 7, 27), "100"),
        _lot("600000", date(2026, 7, 28), "100"),
    )

    reason = _china_reason(
        ChinaAShareRules(cn_calendar), side=Side.SELL, quantity=Decimal("101"), lots=lots
    )

    assert reason is not None
    assert "t+1" in reason.lower() or "settled" in reason.lower()


def test_china_sell_allows_quantity_equal_to_settled_lots(
    cn_calendar: TradingCalendar,
) -> None:
    lots = (_lot("600000", date(2026, 7, 27), "100"),)

    assert (
        _china_reason(
            ChinaAShareRules(cn_calendar), side=Side.SELL, quantity=Decimal("100"), lots=lots
        )
        is None
    )


def test_china_buy_does_not_require_acquisition_lots(
    cn_calendar: TradingCalendar,
) -> None:
    assert _china_reason(ChinaAShareRules(cn_calendar), side=Side.BUY, lots=()) is None


@pytest.mark.parametrize(
    "state",
    [
        None,
        _state(symbol="000001"),
        _state(session_date=date(2026, 7, 29)),
    ],
)
def test_china_requires_matching_session_state(
    cn_calendar: TradingCalendar,
    state: CnSessionState | None,
) -> None:
    rules = ChinaAShareRules(cn_calendar)
    with pytest.raises(ValueError):
        rules.execution_block_reason(
            side=Side.BUY,
            symbol="600000",
            session_date=date(2026, 7, 28),
            quantity=Decimal("100"),
            state=state,
            acquisition_lots=(),
        )


def test_us_buy_execution_has_no_market_block_without_holdings() -> None:
    assert (
        USCashEquityRules().execution_block_reason(
            side=Side.BUY,
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            quantity=Decimal("0.125"),
            state=None,
            acquisition_lots=(),
        )
        is None
    )


def test_us_sell_execution_blocks_quantity_above_held_lots() -> None:
    rules = USCashEquityRules()
    session = date(2026, 7, 28)

    reason = rules.execution_block_reason(
        side=Side.SELL,
        symbol="AAPL",
        session_date=session,
        quantity=Decimal("1.5"),
        state=None,
        acquisition_lots=(_lot("AAPL", session, "1.25"),),
    )

    assert reason is not None
    assert any(fragment in reason.lower() for fragment in ("held", "available", "short"))


def test_us_sell_execution_allows_quantity_equal_to_held_lots() -> None:
    session = date(2026, 7, 28)

    assert (
        USCashEquityRules().execution_block_reason(
            side=Side.SELL,
            symbol="AAPL",
            session_date=session,
            quantity=Decimal("1.25"),
            state=None,
            acquisition_lots=(_lot("AAPL", session, "1.25"),),
        )
        is None
    )


def test_us_rejects_china_session_state() -> None:
    with pytest.raises(ValueError):
        USCashEquityRules().execution_block_reason(
            side=Side.BUY,
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            quantity=Decimal("1"),
            state=_state(),
            acquisition_lots=(),
        )


@pytest.mark.parametrize("side", [Side.HOLD, Side.REDUCE])
def test_execution_block_reason_rejects_non_execution_sides(
    cn_calendar: TradingCalendar,
    side: Side,
) -> None:
    with pytest.raises(ValueError):
        _china_reason(ChinaAShareRules(cn_calendar), side=side)
    with pytest.raises(ValueError):
        USCashEquityRules().execution_block_reason(
            side=side,
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            quantity=Decimal("1"),
            state=None,
            acquisition_lots=(),
        )


@pytest.mark.parametrize("quantity", [1, Decimal("0"), Decimal("NaN"), Decimal("1E26")])
def test_execution_block_reason_strictly_validates_quantity(
    cn_calendar: TradingCalendar,
    quantity: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ChinaAShareRules(cn_calendar).execution_block_reason(
            side=Side.BUY,
            symbol="600000",
            session_date=date(2026, 7, 28),
            quantity=quantity,  # type: ignore[arg-type]
            state=_state(),
            acquisition_lots=(),
        )
    with pytest.raises((TypeError, ValueError)):
        USCashEquityRules().execution_block_reason(
            side=Side.BUY,
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            quantity=quantity,  # type: ignore[arg-type]
            state=None,
            acquisition_lots=(),
        )


def test_market_rules_module_declares_its_public_api() -> None:
    assert rules_module.__all__ == ["ChinaAShareRules", "MarketRuleSet", "USCashEquityRules"]


def test_execution_package_precisely_extends_its_public_api() -> None:
    assert execution_module.__all__ == [
        "ChinaAShareRules",
        "ExecutionSimulator",
        "Fill",
        "FillStatus",
        "MarketRuleSet",
        "OrderIntent",
        "USCashEquityRules",
    ]
