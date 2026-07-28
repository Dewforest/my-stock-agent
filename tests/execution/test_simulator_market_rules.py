from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import pytest

from stock_agent.account import AcquisitionLot
from stock_agent.domain import Bar, Market, Side
from stock_agent.execution import ExecutionSimulator, FillStatus
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.execution.models import OrderIntent
from stock_agent.execution.rules import ChinaAShareRules, USCashEquityRules
from stock_agent.market import TradingCalendar

D1 = date(2026, 7, 27)
D2 = date(2026, 7, 28)
D3 = date(2026, 7, 29)


def calendar(market: Market) -> TradingCalendar:
    return TradingCalendar(market, (D1, D2, D3))


def intent(
    order_id: str,
    *,
    market: Market = Market.CN,
    side: Side = Side.BUY,
    quantity: str = "100",
    symbol: str | None = None,
    account_id: str = "account-1",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        account_id=account_id,
        symbol=symbol or ("600000" if market is Market.CN else "AAPL"),
        market=market,
        side=side,
        quantity=Decimal(quantity),
    )


def bar(*, market: Market = Market.CN, symbol: str | None = None) -> Bar:
    return Bar(
        symbol=symbol or ("600000" if market is Market.CN else "AAPL"),
        market=market,
        session_date=D2,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("1000"),
        available_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )


def state(
    *,
    symbol: str = "600000",
    suspended: bool = False,
    limit: CnPriceLimitState = CnPriceLimitState.NONE,
) -> CnSessionState:
    return CnSessionState(
        symbol=symbol,
        session_date=D2,
        suspended=suspended,
        price_limit_state=limit,
    )


def lot(symbol: str, acquired: date, quantity: str) -> AcquisitionLot:
    return AcquisitionLot(
        symbol=symbol,
        acquired_session=acquired,
        quantity=Decimal(quantity),
        cost_basis=Decimal("100"),
    )


def test_cn_buy_is_rounded_down_before_pending_and_fill_while_us_is_unchanged() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN), Market.US: calendar(Market.US)})

    cn_pending = simulator.submit(intent("cn", quantity="250"), D1)
    us_pending = simulator.submit(intent("us", market=Market.US, quantity="99.125"), D1)

    assert cn_pending.requested_quantity == Decimal("200")
    assert us_pending.requested_quantity == Decimal("99.125")
    cn_fill = simulator.process_session(
        market=Market.CN, session_date=D2, bars=[bar()], session_states=[state()]
    )[0]
    us_fill = simulator.process_session(
        market=Market.US, session_date=D2, bars=[bar(market=Market.US)]
    )[0]
    assert (cn_fill.requested_quantity, cn_fill.filled_quantity, cn_fill.fees) == (
        Decimal("200"), Decimal("200"), Decimal("2.4")
    )
    assert us_fill.filled_quantity == Decimal("99.125")


def test_cn_buy_below_one_lot_is_rejected_and_order_id_remains_reserved() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})

    rejected = simulator.submit(intent("small", quantity="99"), D1)
    duplicate = simulator.submit(intent("small", quantity="200"), D1)

    assert rejected.status is FillStatus.REJECTED
    assert rejected.reason and "100" in rejected.reason and "lot" in rejected.reason.lower()
    assert duplicate.status is FillStatus.REJECTED
    assert "duplicate" in (duplicate.reason or "")
    assert simulator.pending_order_ids == ()


def test_cn_bar_without_state_stays_pending_and_can_fill_on_same_session_retry() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("late-state"), D1)

    assert simulator.process_session(market=Market.CN, session_date=D2, bars=[bar()]) == ()
    assert simulator.pending_order_ids == ("late-state",)

    fill = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=(item for item in [state()]),
    )[0]
    assert fill.status is FillStatus.FILLED
    assert simulator.pending_order_ids == ()


@pytest.mark.parametrize(
    ("side", "limit", "suspended", "blocked"),
    [
        (Side.BUY, CnPriceLimitState.NONE, True, True),
        (Side.BUY, CnPriceLimitState.LIMIT_UP, False, True),
        (Side.SELL, CnPriceLimitState.LIMIT_DOWN, False, True),
        (Side.BUY, CnPriceLimitState.LIMIT_DOWN, False, False),
        (Side.SELL, CnPriceLimitState.LIMIT_UP, False, False),
    ],
)
def test_cn_session_state_blocks_only_suspended_or_constrained_direction(
    side: Side,
    limit: CnPriceLimitState,
    suspended: bool,
    blocked: bool,
) -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("state-order", side=side), D1)
    lots = {"account-1": [lot("600000", D1, "100")]}

    fill = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state(suspended=suspended, limit=limit)],
        account_lots=lots,
    )[0]

    assert fill.status is (FillStatus.REJECTED if blocked else FillStatus.FILLED)
    if blocked:
        assert fill.reason and ("suspended" in fill.reason or "limit" in fill.reason)


