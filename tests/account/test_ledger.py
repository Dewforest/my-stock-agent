from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError

import stock_agent.account as account
from stock_agent.account import (
    AcquisitionLot,
    BuyFilled,
    CashAdjusted,
    CashInitialized,
    EventReversed,
    PortfolioLedger,
    PositionMarked,
    SellFilled,
)
from stock_agent.domain import Market

BASE_TIME = datetime(2026, 7, 28, 12, tzinfo=UTC)


def initialized_ledger(amount: str = "1000") -> PortfolioLedger:
    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(
        CashInitialized(
            event_id="init",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME,
            amount=Decimal(amount),
        )
    )
    return ledger


def buy_event(event_id: str = "buy-1", seconds: int = 1, **overrides: object) -> BuyFilled:
    values = {
        "event_id": event_id,
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": BASE_TIME + timedelta(seconds=seconds),
        "symbol": "AAPL",
        "session_date": date(2026, 7, 28),
        "quantity": Decimal("2"),
        "price": Decimal("100"),
        "fees": Decimal("1"),
    }
    values.update(overrides)
    return BuyFilled(**values)


def sell_event(event_id: str = "sell-1", seconds: int = 2, **overrides: object) -> SellFilled:
    values = {
        "event_id": event_id,
        "account_id": "account-1",
        "market": Market.US,
        "occurred_at": BASE_TIME + timedelta(seconds=seconds),
        "symbol": "AAPL",
        "session_date": date(2026, 7, 28),
        "quantity": Decimal("1"),
        "price": Decimal("120"),
        "fees": Decimal("2"),
    }
    values.update(overrides)
    return SellFilled(**values)


def test_cash_initialization_exposes_the_first_atomic_snapshot() -> None:
    ledger = PortfolioLedger(" account-1 ", Market.US)
    event = CashInitialized(
        event_id="init",
        account_id="account-1",
        market=Market.US,
        occurred_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        amount=Decimal("1000.00"),
    )

    for attribute in ("cash", "positions", "lots", "realized_pnl"):
        with pytest.raises(RuntimeError):
            getattr(ledger, attribute)
    with pytest.raises(RuntimeError):
        ledger.snapshot()

    ledger.append(event)

    assert ledger.account_id == "account-1"
    assert ledger.market is Market.US
    assert ledger.events == (event,)
    assert ledger.cash == Decimal("1000.00")
    assert ledger.realized_pnl == Decimal("0")
    assert ledger.positions == ()
    assert ledger.snapshot().cash == Decimal("1000.00")
    assert ledger.snapshot().nav == Decimal("1000.00")
    assert ledger.snapshot().peak_nav == Decimal("1000.00")
    assert ledger.snapshot().as_of == event.occurred_at


def test_buy_uses_fee_in_cost_basis_and_marks_at_fill_price() -> None:
    ledger = initialized_ledger()
    event = buy_event()

    ledger.append(event)

    assert ledger.cash == Decimal("799")
    assert ledger.realized_pnl == Decimal("0")
    assert len(ledger.positions) == 1
    position = ledger.positions[0]
    assert position.symbol == "AAPL"
    assert position.quantity == Decimal("2")
    assert position.average_cost == Decimal("100.5")
    assert position.market_value == Decimal("200")
    assert ledger.snapshot().nav == Decimal("999")
    assert ledger.snapshot().peak_nav == Decimal("1000")


def test_mark_updates_market_value_nav_and_peak_only() -> None:
    ledger = initialized_ledger()
    ledger.append(buy_event())
    before_cash = ledger.cash
    before_average_cost = ledger.positions[0].average_cost

    ledger.append(
        PositionMarked(
            event_id="mark-1",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME + timedelta(seconds=2),
            symbol="AAPL",
            session_date=date(2026, 7, 28),
            price=Decimal("110"),
        )
    )

    assert ledger.cash == before_cash
    assert ledger.realized_pnl == Decimal("0")
    assert ledger.positions[0].average_cost == before_average_cost
    assert ledger.positions[0].market_value == Decimal("220")
    assert ledger.snapshot().nav == Decimal("1019")
    assert ledger.snapshot().peak_nav == Decimal("1019")


