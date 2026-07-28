from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal, localcontext

import pytest
from pydantic import ValidationError

import stock_agent.execution.cn_rules as cn_rules_module
from stock_agent.domain import Market, Side
from stock_agent.execution.cn_rules import (
    CnPriceLimitState,
    CnSessionState,
    apply_lot_size,
    can_sell_t1,
    execution_block_reason,
)
from stock_agent.market import TradingCalendar


def test_buy_quantity_is_floored_to_default_board_lot() -> None:
    assert apply_lot_size(side=Side.BUY, quantity=Decimal("250")) == Decimal("200")
    assert apply_lot_size(side=Side.BUY, quantity=Decimal("100")) == Decimal("100")
    assert apply_lot_size(side=Side.BUY, quantity=Decimal("99")) == Decimal("0")


def test_sell_quantity_is_returned_unchanged() -> None:
    quantity = Decimal("99.125")

    assert apply_lot_size(side=Side.SELL, quantity=quantity) is quantity


def test_buy_supports_a_custom_integer_lot_size() -> None:
    assert apply_lot_size(
        side=Side.BUY,
        quantity=Decimal("26"),
        lot_size=Decimal("10"),
    ) == Decimal("20")


@pytest.mark.parametrize("side", [Side.HOLD, Side.REDUCE])
def test_lot_size_rejects_non_execution_sides(side: Side) -> None:
    with pytest.raises(ValueError):
        apply_lot_size(side=side, quantity=Decimal("100"))


@pytest.mark.parametrize("invalid", [100, 100.0, "100"])
def test_quantity_requires_an_actual_decimal(invalid: object) -> None:
    with pytest.raises(TypeError):
        apply_lot_size(side=Side.BUY, quantity=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1.0000000000001"),
        Decimal("1E26"),
    ],
)
def test_quantity_rejects_values_outside_the_decimal_contract(invalid: Decimal) -> None:
    with pytest.raises(ValueError):
        apply_lot_size(side=Side.BUY, quantity=invalid)


@pytest.mark.parametrize("invalid", [100, 100.0, "100"])
def test_lot_size_requires_an_actual_decimal(invalid: object) -> None:
    with pytest.raises(TypeError):
        apply_lot_size(
            side=Side.BUY,
            quantity=Decimal("100"),
            lot_size=invalid,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "invalid",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1.5"),
        Decimal("1E26"),
    ],
)
def test_lot_size_requires_a_valid_positive_integer(invalid: Decimal) -> None:
    with pytest.raises(ValueError):
        apply_lot_size(
            side=Side.BUY,
            quantity=Decimal("100"),
            lot_size=invalid,
        )


def test_lot_calculation_is_independent_of_ambient_decimal_context() -> None:
    quantity = Decimal("9999999999999999999999999.123456789012")
    lot_size = Decimal("100")

    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_CEILING
        for signal in context.traps:
            context.traps[signal] = False
        result = apply_lot_size(side=Side.BUY, quantity=quantity, lot_size=lot_size)

    assert result == Decimal("9999999999999999999999900")


@pytest.fixture
def cn_calendar() -> TradingCalendar:
    return TradingCalendar(
        Market.CN,
        [date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28)],
    )


def test_t1_blocks_sale_on_acquisition_session(cn_calendar: TradingCalendar) -> None:
    acquired = date(2026, 7, 24)

    assert not can_sell_t1(
        acquired_session=acquired,
        sell_session=acquired,
        calendar=cn_calendar,
    )


def test_t1_allows_sale_on_next_explicit_session(cn_calendar: TradingCalendar) -> None:
    assert can_sell_t1(
        acquired_session=date(2026, 7, 24),
        sell_session=date(2026, 7, 27),
        calendar=cn_calendar,
    )


def test_t1_skips_unlisted_weekends_and_holidays(cn_calendar: TradingCalendar) -> None:
    assert can_sell_t1(
        acquired_session=date(2026, 7, 24),
        sell_session=date(2026, 7, 28),
        calendar=cn_calendar,
    )


def test_t1_returns_false_for_an_earlier_session(cn_calendar: TradingCalendar) -> None:
    assert not can_sell_t1(
        acquired_session=date(2026, 7, 27),
        sell_session=date(2026, 7, 24),
        calendar=cn_calendar,
    )


@pytest.mark.parametrize(
    ("acquired", "sell"),
    [
        (date(2026, 7, 25), date(2026, 7, 27)),
        (date(2026, 7, 24), date(2026, 7, 25)),
    ],
)
def test_t1_rejects_dates_not_explicitly_listed_as_sessions(
    cn_calendar: TradingCalendar,
    acquired: date,
    sell: date,
) -> None:
    with pytest.raises(ValueError):
        can_sell_t1(
            acquired_session=acquired,
            sell_session=sell,
            calendar=cn_calendar,
        )


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 24), "2026-07-24"])
def test_t1_rejects_non_plain_dates(
    cn_calendar: TradingCalendar,
    invalid: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        can_sell_t1(
            acquired_session=invalid,  # type: ignore[arg-type]
            sell_session=date(2026, 7, 27),
            calendar=cn_calendar,
        )


def test_t1_rejects_non_cn_calendar() -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 24), date(2026, 7, 27)])

    with pytest.raises(ValueError):
        can_sell_t1(
            acquired_session=date(2026, 7, 24),
            sell_session=date(2026, 7, 27),
            calendar=calendar,
        )


