from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext

import pytest
from pydantic import ValidationError

import stock_agent.execution as execution
from stock_agent.domain import Bar, Market, Side
from stock_agent.execution import ExecutionSimulator, Fill, FillStatus, OrderIntent
from stock_agent.market import TradingCalendar

US_SESSIONS = (date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28))


def make_calendar(
    market: Market = Market.US,
    sessions: tuple[date, ...] = US_SESSIONS,
) -> TradingCalendar:
    return TradingCalendar(market, sessions)


def make_intent(**overrides: object) -> OrderIntent:
    values = {
        "order_id": "order-1",
        "account_id": "account-1",
        "symbol": "AAPL",
        "market": Market.US,
        "side": Side.BUY,
        "quantity": Decimal("10"),
    }
    values.update(overrides)
    return OrderIntent(**values)


def make_bar(**overrides: object) -> Bar:
    values = {
        "symbol": "AAPL",
        "market": Market.US,
        "session_date": date(2026, 7, 27),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": Decimal("1000000"),
        "available_at": datetime(2026, 7, 27, 21, tzinfo=UTC),
    }
    values.update(overrides)
    return Bar(**values)


def test_buy_fills_at_next_session_open_not_decision_session() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    intent = make_intent()

    pending = simulator.submit(intent, date(2026, 7, 24))

    assert pending.status is FillStatus.PENDING
    assert pending.filled_quantity == Decimal("0")
    assert simulator.pending_order_ids == ("order-1",)
    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 24),
        bars=[],
    ) == ()

    fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )

    assert len(fills) == 1
    fill = fills[0]
    assert fill.status is FillStatus.FILLED
    assert fill.price == Decimal("100")
    assert fill.price != Decimal("105")
    assert fill.filled_quantity == Decimal("10")
    assert fill.session_date == date(2026, 7, 27)
    assert fill.reason is None
    assert simulator.pending_order_ids == ()


def test_default_transaction_costs_are_exact_for_cn_and_us() -> None:
    cn_sessions = (date(2026, 7, 24), date(2026, 7, 27))
    simulator = ExecutionSimulator(
        {
            Market.CN: make_calendar(Market.CN, cn_sessions),
            Market.US: make_calendar(),
        }
    )
    simulator.submit(
        make_intent(order_id="cn", symbol="600519", market=Market.CN),
        date(2026, 7, 24),
    )
    simulator.submit(make_intent(order_id="us"), date(2026, 7, 24))

    cn_fill = simulator.process_session(
        market=Market.CN,
        session_date=date(2026, 7, 27),
        bars=[
            make_bar(
                symbol="600519",
                market=Market.CN,
                open=Decimal("100"),
                session_date=date(2026, 7, 27),
            )
        ],
    )[0]
    us_fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )[0]

    assert cn_fill.fees == Decimal("1.2")
    assert us_fill.fees == Decimal("0.5")
    assert isinstance(cn_fill.fees, Decimal)
    assert isinstance(us_fill.fees, Decimal)


def test_sell_fills_with_overridden_decimal_transaction_cost() -> None:
    simulator = ExecutionSimulator(
        {Market.US: make_calendar()},
        transaction_cost_bps={Market.US: Decimal("7.5")},
    )
    simulator.submit(make_intent(side=Side.SELL, quantity=Decimal("3")), date(2026, 7, 24))

    fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar(open=Decimal("101"))],
    )[0]

    assert fill.side is Side.SELL
    assert fill.filled_quantity == Decimal("3")
    assert fill.price == Decimal("101")
    assert fill.fees == Decimal("3") * Decimal("101") * Decimal("7.5") / Decimal("10000")


def test_fees_are_exact_and_independent_of_ambient_decimal_precision() -> None:
    quantity = Decimal("99999999999999999999999999.999999999999")
    open_price = Decimal("99999999999999999999999999.999999999998")
    bps = Decimal("99999999999999999999999999.999999999997")
    fees = []

    for precision in (10, 28, 50):
        with localcontext() as context:
            context.prec = precision
            simulator = ExecutionSimulator(
                {Market.US: make_calendar()},
                transaction_cost_bps={Market.US: bps},
            )
            simulator.submit(make_intent(quantity=quantity), date(2026, 7, 24))
            fill = simulator.process_session(
                market=Market.US,
                session_date=date(2026, 7, 27),
                bars=[
                    make_bar(
                        open=open_price,
                        high=open_price,
                        low=open_price,
                        close=open_price,
                    )
                ],
            )[0]
            fees.append(fill.fees)

    with localcontext() as context:
        context.prec = 128
        expected = quantity * open_price * bps / Decimal("10000")
    assert fees == [expected, expected, expected]