def test_partial_sell_allocates_fifo_cost_and_fees_against_realized_pnl() -> None:
    ledger = initialized_ledger()
    ledger.append(buy_event())

    ledger.append(sell_event())

    assert ledger.cash == Decimal("917")
    assert ledger.realized_pnl == Decimal("17.5")
    assert ledger.positions[0].quantity == Decimal("1")
    assert ledger.positions[0].average_cost == Decimal("100.5")
    assert ledger.positions[0].market_value == Decimal("120")
    assert ledger.snapshot().nav == Decimal("1037")
    assert ledger.snapshot().peak_nav == Decimal("1037")


def test_partial_sell_uses_fifo_and_preserves_remaining_acquisition_lots() -> None:
    ledger = initialized_ledger()
    ledger.append(
        buy_event(
            quantity=Decimal("3"),
            price=Decimal("10"),
            fees=Decimal("0.30"),
            session_date=date(2026, 7, 27),
        )
    )
    ledger.append(
        buy_event(
            event_id="buy-2",
            seconds=2,
            quantity=Decimal("2"),
            price=Decimal("20"),
            fees=Decimal("0.20"),
        )
    )

    ledger.append(
        sell_event(
            seconds=3,
            quantity=Decimal("1"),
            price=Decimal("30"),
            fees=Decimal("0.10"),
        )
    )

    assert ledger.realized_pnl == Decimal("19.80")
    assert tuple(
        (lot.symbol, lot.acquired_session, lot.quantity, lot.cost_basis)
        for lot in ledger.lots
    ) == (
        ("AAPL", date(2026, 7, 27), Decimal("2"), Decimal("20.20")),
        ("AAPL", date(2026, 7, 28), Decimal("2"), Decimal("40.20")),
    )
    assert ledger.positions[0].quantity == Decimal("4")
    assert ledger.positions[0].average_cost == Decimal("15.10")


def test_cash_adjustment_changes_cash_and_nav_but_not_realized_pnl() -> None:
    ledger = initialized_ledger()

    ledger.append(
        CashAdjusted(
            event_id="deposit",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME + timedelta(seconds=1),
            amount=Decimal("50"),
            reason="deposit",
        )
    )

    assert ledger.cash == Decimal("1050")
    assert ledger.realized_pnl == Decimal("0")
    assert ledger.snapshot().nav == Decimal("1050")
    assert ledger.snapshot().peak_nav == Decimal("1050")


def ledger_state(ledger: PortfolioLedger) -> tuple[object, ...]:
    return (
        ledger.events,
        ledger.cash,
        ledger.positions,
        ledger.lots,
        ledger.realized_pnl,
        ledger.snapshot(),
    )


def test_full_sell_removes_position_without_cost_residue() -> None:
    ledger = initialized_ledger()
    ledger.append(buy_event())
    ledger.append(sell_event())

    ledger.append(
        sell_event(
            event_id="sell-2",
            seconds=3,
            price=Decimal("130"),
            fees=Decimal("1"),
        )
    )

    assert ledger.positions == ()
    assert ledger.cash == Decimal("1046")
    assert ledger.realized_pnl == Decimal("46")
    assert ledger.snapshot().nav == Decimal("1046")
    assert ledger.snapshot().peak_nav == Decimal("1046")


def test_multiple_buys_project_weighted_average_and_latest_fill_mark() -> None:
    ledger = initialized_ledger()
    ledger.append(buy_event())
    ledger.append(
        buy_event(
            event_id="buy-2",
            seconds=2,
            quantity=Decimal("3"),
            price=Decimal("110"),
            fees=Decimal("2"),
        )
    )

    assert ledger.cash == Decimal("467")
    assert ledger.positions[0].quantity == Decimal("5")
    assert ledger.positions[0].average_cost == Decimal("106.6")
    assert ledger.positions[0].market_value == Decimal("550")
    assert ledger.snapshot().nav == Decimal("1017")
    assert ledger.snapshot().peak_nav == Decimal("1017")


