from datetime import UTC, date, datetime
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_agent.domain import Market, PortfolioSnapshot, Position

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)
]


def _utc_instant(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _validate_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")

    decimal_tuple = value.as_tuple()
    digits = decimal_tuple.digits
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("value must be finite")

    trailing_zeroes = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    effective_scale = 0 if trailing_zeroes == len(digits) else max(0, -exponent - trailing_zeroes)
    if effective_scale > 12:
        raise ValueError("value must have at most 12 effective decimal places")
    if value.copy_abs() >= Decimal("1E26"):
        raise ValueError("absolute value must be less than 1E26")
    return value


SupportedDecimal = Annotated[Decimal, BeforeValidator(_validate_decimal)]
PositiveDecimal = Annotated[SupportedDecimal, Field(gt=0)]
NonNegativeDecimal = Annotated[SupportedDecimal, Field(ge=0)]


def _validate_finite_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    return value


LotPositiveDecimal = Annotated[
    Decimal, BeforeValidator(_validate_finite_decimal), Field(gt=0)
]


class AcquisitionLot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: Symbol
    acquired_session: date
    quantity: LotPositiveDecimal
    cost_basis: LotPositiveDecimal

    @field_validator("acquired_session", mode="before")
    @classmethod
    def acquired_session_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("acquired_session must be a plain date")
        return value


class _LedgerEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: NonEmptyStr
    account_id: NonEmptyStr
    market: Market
    occurred_at: AwareDatetime


class CashInitialized(_LedgerEvent):
    amount: NonNegativeDecimal


class _FillEvent(_LedgerEvent):
    symbol: Symbol
    session_date: date
    quantity: PositiveDecimal
    price: PositiveDecimal
    fees: NonNegativeDecimal

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value


class BuyFilled(_FillEvent):
    pass


class SellFilled(_FillEvent):
    pass


class PositionMarked(_LedgerEvent):
    symbol: Symbol
    session_date: date
    price: PositiveDecimal

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value


class CashAdjusted(_LedgerEvent):
    amount: SupportedDecimal
    reason: NonEmptyStr

    @field_validator("amount")
    @classmethod
    def amount_is_non_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value


class EventReversed(_LedgerEvent):
    target_event_id: NonEmptyStr
    reason: NonEmptyStr

    @model_validator(mode="after")
    def target_is_not_self(self) -> Self:
        if self.target_event_id == self.event_id:
            raise ValueError("an event cannot reverse itself")
        return self


LedgerEvent = (
    CashInitialized | BuyFilled | SellFilled | CashAdjusted | PositionMarked | EventReversed
)

_ARITHMETIC_CONTEXT = Context(
    prec=128,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
)


class _Lot:
    def __init__(self, acquired_session: date, quantity: Decimal, cost_basis: Decimal) -> None:
        self.acquired_session = acquired_session
        self.quantity = quantity
        self.cost_basis = cost_basis


class _Holding:
    def __init__(self, lots: list[_Lot], mark_price: Decimal) -> None:
        self.lots = lots
        self.mark_price = mark_price


class PortfolioLedger:
    def __init__(self, account_id: str, market: Market) -> None:
        if not isinstance(account_id, str):
            raise TypeError("account_id must be a string")
        account_id = account_id.strip()
        if not account_id:
            raise ValueError("account_id must not be blank")
        if not isinstance(market, Market):
            raise TypeError("market must be a Market")

        self._account_id = account_id
        self._market = market
        self._events: tuple[LedgerEvent, ...] = ()
        self._cash: Decimal | None = None
        self._realized_pnl: Decimal | None = None
        self._positions: tuple[Position, ...] | None = None
        self._lots: tuple[AcquisitionLot, ...] | None = None
        self._snapshot: PortfolioSnapshot | None = None

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def market(self) -> Market:
        return self._market

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return self._events

    @property
    def cash(self) -> Decimal:
        if self._cash is None:
            raise RuntimeError("cash has not been initialized")
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        if self._realized_pnl is None:
            raise RuntimeError("cash has not been initialized")
        return self._realized_pnl

    @property
    def positions(self) -> tuple[Position, ...]:
        if self._positions is None:
            raise RuntimeError("cash has not been initialized")
        return self._positions

    @property
    def lots(self) -> tuple[AcquisitionLot, ...]:
        if self._lots is None:
            raise RuntimeError("cash has not been initialized")
        return self._lots

    def snapshot(self, as_of: datetime | None = None) -> PortfolioSnapshot:
        if as_of is None:
            if self._snapshot is None:
                raise RuntimeError("cash has not been initialized")
            return self._snapshot
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

        as_of_instant = _utc_instant(as_of)
        prefix = tuple(
            event
            for event in self._events
            if _utc_instant(event.occurred_at) <= as_of_instant
        )
        if not prefix:
            raise RuntimeError("cash has not been initialized")
        try:
            active_events = self._active_events(prefix)
            return self._replay(active_events, snapshot_as_of=as_of)[4]
        except DecimalException as error:
            raise ValueError("decimal arithmetic failed") from error

    def append(self, event: LedgerEvent) -> None:
        if not isinstance(
            event,
            (CashInitialized, BuyFilled, SellFilled, CashAdjusted, PositionMarked, EventReversed),
        ):
            raise TypeError("event must be a LedgerEvent")
        if event.account_id != self._account_id or event.market is not self._market:
            raise ValueError("event account and market must match the ledger")
        if not self._events and not isinstance(event, CashInitialized):
            raise ValueError("the first event must be CashInitialized")
        if any(existing.event_id == event.event_id for existing in self._events):
            raise ValueError("event_id must be unique")
        if self._events and _utc_instant(event.occurred_at) <= _utc_instant(
            self._events[-1].occurred_at
        ):
            raise ValueError("occurred_at must be strictly increasing")
        if isinstance(event, CashInitialized) and self._events:
            raise ValueError("CashInitialized can only occur once")

        candidate = (*self._events, event)
        try:
            active_events = self._active_events(candidate)
            cash, realized_pnl, positions, lots, snapshot = self._replay(
                active_events, snapshot_as_of=event.occurred_at
            )
        except DecimalException as error:
            raise ValueError("decimal arithmetic failed") from error

        self._events = candidate
        self._cash = cash
        self._realized_pnl = realized_pnl
        self._positions = positions
        self._lots = lots
        self._snapshot = snapshot

    def _replay(
        self,
        events: tuple[LedgerEvent, ...],
        *,
        snapshot_as_of: datetime,
    ) -> tuple[
        Decimal,
        Decimal,
        tuple[Position, ...],
        tuple[AcquisitionLot, ...],
        PortfolioSnapshot,
    ]:
        context = _ARITHMETIC_CONTEXT.copy()
        cash: Decimal | None = None
        realized_pnl = Decimal(0)
        peak_nav: Decimal | None = None
        holdings: dict[str, _Holding] = {}
        positions: tuple[Position, ...] = ()
        snapshot: PortfolioSnapshot | None = None

        for replayed in events:
            if isinstance(replayed, CashInitialized):
                cash = replayed.amount
                peak_nav = replayed.amount
            elif isinstance(replayed, BuyFilled):
                assert cash is not None
                cost = context.add(
                    context.multiply(replayed.quantity, replayed.price), replayed.fees
                )
                new_cash = context.subtract(cash, cost)
                self._require_finite(cost, new_cash)
                if cost > cash or new_cash < 0:
                    raise ValueError("insufficient cash")
                holding = holdings.get(replayed.symbol)
                if holding is None:
                    holdings[replayed.symbol] = _Holding(
                        [_Lot(replayed.session_date, replayed.quantity, cost)], replayed.price
                    )
                else:
                    holding.lots.append(_Lot(replayed.session_date, replayed.quantity, cost))
                    holding.mark_price = replayed.price
                cash = new_cash
            elif isinstance(replayed, SellFilled):
                assert cash is not None
                holding = holdings.get(replayed.symbol)
                held_quantity = (
                    Decimal(0)
                    if holding is None
                    else self._sum_lot_quantity(holding, context)
                )
                if holding is None or replayed.quantity > held_quantity:
                    raise ValueError("cannot sell more than the held quantity")
                proceeds = context.subtract(
                    context.multiply(replayed.quantity, replayed.price), replayed.fees
                )
                self._require_finite(proceeds)
                if proceeds < 0:
                    raise ValueError("sell proceeds cannot be negative")
                allocated_cost = Decimal(0)
                remaining_to_sell = replayed.quantity
                while remaining_to_sell > 0:
                    lot = holding.lots[0]
                    take = min(remaining_to_sell, lot.quantity)
                    if take == lot.quantity:
                        lot_cost = lot.cost_basis
                        holding.lots.pop(0)
                    else:
                        old_quantity = lot.quantity
                        lot_cost = context.divide(
                            context.multiply(lot.cost_basis, take), old_quantity
                        )
                        lot.quantity = context.subtract(old_quantity, take)
                        lot.cost_basis = context.subtract(lot.cost_basis, lot_cost)
                    allocated_cost = context.add(allocated_cost, lot_cost)
                    remaining_to_sell = context.subtract(remaining_to_sell, take)
                cash = context.add(cash, proceeds)
                realized_pnl = context.add(
                    realized_pnl, context.subtract(proceeds, allocated_cost)
                )
                self._require_finite(allocated_cost, cash, realized_pnl)
                if not holding.lots:
                    del holdings[replayed.symbol]
                else:
                    holding.mark_price = replayed.price
            elif isinstance(replayed, CashAdjusted):
                assert cash is not None
                new_cash = context.add(cash, replayed.amount)
                self._require_finite(new_cash)
                if new_cash < 0:
                    raise ValueError("cash adjustment cannot make cash negative")
                cash = new_cash
            elif isinstance(replayed, PositionMarked):
                holding = holdings.get(replayed.symbol)
                if holding is None:
                    raise ValueError("cannot mark an unknown position")
                holding.mark_price = replayed.price
            else:
                raise ValueError("event replay is not implemented")

            positions = self._public_positions(holdings, context)
            market_value = Decimal(0)
            for position in positions:
                market_value = context.add(market_value, position.market_value)
            nav = context.add(cash, market_value)
            self._require_finite(cash, realized_pnl, market_value, nav)
            assert peak_nav is not None
            peak_nav = max(peak_nav, nav)
            with localcontext(context):
                snapshot = PortfolioSnapshot(
                    account_id=self._account_id,
                    market=self._market,
                    cash=cash,
                    nav=nav,
                    peak_nav=peak_nav,
                    positions=positions,
                    as_of=snapshot_as_of if replayed is events[-1] else replayed.occurred_at,
                )

        assert cash is not None and snapshot is not None
        return cash, realized_pnl, positions, self._public_lots(holdings), snapshot

    @staticmethod
    def _active_events(events: tuple[LedgerEvent, ...]) -> tuple[LedgerEvent, ...]:
        by_id: dict[str, LedgerEvent] = {}
        reversed_ids: set[str] = set()
        ordinary: list[LedgerEvent] = []
        for event in events:
            if isinstance(event, EventReversed):
                target = by_id.get(event.target_event_id)
                if target is None:
                    raise ValueError("reversal target must be an earlier event")
                if isinstance(target, CashInitialized):
                    raise ValueError("CashInitialized cannot be reversed")
                if isinstance(target, EventReversed):
                    raise ValueError("EventReversed cannot be reversed")
                if event.target_event_id in reversed_ids:
                    raise ValueError("an event can only be reversed once")
                reversed_ids.add(event.target_event_id)
            else:
                ordinary.append(event)
            by_id[event.event_id] = event
        return tuple(event for event in ordinary if event.event_id not in reversed_ids)

    @staticmethod
    def _require_finite(*values: Decimal) -> None:
        if not all(value.is_finite() for value in values):
            raise ValueError("decimal arithmetic must remain finite")

    @staticmethod
    def _public_positions(
        holdings: dict[str, _Holding], context: Context
    ) -> tuple[Position, ...]:
        result = []
        for symbol in sorted(holdings):
            holding = holdings[symbol]
            quantity = PortfolioLedger._sum_lot_quantity(holding, context)
            cost_basis = Decimal(0)
            for lot in holding.lots:
                cost_basis = context.add(cost_basis, lot.cost_basis)
            average_cost = context.divide(cost_basis, quantity)
            market_value = context.multiply(quantity, holding.mark_price)
            PortfolioLedger._require_finite(average_cost, market_value)
            result.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_cost=average_cost,
                    market_value=market_value,
                )
            )
        return tuple(result)

    @staticmethod
    def _sum_lot_quantity(holding: _Holding, context: Context) -> Decimal:
        quantity = Decimal(0)
        for lot in holding.lots:
            quantity = context.add(quantity, lot.quantity)
        PortfolioLedger._require_finite(quantity)
        return quantity

    @staticmethod
    def _public_lots(holdings: dict[str, _Holding]) -> tuple[AcquisitionLot, ...]:
        return tuple(
            AcquisitionLot(
                symbol=symbol,
                acquired_session=lot.acquired_session,
                quantity=lot.quantity,
                cost_basis=lot.cost_basis,
            )
            for symbol in sorted(holdings)
            for lot in holdings[symbol].lots
        )