def test_decimal_calculation_exception_becomes_rejected_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))

    def fail_calculation(*_values: Decimal) -> Decimal:
        raise InvalidOperation

    monkeypatch.setattr(simulator, "_calculate_fees", fail_calculation)
    fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )[0]

    assert_rejected(fill, "calculation")
    assert simulator.pending_order_ids == ()


@pytest.mark.parametrize(
    "quantity",
    [Decimal("1.0000000000001"), Decimal("1E26")],
)
def test_unsupported_quantity_is_rejected_without_pending_pollution(
    quantity: Decimal,
) -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    rejected = simulator.submit(make_intent(quantity=quantity), date(2026, 7, 24))

    assert_rejected(rejected, "unsupported numeric range/precision")
    assert simulator.pending_order_ids == ()


@pytest.mark.parametrize(
    "bps",
    [Decimal("1.0000000000001"), Decimal("1E26")],
)
def test_unsupported_transaction_cost_is_rejected_by_constructor(bps: Decimal) -> None:
    with pytest.raises(ValueError, match="unsupported numeric range/precision"):
        ExecutionSimulator(
            {Market.US: make_calendar()},
            transaction_cost_bps={Market.US: bps},
        )


@pytest.mark.parametrize(
    "open_price",
    [Decimal("100.0000000000001"), Decimal("1E26")],
)
def test_unsupported_bar_open_rejects_eligible_order_and_removes_it(
    open_price: Decimal,
) -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))

    fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[
            make_bar(
                open=open_price,
                high=max(open_price, Decimal("110")),
                close=max(open_price, Decimal("105")),
            )
        ],
    )[0]

    assert_rejected(fill, "unsupported numeric range/precision")
    assert simulator.pending_order_ids == ()


def test_missing_eligible_bar_stays_pending_and_fills_on_later_session() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))

    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[],
    ) == ()
    assert simulator.pending_order_ids == ("order-1",)

    fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 28),
        bars=[
            make_bar(
                session_date=date(2026, 7, 28),
                available_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
            )
        ],
    )

    assert [fill.order_id for fill in fills] == ["order-1"]
    assert fills[0].session_date == date(2026, 7, 28)
    assert simulator.pending_order_ids == ()


def test_process_rejects_market_time_travel_without_filling_pending_order() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))
    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 28),
        bars=[],
    ) == ()

    with pytest.raises(ValueError, match=r"timeline|backward|processed"):
        simulator.process_session(
            market=Market.US,
            session_date=date(2026, 7, 27),
            bars=[make_bar()],
        )

    assert simulator.pending_order_ids == ("order-1",)


def test_backdated_submit_is_rejected_and_reserves_order_id() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 28),
        bars=[],
    )

    rejected = simulator.submit(make_intent(), date(2026, 7, 24))
    duplicate = simulator.submit(make_intent(symbol="MSFT"), date(2026, 7, 28))

    assert_rejected(rejected, "timeline")
    assert_rejected(duplicate, "duplicate")
    assert simulator.pending_order_ids == ()


def test_submit_on_processed_session_is_allowed_for_close_decision() -> None:
    sessions = (*US_SESSIONS, date(2026, 7, 29))
    simulator = ExecutionSimulator({Market.US: make_calendar(sessions=sessions)})
    simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[],
    )

    pending = simulator.submit(make_intent(), date(2026, 7, 27))
    assert pending.status is FillStatus.PENDING
    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    ) == ()
    fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 28),
        bars=[
            make_bar(
                session_date=date(2026, 7, 28),
                available_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
            )
        ],
    )[0]
    assert fill.session_date == date(2026, 7, 28)


def test_reprocessing_same_session_can_fill_bar_that_arrived_late() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))

    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[],
    ) == ()
    fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )

    assert [fill.order_id for fill in fills] == ["order-1"]
    assert simulator.pending_order_ids == ()


def test_processed_watermarks_are_independent_per_market() -> None:
    cn_sessions = (date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28))
    simulator = ExecutionSimulator(
        {
            Market.US: make_calendar(),
            Market.CN: make_calendar(Market.CN, cn_sessions),
        }
    )
    simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 28),
        bars=[],
    )

    cn_pending = simulator.submit(
        make_intent(order_id="cn", symbol="600519", market=Market.CN),
        date(2026, 7, 24),
    )
    assert cn_pending.status is FillStatus.PENDING
    assert simulator.process_session(
        market=Market.CN,
        session_date=date(2026, 7, 27),
        bars=[],
    ) == ()