def test_consecutive_fifo_sells_cross_lots_and_finish_without_residue() -> None:
    ledger = initialized_ledger()
    ledger.append(
        buy_event(quantity=Decimal("3"), price=Decimal("10"), fees=Decimal("0.30"))
    )
    ledger.append(
        buy_event(
            event_id="buy-2",
            seconds=2,
            quantity=Decimal("2"),
            price=Decimal("20"),
            fees=Decimal("0.20"),
        )
    )
    ledger.append(
        sell_event(
            event_id="sell-1",
            seconds=3,
            quantity=Decimal("2"),
            price=Decimal("30"),
            fees=Decimal("0.10"),
        )
    )

    assert ledger.realized_pnl == Decimal("39.70")
    assert tuple((lot.quantity, lot.cost_basis) for lot in ledger.lots) == (
        (Decimal("1"), Decimal("10.10")),
        (Decimal("2"), Decimal("40.20")),
    )

    ledger.append(
        sell_event(
            event_id="sell-2",
            seconds=4,
            quantity=Decimal("2"),
            price=Decimal("25"),
            fees=Decimal("0.10"),
        )
    )

    assert ledger.realized_pnl == Decimal("59.40")
    assert tuple((lot.quantity, lot.cost_basis) for lot in ledger.lots) == (
        (Decimal("1"), Decimal("20.10")),
    )

    ledger.append(
        sell_event(
            event_id="sell-3",
            seconds=5,
            quantity=Decimal("1"),
            price=Decimal("22"),
            fees=Decimal("0.10"),
        )
    )

    assert ledger.cash == Decimal("1061.20")
    assert ledger.realized_pnl == Decimal("61.20")
    assert ledger.lots == ()
    assert ledger.positions == ()
    assert ledger.snapshot().nav == Decimal("1061.20")


def test_lots_distinguish_equal_aggregate_positions_by_acquisition_session() -> None:
    split = initialized_ledger("10000")
    split.append(
        buy_event(
            quantity=Decimal("100"),
            price=Decimal("10"),
            fees=Decimal("0"),
            session_date=date(2026, 7, 27),
        )
    )
    split.append(
        buy_event(
            event_id="buy-2",
            seconds=2,
            quantity=Decimal("100"),
            price=Decimal("10"),
            fees=Decimal("0"),
            session_date=date(2026, 7, 28),
        )
    )
    combined = initialized_ledger("10000")
    combined.append(
        buy_event(
            quantity=Decimal("200"),
            price=Decimal("10"),
            fees=Decimal("0"),
            session_date=date(2026, 7, 27),
        )
    )

    assert split.positions == combined.positions
    assert tuple((lot.acquired_session, lot.quantity) for lot in split.lots) == (
        (date(2026, 7, 27), Decimal("100")),
        (date(2026, 7, 28), Decimal("100")),
    )
    assert tuple((lot.acquired_session, lot.quantity) for lot in combined.lots) == (
        (date(2026, 7, 27), Decimal("200")),
    )


def test_financial_replay_failures_are_atomic() -> None:
    cases = [
        buy_event(
            event_id="too-expensive",
            seconds=2,
            quantity=Decimal("100"),
            price=Decimal("100"),
        ),
        sell_event(event_id="oversell", seconds=2, quantity=Decimal("3")),
        sell_event(event_id="negative-proceeds", seconds=2, fees=Decimal("121")),
        CashAdjusted(
            event_id="overdraw",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME + timedelta(seconds=2),
            amount=Decimal("-800"),
            reason="withdrawal",
        ),
        PositionMarked(
            event_id="unknown-mark",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME + timedelta(seconds=2),
            symbol="MSFT",
            session_date=date(2026, 7, 28),
            price=Decimal("100"),
        ),
        CashInitialized(
            event_id="second-init",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME + timedelta(seconds=2),
            amount=Decimal("1000"),
        ),
    ]

    for event in cases:
        ledger = initialized_ledger()
        ledger.append(buy_event())
        before = ledger_state(ledger)
        with pytest.raises(ValueError):
            ledger.append(event)
        assert ledger_state(ledger) == before


def test_append_contract_failures_are_atomic_and_compare_instants() -> None:
    ledger = initialized_ledger()
    before = ledger_state(ledger)
    invalid_events: list[object] = [
        object(),
        buy_event(account_id="other"),
        buy_event(market=Market.CN),
        buy_event(event_id="init"),
        buy_event(occurred_at=BASE_TIME),
        buy_event(
            occurred_at=datetime(
                2026, 7, 28, 8, tzinfo=timezone(timedelta(hours=-4))
            )
        ),
    ]

    for event in invalid_events:
        with pytest.raises((TypeError, ValueError)):
            ledger.append(event)  # type: ignore[arg-type]
        assert ledger_state(ledger) == before


