from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal, Inexact, localcontext

import pytest

from stock_agent.account import (
    AcquisitionLot,
    BookedFill,
    CashInitialized,
    OpenExecutionBatchBooked,
    PortfolioLedger,
    PositionMark,
)
from stock_agent.domain import Bar, Market, Side
from stock_agent.execution import ExecutionSimulator, FillStatus, OrderIntent
from stock_agent.market import TradingCalendar

D1 = date(2026, 7, 24)
D2 = date(2026, 7, 27)
D3 = date(2026, 7, 28)


def simulator(*, bps: str = "5") -> ExecutionSimulator:
    return ExecutionSimulator(
        {Market.US: TradingCalendar(Market.US, (D1, D2, D3))},
        transaction_cost_bps={Market.US: Decimal(bps)},
    )


def intent(
    order_id: str,
    *,
    account_id: str = "account-1",
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: str = "1",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        account_id=account_id,
        symbol=symbol,
        market=Market.US,
        side=side,
        quantity=Decimal(quantity),
    )


def bar(symbol: str = "AAPL", *, open_price: str = "100", day: date = D2) -> Bar:
    price = Decimal(open_price)
    return Bar(
        symbol=symbol,
        market=Market.US,
        session_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        available_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
    )


def lot(symbol: str = "AAPL", quantity: str = "1") -> AcquisitionLot:
    return AcquisitionLot(
        symbol=symbol,
        acquired_session=D1,
        quantity=Decimal(quantity),
        cost_basis=Decimal("1"),
    )


def test_gap_up_buy_plus_fees_is_terminally_rejected_when_cash_is_insufficient() -> None:
    value = simulator(bps="100")
    value.submit(intent("gap", quantity="2"), D1)

    fill = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(open_price="60")],
        available_cash_by_account={"account-1": Decimal("120")},
    )[0]

    assert fill.status is FillStatus.REJECTED
    assert fill.reason == "insufficient available cash"
    assert fill.price is None
    assert fill.fees == Decimal("0")
    assert value.pending_order_ids == ()


class DuplicateCashAccounts(Mapping[str, Decimal]):
    def __getitem__(self, key: str) -> Decimal:
        return Decimal("100")

    def __iter__(self) -> Iterator[str]:
        return iter(("account-1", " account-1 "))

    def __len__(self) -> int:
        return 2

    def items(self):  # type: ignore[override]
        return (
            ("account-1", Decimal("100")),
            (" account-1 ", Decimal("100")),
        )


@pytest.mark.parametrize(
    "cash",
    [
        [],
        {1: Decimal("100")},
        {"": Decimal("100")},
        {"account-1": 100},
        {"account-1": Decimal("NaN")},
        {"account-1": Decimal("-1")},
        {"account-1": Decimal("1E26")},
        {"account-1": Decimal("1.0000000000001")},
        DuplicateCashAccounts(),
    ],
)
def test_invalid_cash_mapping_is_atomic_and_does_not_advance_timeline(
    cash: object,
) -> None:
    value = simulator()
    value.submit(intent("atomic"), D1)

    with pytest.raises((TypeError, ValueError)):
        value.process_session(
            market=Market.US,
            session_date=D3,
            bars=[bar(day=D3)],
            available_cash_by_account=cash,  # type: ignore[arg-type]
        )

    assert value.pending_order_ids == ("atomic",)
    fill = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar()],
        available_cash_by_account={"account-1": Decimal("101")},
    )[0]
    assert fill.status is FillStatus.FILLED


def test_missing_cash_for_an_eligible_account_is_atomic() -> None:
    value = simulator()
    value.submit(intent("first", account_id="account-1"), D1)
    value.submit(intent("second", account_id="account-2", symbol="MSFT"), D1)

    with pytest.raises(
        ValueError, match="available_cash_by_account missing eligible account account-2"
    ):
        value.process_session(
            market=Market.US,
            session_date=D2,
            bars=[bar(), bar("MSFT")],
            available_cash_by_account={"account-1": Decimal("1000")},
        )

    assert value.pending_order_ids == ("first", "second")


def test_missing_cash_for_eligible_order_without_bar_is_atomic() -> None:
    value = simulator()
    value.submit(intent("no-bar"), D1)

    with pytest.raises(
        ValueError, match="available_cash_by_account missing eligible account account-1"
    ):
        value.process_session(
            market=Market.US,
            session_date=D3,
            bars=(),
            available_cash_by_account={},
        )

    assert value.pending_order_ids == ("no-bar",)
    fill = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar()],
        available_cash_by_account={"account-1": Decimal("101")},
    )[0]
    assert fill.status is FillStatus.FILLED


def test_omitted_cash_mapping_preserves_legacy_unfunded_fill_behavior() -> None:
    value = simulator(bps="100")
    value.submit(intent("legacy", quantity="2"), D1)

    fill = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(open_price="60")],
    )[0]

    assert fill.status is FillStatus.FILLED
    assert fill.price == Decimal("60")
    assert fill.fees == Decimal("1.2")


def test_exact_cash_boundary_fills_and_buy_orders_compete_in_pending_order() -> None:
    value = simulator(bps="100")
    value.submit(intent("first", symbol="AAPL"), D1)
    value.submit(intent("second", symbol="MSFT"), D1)

    fills = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(), bar("MSFT")],
        available_cash_by_account={"account-1": Decimal("101")},
    )

    assert [(fill.order_id, fill.status, fill.reason) for fill in fills] == [
        ("first", FillStatus.FILLED, None),
        ("second", FillStatus.REJECTED, "insufficient available cash"),
    ]