def test_processing_is_market_isolated_ordered_and_idempotent() -> None:
    cn_calendar = make_calendar(
        Market.CN,
        (date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28)),
    )
    simulator = ExecutionSimulator(
        {Market.US: make_calendar(), Market.CN: cn_calendar}
    )
    simulator.submit(make_intent(order_id="us-1", symbol="AAPL"), date(2026, 7, 24))
    simulator.submit(
        make_intent(order_id="cn-1", symbol="600519", market=Market.CN),
        date(2026, 7, 24),
    )
    simulator.submit(make_intent(order_id="us-2", symbol="MSFT"), date(2026, 7, 24))

    us_fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar(), make_bar(symbol="MSFT")],
    )

    assert [fill.order_id for fill in us_fills] == ["us-1", "us-2"]
    assert simulator.pending_order_ids == ("cn-1",)
    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar(), make_bar(symbol="MSFT")],
    ) == ()


def assert_rejected(fill: Fill, reason_fragment: str) -> None:
    assert fill.status is FillStatus.REJECTED
    assert fill.filled_quantity == Decimal("0")
    assert fill.price is None
    assert fill.fees == Decimal("0")
    assert fill.session_date is None
    assert fill.reason is not None
    assert reason_fragment.lower() in fill.reason.lower()


@pytest.mark.parametrize("side", [Side.HOLD, Side.REDUCE])
def test_non_executable_sides_are_rejected_without_pending_pollution(side: Side) -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    rejected = simulator.submit(make_intent(side=side), date(2026, 7, 24))

    assert_rejected(rejected, side.value)
    assert simulator.pending_order_ids == ()


def test_non_session_decision_date_is_rejected() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    rejected = simulator.submit(make_intent(), date(2026, 7, 25))

    assert_rejected(rejected, "session")
    assert simulator.pending_order_ids == ()


def test_no_future_session_is_rejected() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    rejected = simulator.submit(make_intent(), date(2026, 7, 28))

    assert_rejected(rejected, "future")
    assert simulator.pending_order_ids == ()


def test_missing_calendar_is_rejected() -> None:
    simulator = ExecutionSimulator({})

    rejected = simulator.submit(make_intent(), date(2026, 7, 24))

    assert_rejected(rejected, "calendar")
    assert simulator.pending_order_ids == ()


def test_duplicate_order_id_is_rejected_without_replacing_original() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    original = simulator.submit(make_intent(symbol="AAPL"), date(2026, 7, 24))

    duplicate = simulator.submit(make_intent(symbol="MSFT"), date(2026, 7, 24))

    assert original.status is FillStatus.PENDING
    assert_rejected(duplicate, "duplicate")
    assert duplicate.symbol == "MSFT"
    assert simulator.pending_order_ids == ("order-1",)


def test_rejected_and_filled_order_ids_remain_reserved() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(order_id="rejected", side=Side.HOLD), date(2026, 7, 24))
    simulator.submit(make_intent(order_id="filled"), date(2026, 7, 24))
    simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )

    rejected_duplicate = simulator.submit(
        make_intent(order_id="rejected"), date(2026, 7, 24)
    )
    filled_duplicate = simulator.submit(make_intent(order_id="filled"), date(2026, 7, 24))

    assert_rejected(rejected_duplicate, "duplicate")
    assert_rejected(filled_duplicate, "duplicate")
    assert simulator.pending_order_ids == ()


@pytest.mark.parametrize(
    "bars",
    [
        [make_bar(), make_bar(market=Market.CN, symbol="600519")],
        [
            make_bar(),
            make_bar(
                session_date=date(2026, 7, 28),
                symbol="MSFT",
                available_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
            ),
        ],
        [make_bar(symbol="aapl"), make_bar(symbol=" AAPL ")],
    ],
)
def test_invalid_bars_are_rejected_atomically(bars: list[Bar]) -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(order_id="first", symbol="AAPL"), date(2026, 7, 24))
    simulator.submit(make_intent(order_id="second", symbol="MSFT"), date(2026, 7, 24))

    with pytest.raises(ValueError):
        simulator.process_session(
            market=Market.US,
            session_date=date(2026, 7, 27),
            bars=bars,
        )

    assert simulator.pending_order_ids == ("first", "second")
    fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar(), make_bar(symbol="MSFT")],
    )
    assert [fill.order_id for fill in fills] == ["first", "second"]