def test_cash_adjustment_can_be_reversed_without_removing_events() -> None:
    ledger = initialized_ledger()
    adjustment = CashAdjusted(
        event_id="deposit",
        account_id="account-1",
        market=Market.US,
        occurred_at=BASE_TIME + timedelta(seconds=1),
        amount=Decimal("50"),
        reason="deposit",
    )
    reversal = EventReversed(
        event_id="reverse-1",
        account_id="account-1",
        market=Market.US,
        occurred_at=BASE_TIME + timedelta(seconds=2),
        target_event_id="deposit",
        reason="correction",
    )
    ledger.append(adjustment)

    ledger.append(reversal)

    assert ledger.events == (ledger.events[0], adjustment, reversal)
    assert ledger.cash == Decimal("1000")
    assert ledger.snapshot().as_of == reversal.occurred_at


def test_positions_are_sorted_and_returned_objects_are_immutable() -> None:
    ledger = initialized_ledger()
    ledger.append(buy_event(symbol="MSFT"))
    ledger.append(buy_event(event_id="buy-2", seconds=2, symbol="AAPL"))

    assert tuple(position.symbol for position in ledger.positions) == ("AAPL", "MSFT")
    assert isinstance(ledger.events, tuple)
    assert isinstance(ledger.positions, tuple)
    assert isinstance(ledger.lots, tuple)
    assert tuple(lot.symbol for lot in ledger.lots) == ("AAPL", "MSFT")
    assert ledger.snapshot().positions == ledger.positions
    with pytest.raises(ValidationError):
        ledger.positions[0].quantity = Decimal("99")
    with pytest.raises(ValidationError):
        ledger.snapshot().cash = Decimal("0")
    with pytest.raises(ValidationError):
        ledger.lots[0].quantity = Decimal("99")


def test_private_decimal_context_ignores_hostile_ambient_context() -> None:
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        ledger = initialized_ledger()
        ledger.append(buy_event())
        ledger.append(
            buy_event(
                event_id="buy-2",
                seconds=2,
                quantity=Decimal("3"),
                price=Decimal("110"),
                fees=Decimal("2"),
            )
        )

    assert ledger.positions[0].average_cost == Decimal("106.6")
    assert ledger.snapshot().nav == Decimal("1017")


def test_decimal_arithmetic_error_is_value_error_and_atomic() -> None:
    ledger = PortfolioLedger("account-1", Market.US)
    ledger.append(
        CashInitialized.model_construct(
            event_id="init",
            account_id="account-1",
            market=Market.US,
            occurred_at=BASE_TIME,
            amount=Decimal("1E+999999"),
        )
    )
    before = ledger_state(ledger)
    extreme = BuyFilled.model_construct(
        event_id="extreme",
        account_id="account-1",
        market=Market.US,
        occurred_at=BASE_TIME + timedelta(seconds=1),
        symbol="AAPL",
        session_date=date(2026, 7, 28),
        quantity=Decimal("1E+999999"),
        price=Decimal("1E+999999"),
        fees=Decimal("0"),
    )

    with pytest.raises(ValueError, match="decimal arithmetic failed"):
        ledger.append(extreme)

    assert ledger_state(ledger) == before


@pytest.mark.parametrize("account_id", ["", "   "])
def test_ledger_rejects_blank_account_id(account_id: str) -> None:
    with pytest.raises(ValueError):
        PortfolioLedger(account_id, Market.US)


@pytest.mark.parametrize("market", ["US", None, 1])
def test_ledger_requires_strict_market(market: object) -> None:
    with pytest.raises(TypeError):
        PortfolioLedger("account-1", market)  # type: ignore[arg-type]


def test_public_export_includes_portfolio_ledger() -> None:
    assert "AcquisitionLot" in account.__all__
    assert account.AcquisitionLot is AcquisitionLot
    assert account.__all__[-1] == "PortfolioLedger"
    assert account.PortfolioLedger is PortfolioLedger