def test_earlier_sell_funds_later_buy_but_later_sell_does_not_rescue_buy() -> None:
    funded = simulator(bps="0")
    funded.submit(intent("sell", side=Side.SELL), D1)
    funded.submit(intent("buy", symbol="MSFT"), D1)
    funded_fills = funded.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(), bar("MSFT")],
        account_lots={"account-1": [lot()]},
        available_cash_by_account={"account-1": Decimal("0")},
    )

    unfunded = simulator(bps="0")
    unfunded.submit(intent("buy", symbol="MSFT"), D1)
    unfunded.submit(intent("sell", side=Side.SELL), D1)
    unfunded_fills = unfunded.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(), bar("MSFT")],
        account_lots={"account-1": [lot()]},
        available_cash_by_account={"account-1": Decimal("0")},
    )

    assert [fill.status for fill in funded_fills] == [
        FillStatus.FILLED,
        FillStatus.FILLED,
    ]
    assert [(fill.status, fill.reason) for fill in unfunded_fills] == [
        (FillStatus.REJECTED, "insufficient available cash"),
        (FillStatus.FILLED, None),
    ]


def test_sell_fees_over_proceeds_are_rejected_without_funding_later_buy() -> None:
    value = simulator(bps="20000")
    value.submit(intent("sell", side=Side.SELL), D1)
    value.submit(intent("buy", symbol="MSFT"), D1)

    fills = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(), bar("MSFT", open_price="1")],
        account_lots={"account-1": [lot()]},
        available_cash_by_account={"account-1": Decimal("0")},
    )

    assert [(fill.status, fill.reason) for fill in fills] == [
        (FillStatus.REJECTED, "fees exceed sell proceeds"),
        (FillStatus.REJECTED, "insufficient available cash"),
    ]


def test_rejected_buy_does_not_reduce_cash_available_to_a_later_buy() -> None:
    value = simulator(bps="100")
    value.submit(intent("too-expensive", symbol="AAPL"), D1)
    value.submit(intent("affordable", symbol="MSFT"), D1)

    fills = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(open_price="101"), bar("MSFT", open_price="99")],
        available_cash_by_account={"account-1": Decimal("100")},
    )

    assert [fill.status for fill in fills] == [FillStatus.REJECTED, FillStatus.FILLED]


def test_cash_arithmetic_is_hostile_context_safe_and_leaves_ambient_unchanged() -> None:
    value = simulator(bps="100")
    value.submit(intent("hostile"), D1)

    with localcontext() as hostile:
        hostile.prec = 2
        hostile.rounding = ROUND_CEILING
        hostile.Emin = -1
        hostile.Emax = 1
        hostile.traps[Inexact] = True
        before = repr(hostile)
        fill = value.process_session(
            market=Market.US,
            session_date=D2,
            bars=[bar()],
            available_cash_by_account={" account-1 ": Decimal("101")},
        )[0]
        after = repr(hostile)

    assert fill.status is FillStatus.FILLED
    assert before == after


def test_unsupported_cash_arithmetic_is_a_stable_terminal_rejection() -> None:
    value = simulator(bps="0")
    value.submit(intent("range", quantity="1E13"), D1)

    fill = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(open_price="1E13")],
        available_cash_by_account={"account-1": Decimal("9E25")},
    )[0]

    assert fill.status is FillStatus.REJECTED
    assert fill.reason == "cash change has unsupported numeric range/precision"
    assert value.pending_order_ids == ()


def test_successful_cash_aware_fills_reconcile_with_open_execution_batch() -> None:
    value = simulator(bps="100")
    value.submit(intent("aapl", symbol="AAPL"), D1)
    value.submit(intent("msft", symbol="MSFT"), D1)
    fills = value.process_session(
        market=Market.US,
        session_date=D2,
        bars=[bar(), bar("MSFT", open_price="50")],
        available_cash_by_account={"account-1": Decimal("200")},
    )

    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(
        CashInitialized(
            event_id="init",
            account_id="account-1",
            market=Market.US,
            occurred_at=datetime(2026, 7, 27, 13, 29, tzinfo=UTC),
            amount=Decimal("200"),
        )
    )
    booked = tuple(
        BookedFill(
            fill_id=fill.order_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.filled_quantity,
            price=fill.price,
            fees=fill.fees,
        )
        for fill in fills
        if fill.status is FillStatus.FILLED
    )
    ledger.append(
        OpenExecutionBatchBooked(
            event_id="open",
            account_id="account-1",
            market=Market.US,
            occurred_at=datetime(2026, 7, 27, 13, 30, tzinfo=UTC),
            session_date=D2,
            fills=booked,
            marks=(
                PositionMark(symbol="AAPL", price=Decimal("100")),
                PositionMark(symbol="MSFT", price=Decimal("50")),
            ),
        )
    )

    assert ledger.cash == Decimal("48.5")
    assert [(item.symbol, item.quantity, item.cost_basis) for item in ledger.lots] == [
        ("AAPL", Decimal("1"), Decimal("101")),
        ("MSFT", Decimal("1"), Decimal("50.5")),
    ]
    assert ledger.snapshot().nav == Decimal("198.5")