def test_invalid_bars_do_not_advance_processed_watermark() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    with pytest.raises(ValueError, match="session_date"):
        simulator.process_session(
            market=Market.US,
            session_date=date(2026, 7, 28),
            bars=[make_bar()],
        )

    assert simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[],
    ) == ()


def test_bars_iterable_is_materialized_once() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})
    simulator.submit(make_intent(), date(2026, 7, 24))
    iterations = 0

    def generate_bars():
        nonlocal iterations
        iterations += 1
        yield make_bar()

    fills = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=generate_bars(),
    )

    assert iterations == 1
    assert len(fills) == 1


def test_process_session_requires_configured_calendar() -> None:
    simulator = ExecutionSimulator({})

    with pytest.raises(ValueError, match="calendar"):
        simulator.process_session(
            market=Market.US,
            session_date=date(2026, 7, 27),
            bars=[],
        )


def test_process_session_requires_explicit_market_session() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    with pytest.raises(ValueError, match="session"):
        simulator.process_session(
            market=Market.US,
            session_date=date(2026, 7, 25),
            bars=[],
        )


def make_fill(**overrides: object) -> Fill:
    values = {
        "status": FillStatus.PENDING,
        "order_id": "order-1",
        "account_id": "account-1",
        "symbol": "AAPL",
        "market": Market.US,
        "side": Side.BUY,
        "requested_quantity": Decimal("10"),
        "filled_quantity": Decimal("0"),
        "price": None,
        "fees": Decimal("0"),
        "session_date": None,
        "reason": None,
    }
    values.update(overrides)
    return Fill(**values)


def test_order_intent_strips_text_canonicalizes_symbol_and_accepts_all_sides() -> None:
    for side in Side:
        intent = make_intent(
            order_id=" order-1 ",
            account_id=" account-1 ",
            symbol=" aapl ",
            side=side,
        )
        assert intent.order_id == "order-1"
        assert intent.account_id == "account-1"
        assert intent.symbol == "AAPL"
        assert intent.side is side


@pytest.mark.parametrize("field", ["order_id", "account_id", "symbol"])
def test_order_intent_rejects_blank_text(field: str) -> None:
    with pytest.raises(ValidationError):
        make_intent(**{field: "   "})