def test_cn_sell_uses_account_lots_and_distinguishes_settled_from_unsettled() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("settled", side=Side.SELL, quantity="101"), D1)

    rejected = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
        account_lots={
            "account-1": [lot("600000", D1, "100"), lot("600000", D2, "100")]
        },
    )[0]

    assert rejected.status is FillStatus.REJECTED
    assert rejected.reason and ("t+1" in rejected.reason.lower() or "settled" in rejected.reason)

    fully_settled = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    fully_settled.submit(intent("all-settled", side=Side.SELL, quantity="200"), D1)
    filled = fully_settled.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
        account_lots={
            "account-1": [lot("600000", D1, "100"), lot("600000", D1, "100")]
        },
    )[0]
    assert filled.status is FillStatus.FILLED


def test_cn_sell_lot_checks_are_isolated_by_account_and_symbol() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("a-600", side=Side.SELL), D1)
    simulator.submit(
        intent("b-600", side=Side.SELL, account_id="account-2"), D1
    )
    simulator.submit(intent("a-001", side=Side.SELL, symbol="000001"), D1)

    fills = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar(), bar(symbol="000001")],
        session_states=[state(), state(symbol="000001")],
        account_lots={
            "account-1": [lot("600000", D1, "100"), lot("000001", D1, "100")],
            "account-2": [lot("600000", D2, "100")],
        },
    )

    assert [fill.status for fill in fills] == [
        FillStatus.FILLED,
        FillStatus.REJECTED,
        FillStatus.FILLED,
    ]


def test_cn_multiple_sells_share_remaining_settled_quantity() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("first", side=Side.SELL, quantity="60"), D1)
    simulator.submit(intent("second", side=Side.SELL, quantity="60"), D1)

    fills = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
        account_lots={"account-1": [lot("600000", D1, "100")]},
    )

    assert [fill.status for fill in fills] == [FillStatus.FILLED, FillStatus.REJECTED]
    assert fills[1].reason and "remaining" in fills[1].reason


def test_failed_fee_calculation_does_not_consume_shared_sellable_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("fee-fails", side=Side.SELL), D1)
    simulator.submit(intent("then-fills", side=Side.SELL), D1)
    original = simulator._calculate_fees
    calls = 0

    def fail_once(quantity: Decimal, price: Decimal, bps: Decimal) -> Decimal:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InvalidOperation
        return original(quantity, price, bps)

    monkeypatch.setattr(simulator, "_calculate_fees", fail_once)
    fills = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
        account_lots={"account-1": [lot("600000", D1, "100")]},
    )

    assert [fill.status for fill in fills] == [FillStatus.REJECTED, FillStatus.FILLED]


def test_cn_buy_does_not_make_a_later_same_session_sell_sellable() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("buy", side=Side.BUY), D1)
    simulator.submit(intent("sell", side=Side.SELL), D1)

    fills = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
        account_lots={"account-1": []},
    )

    assert [fill.status for fill in fills] == [FillStatus.FILLED, FillStatus.REJECTED]


class HalfQuantityUSRules(USCashEquityRules):
    def normalize_quantity(self, *, side: Side, quantity: Decimal) -> Decimal:
        return super().normalize_quantity(side=side, quantity=quantity) / Decimal("2")


class ExplodingUSRules(USCashEquityRules):
    def normalize_quantity(self, *, side: Side, quantity: Decimal) -> Decimal:
        raise RuntimeError("sensitive implementation detail")


def test_custom_rules_override_defaults_and_input_mapping_is_copied() -> None:
    rules = {Market.US: HalfQuantityUSRules()}
    simulator = ExecutionSimulator({Market.US: calendar(Market.US)}, rule_sets=rules)
    rules.clear()

    pending = simulator.submit(intent("custom", market=Market.US, quantity="3.5"), D1)

    assert pending.status is FillStatus.PENDING
    assert pending.requested_quantity == Decimal("1.75")


def test_rule_exception_rejects_without_leaking_detail_and_reserves_id() -> None:
    simulator = ExecutionSimulator(
        {Market.US: calendar(Market.US)},
        rule_sets={Market.US: ExplodingUSRules()},
    )

    rejected = simulator.submit(intent("boom", market=Market.US), D1)
    duplicate = simulator.submit(intent("boom", market=Market.US), D1)

    assert rejected.status is FillStatus.REJECTED
    assert "rule" in (rejected.reason or "")
    assert "sensitive" not in (rejected.reason or "")
    assert duplicate.status is FillStatus.REJECTED