def test_t1_returns_false_at_end_of_calendar_without_leaking_lookup_error(
    cn_calendar: TradingCalendar,
) -> None:
    last_session = date(2026, 7, 28)

    assert not can_sell_t1(
        acquired_session=last_session,
        sell_session=last_session,
        calendar=cn_calendar,
    )


def _session_state(
    *,
    suspended: bool = False,
    price_limit_state: CnPriceLimitState = CnPriceLimitState.NONE,
) -> CnSessionState:
    return CnSessionState(
        symbol="600000",
        session_date=date(2026, 7, 28),
        suspended=suspended,
        price_limit_state=price_limit_state,
    )


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_suspension_blocks_both_execution_directions(side: Side) -> None:
    reason = execution_block_reason(side=side, state=_session_state(suspended=True))

    assert reason is not None
    assert "suspended" in reason.lower()


@pytest.mark.parametrize(
    ("side", "limit_state", "reason_fragment"),
    [
        (Side.BUY, CnPriceLimitState.LIMIT_UP, "limit up"),
        (Side.SELL, CnPriceLimitState.LIMIT_DOWN, "limit down"),
    ],
)
def test_price_limit_blocks_only_the_constrained_direction(
    side: Side,
    limit_state: CnPriceLimitState,
    reason_fragment: str,
) -> None:
    reason = execution_block_reason(
        side=side,
        state=_session_state(price_limit_state=limit_state),
    )

    assert reason is not None
    assert reason_fragment in reason.lower()


@pytest.mark.parametrize(
    ("side", "limit_state"),
    [
        (Side.SELL, CnPriceLimitState.LIMIT_UP),
        (Side.BUY, CnPriceLimitState.LIMIT_DOWN),
        (Side.BUY, CnPriceLimitState.NONE),
        (Side.SELL, CnPriceLimitState.NONE),
    ],
)
def test_price_limit_allows_the_unconstrained_direction(
    side: Side,
    limit_state: CnPriceLimitState,
) -> None:
    assert (
        execution_block_reason(
            side=side,
            state=_session_state(price_limit_state=limit_state),
        )
        is None
    )


def test_suspension_takes_priority_over_price_limit() -> None:
    reason = execution_block_reason(
        side=Side.BUY,
        state=_session_state(
            suspended=True,
            price_limit_state=CnPriceLimitState.LIMIT_UP,
        ),
    )

    assert reason is not None
    assert "suspended" in reason.lower()


@pytest.mark.parametrize("side", [Side.HOLD, Side.REDUCE])
def test_execution_block_reason_rejects_non_execution_sides(side: Side) -> None:
    with pytest.raises(ValueError):
        execution_block_reason(side=side, state=_session_state())


def test_session_state_normalizes_symbol() -> None:
    state = CnSessionState(
        symbol="  sz000001  ",
        session_date=date(2026, 7, 28),
        suspended=False,
        price_limit_state=CnPriceLimitState.NONE,
    )

    assert state.symbol == "SZ000001"


@pytest.mark.parametrize("invalid", ["", "   "])
def test_session_state_rejects_blank_symbol(invalid: str) -> None:
    with pytest.raises(ValidationError):
        CnSessionState(
            symbol=invalid,
            session_date=date(2026, 7, 28),
            suspended=False,
            price_limit_state=CnPriceLimitState.NONE,
        )


@pytest.mark.parametrize("invalid", [0, 1, "false", "true"])
def test_session_state_requires_strict_boolean(invalid: object) -> None:
    with pytest.raises(ValidationError):
        CnSessionState(
            symbol="600000",
            session_date=date(2026, 7, 28),
            suspended=invalid,  # type: ignore[arg-type]
            price_limit_state=CnPriceLimitState.NONE,
        )


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 28), "2026-07-28"])
def test_session_state_requires_plain_date(invalid: object) -> None:
    with pytest.raises(ValidationError):
        CnSessionState(
            symbol="600000",
            session_date=invalid,  # type: ignore[arg-type]
            suspended=False,
            price_limit_state=CnPriceLimitState.NONE,
        )


def test_session_state_is_frozen() -> None:
    state = _session_state()

    with pytest.raises(ValidationError):
        state.suspended = True


def test_session_state_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CnSessionState(
            symbol="600000",
            session_date=date(2026, 7, 28),
            suspended=False,
            price_limit_state=CnPriceLimitState.NONE,
            inferred_limit=True,  # type: ignore[call-arg]
        )


def test_price_limit_state_has_only_explicit_fixture_states() -> None:
    assert list(CnPriceLimitState) == [
        CnPriceLimitState.NONE,
        CnPriceLimitState.LIMIT_UP,
        CnPriceLimitState.LIMIT_DOWN,
    ]


def test_cn_rules_module_declares_its_public_api() -> None:
    assert cn_rules_module.__all__ == [
        "CnPriceLimitState",
        "CnSessionState",
        "apply_lot_size",
        "can_sell_t1",
        "execution_block_reason",
    ]