@pytest.mark.parametrize(
    "value",
    [
        Decimal("0"),
        Decimal("-1"),
        1,
        1.0,
        "1",
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_order_intent_quantity_is_strict_positive_finite_decimal(value: object) -> None:
    with pytest.raises(ValidationError):
        make_intent(quantity=value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": FillStatus.PENDING, "filled_quantity": Decimal("1")},
        {"status": FillStatus.PENDING, "price": Decimal("1")},
        {"status": FillStatus.PENDING, "fees": Decimal("1")},
        {"status": FillStatus.PENDING, "session_date": date(2026, 7, 27)},
        {"status": FillStatus.PENDING, "reason": "waiting"},
        {"status": FillStatus.REJECTED, "reason": None},
        {"status": FillStatus.REJECTED, "reason": "   "},
        {"status": FillStatus.REJECTED, "filled_quantity": Decimal("1"), "reason": "x"},
        {"status": FillStatus.REJECTED, "price": Decimal("1"), "reason": "x"},
        {"status": FillStatus.REJECTED, "fees": Decimal("1"), "reason": "x"},
        {
            "status": FillStatus.REJECTED,
            "session_date": date(2026, 7, 27),
            "reason": "x",
        },
        {
            "status": FillStatus.FILLED,
            "filled_quantity": Decimal("9"),
            "price": Decimal("100"),
            "session_date": date(2026, 7, 27),
        },
        {
            "status": FillStatus.FILLED,
            "filled_quantity": Decimal("10"),
            "price": None,
            "session_date": date(2026, 7, 27),
        },
        {
            "status": FillStatus.FILLED,
            "filled_quantity": Decimal("10"),
            "price": Decimal("100"),
            "session_date": None,
        },
        {
            "status": FillStatus.FILLED,
            "filled_quantity": Decimal("10"),
            "price": Decimal("100"),
            "session_date": date(2026, 7, 27),
            "reason": "unexpected",
        },
    ],
)
def test_fill_enforces_status_invariants(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_fill(**overrides)


def test_fill_accepts_valid_rejected_and_filled_states() -> None:
    rejected = make_fill(
        status=FillStatus.REJECTED,
        order_id=" order-1 ",
        account_id=" account-1 ",
        symbol=" aapl ",
        reason=" unavailable ",
    )
    filled = make_fill(
        status=FillStatus.FILLED,
        filled_quantity=Decimal("10"),
        price=Decimal("100"),
        fees=Decimal("0.5"),
        session_date=date(2026, 7, 27),
    )

    assert rejected.order_id == "order-1"
    assert rejected.account_id == "account-1"
    assert rejected.symbol == "AAPL"
    assert rejected.reason == "unavailable"
    assert filled.reason is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_quantity", Decimal("0")),
        ("requested_quantity", Decimal("-1")),
        ("filled_quantity", Decimal("-1")),
        ("price", Decimal("0")),
        ("price", Decimal("-1")),
        ("fees", Decimal("-1")),
    ],
)
def test_fill_decimal_fields_enforce_bounds(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_fill(**{field: value})


@pytest.mark.parametrize(
    ("field", "valid"),
    [
        ("requested_quantity", Decimal("10")),
        ("filled_quantity", Decimal("0")),
        ("price", Decimal("100")),
        ("fees", Decimal("0")),
    ],
)
@pytest.mark.parametrize("input_type", [int, float, str])
def test_fill_decimal_fields_reject_non_decimal_inputs(
    field: str, valid: Decimal, input_type: Callable[[Decimal], object]
) -> None:
    with pytest.raises(ValidationError):
        make_fill(**{field: input_type(valid)})


@pytest.mark.parametrize("field", ["requested_quantity", "filled_quantity", "price", "fees"])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_fill_decimal_fields_reject_non_finite_values(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_fill(**{field: value})


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 27), "2026-07-27"])
def test_fill_session_date_requires_plain_date(invalid: object) -> None:
    with pytest.raises(ValidationError):
        make_fill(
            status=FillStatus.FILLED,
            filled_quantity=Decimal("10"),
            price=Decimal("100"),
            session_date=invalid,
        )


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 24), "2026-07-24"])
def test_simulator_date_parameters_require_plain_dates(invalid: object) -> None:
    submit_simulator = ExecutionSimulator({Market.US: make_calendar()})
    process_simulator = ExecutionSimulator({Market.US: make_calendar()})

    with pytest.raises(TypeError):
        submit_simulator.submit(make_intent(), invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        process_simulator.process_session(
            market=Market.US,
            session_date=invalid,  # type: ignore[arg-type]
            bars=[],
        )


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    intent = make_intent()
    fill = make_fill()

    with pytest.raises(ValidationError):
        intent.quantity = Decimal("11")
    with pytest.raises(ValidationError):
        fill.fees = Decimal("1")
    with pytest.raises(ValidationError):
        OrderIntent(
            order_id="x",
            account_id="a",
            symbol="AAPL",
            market=Market.US,
            side=Side.BUY,
            quantity=Decimal("1"),
            extra="forbidden",
        )
    with pytest.raises(ValidationError):
        Fill(**{**fill.model_dump(), "extra": "forbidden"})


def test_calendar_keys_must_match_calendar_market() -> None:
    with pytest.raises(ValueError, match="market"):
        ExecutionSimulator({Market.CN: make_calendar(Market.US)})


@pytest.mark.parametrize(
    "invalid_cost",
    [12, 12.0, "12", Decimal("-1"), Decimal("NaN"), Decimal("Infinity")],
)
def test_transaction_cost_overrides_require_nonnegative_finite_decimals(
    invalid_cost: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ExecutionSimulator(
            {Market.US: make_calendar()},
            transaction_cost_bps={Market.US: invalid_cost},  # type: ignore[dict-item]
        )


def test_constructor_copies_calendar_and_cost_mappings() -> None:
    calendars = {Market.US: make_calendar()}
    costs = {Market.US: Decimal("7")}
    simulator = ExecutionSimulator(calendars, costs)
    calendars.clear()
    costs[Market.US] = Decimal("999")

    simulator.submit(make_intent(), date(2026, 7, 24))
    fill = simulator.process_session(
        market=Market.US,
        session_date=date(2026, 7, 27),
        bars=[make_bar()],
    )[0]

    assert fill.fees == Decimal("0.7")


def test_pending_order_ids_is_read_only() -> None:
    simulator = ExecutionSimulator({Market.US: make_calendar()})

    with pytest.raises(AttributeError):
        simulator.pending_order_ids = ("replacement",)


def test_execution_public_api_exports_only_requested_types() -> None:
    expected = {"FillStatus", "OrderIntent", "Fill", "ExecutionSimulator"}
    assert set(execution.__all__) == expected
    assert {name for name in expected if hasattr(execution, name)} == expected
    assert list(FillStatus) == [
        FillStatus.PENDING,
        FillStatus.FILLED,
        FillStatus.REJECTED,
    ]