def test_constructor_rejects_malformed_or_mismatched_custom_rules() -> None:
    cn = calendar(Market.CN)
    with pytest.raises(TypeError):
        ExecutionSimulator({Market.US: calendar(Market.US)}, rule_sets={"US": USCashEquityRules()})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        ExecutionSimulator({Market.US: calendar(Market.US)}, rule_sets={Market.US: object()})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="match"):
        ExecutionSimulator({Market.CN: cn}, rule_sets={Market.CN: USCashEquityRules()})
    with pytest.raises(ValueError, match="calendar"):
        ExecutionSimulator(
            {Market.CN: cn},
            rule_sets={
                Market.CN: ChinaAShareRules(TradingCalendar(Market.CN, (D1, D2)))
            },
        )


@pytest.mark.parametrize(
    "states",
    [
        [object()],
        [state(), state()],
        [
            CnSessionState(
                symbol="600000",
                session_date=D3,
                suspended=False,
                price_limit_state=CnPriceLimitState.NONE,
            )
        ],
    ],
)
def test_invalid_states_are_atomic_and_do_not_advance_timeline(states: list[object]) -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("atomic-state"), D1)

    with pytest.raises((TypeError, ValueError)):
        simulator.process_session(
            market=Market.CN,
            session_date=D2,
            bars=[bar()],
            session_states=states,  # type: ignore[arg-type]
        )

    assert simulator.pending_order_ids == ("atomic-state",)
    assert simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
    )[0].status is FillStatus.FILLED


@pytest.mark.parametrize(
    "lots",
    [
        {"": []},
        {1: []},
        {"account-1": [object()]},
    ],
)
def test_invalid_account_lots_are_atomic(lots: object) -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("atomic-lots"), D1)

    with pytest.raises((TypeError, ValueError)):
        simulator.process_session(
            market=Market.CN,
            session_date=D2,
            bars=[bar()],
            session_states=[state()],
            account_lots=lots,  # type: ignore[arg-type]
        )

    assert simulator.pending_order_ids == ("atomic-lots",)


def test_us_rejects_nonempty_cn_states_atomically() -> None:
    simulator = ExecutionSimulator({Market.US: calendar(Market.US)})
    simulator.submit(intent("us", market=Market.US), D1)

    with pytest.raises(ValueError, match="China"):
        simulator.process_session(
            market=Market.US,
            session_date=D2,
            bars=[bar(market=Market.US)],
            session_states=[state()],
        )

    assert simulator.pending_order_ids == ("us",)


def test_state_and_lot_generators_are_each_consumed_once_and_copied() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("generated", side=Side.SELL), D1)
    iterations = {"states": 0, "lots": 0}
    supplied_lots = [lot("600000", D1, "100")]

    def states_generator():
        iterations["states"] += 1
        yield state()

    def lots_generator():
        iterations["lots"] += 1
        yield from supplied_lots

    fill = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=states_generator(),
        account_lots={" account-1 ": lots_generator()},
    )[0]
    supplied_lots.clear()

    assert fill.status is FillStatus.FILLED
    assert iterations == {"states": 1, "lots": 1}


class DuplicateNormalizedAccounts(Mapping[str, list[AcquisitionLot]]):
    def __getitem__(self, key: str) -> list[AcquisitionLot]:
        return []

    def __iter__(self) -> Iterator[str]:
        return iter(("account-1", " account-1 "))

    def __len__(self) -> int:
        return 2

    def items(self):  # type: ignore[override]
        return (("account-1", []), (" account-1 ", []))


def test_duplicate_normalized_account_ids_are_rejected_atomically() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("duplicate-account"), D1)

    with pytest.raises(ValueError, match="duplicate"):
        simulator.process_session(
            market=Market.CN,
            session_date=D2,
            bars=[bar()],
            session_states=[state()],
            account_lots=DuplicateNormalizedAccounts(),
        )

    assert simulator.pending_order_ids == ("duplicate-account",)


def test_invalid_late_session_input_does_not_advance_timeline() -> None:
    simulator = ExecutionSimulator({Market.CN: calendar(Market.CN)})
    simulator.submit(intent("timeline"), D1)

    with pytest.raises(ValueError, match="session_date"):
        simulator.process_session(
            market=Market.CN,
            session_date=D3,
            bars=[],
            session_states=[state()],
        )

    fill = simulator.process_session(
        market=Market.CN,
        session_date=D2,
        bars=[bar()],
        session_states=[state()],
    )[0]
    assert fill.status is FillStatus.FILLED
