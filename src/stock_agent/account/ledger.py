from collections.abc import Mapping
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
from itertools import pairwise
from typing import Annotated, Any, Self

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

from stock_agent.domain import Market, PortfolioSnapshot, Position, Side

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


class _CopySafeMixin:
    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update:
            raise TypeError("immutable ledger models do not support copy updates")
        return super().copy(  # type: ignore[misc]
            include=include,
            exclude=exclude,
            update=update,
            deep=deep,
        )

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update:
            raise TypeError("immutable ledger models do not support copy updates")
        return super().model_copy(update=update, deep=deep)  # type: ignore[misc]


class PositionMark(_CopySafeMixin, BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    symbol: Symbol
    price: PositiveDecimal


class BookedFill(_CopySafeMixin, BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    fill_id: NonEmptyStr
    symbol: Symbol
    side: Side
    quantity: PositiveDecimal
    price: PositiveDecimal
    fees: NonNegativeDecimal

    @field_validator("side", mode="before")
    @classmethod
    def side_is_executable(cls, value: object) -> object:
        if type(value) is not Side or value not in (Side.BUY, Side.SELL):
            raise ValueError("side must be exactly Side.BUY or Side.SELL")
        return value


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


class _CompleteValuationEvent(_CopySafeMixin, _LedgerEvent):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    session_date: date
    marks: tuple[PositionMark, ...]

    @field_validator("session_date", mode="before")
    @classmethod
    def session_date_is_plain_date(cls, value: object) -> object:
        if type(value) is not date:
            raise ValueError("session_date must be a plain date")
        return value

    @field_validator("marks", mode="before")
    @classmethod
    def marks_are_exact_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("marks must be a tuple")
        if any(type(item) is not PositionMark for item in value):
            raise ValueError("marks must contain exact PositionMark values")
        return value

    @field_validator("marks")
    @classmethod
    def marks_are_canonical(cls, value: tuple[PositionMark, ...]) -> tuple[PositionMark, ...]:
        symbols = tuple(item.symbol for item in value)
        if symbols != tuple(sorted(symbols)):
            raise ValueError("marks must be sorted by symbol")
        if len(symbols) != len(set(symbols)):
            raise ValueError("mark symbols must be unique")
        return value


class OpenExecutionBatchBooked(_CompleteValuationEvent):
    fills: tuple[BookedFill, ...]

    @field_validator("fills", mode="before")
    @classmethod
    def fills_are_exact_nonempty_tuple(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("fills must be a tuple")
        if not value:
            raise ValueError("fills must not be empty")
        if any(type(item) is not BookedFill for item in value):
            raise ValueError("fills must contain exact BookedFill values")
        return value

    @field_validator("fills")
    @classmethod
    def fill_ids_are_unique(cls, value: tuple[BookedFill, ...]) -> tuple[BookedFill, ...]:
        fill_ids = tuple(item.fill_id for item in value)
        if len(fill_ids) != len(set(fill_ids)):
            raise ValueError("fill_id must be unique within a batch")
        return value


class PortfolioMarked(_CompleteValuationEvent):
    pass


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
    CashInitialized
    | BuyFilled
    | SellFilled
    | CashAdjusted
    | PositionMarked
    | OpenExecutionBatchBooked
    | PortfolioMarked
    | EventReversed
)

_LEDGER_EVENT_TYPES = (
    CashInitialized,
    BuyFilled,
    SellFilled,
    CashAdjusted,
    PositionMarked,
    OpenExecutionBatchBooked,
    PortfolioMarked,
    EventReversed,
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
        if not isinstance(event, _LEDGER_EVENT_TYPES):
            raise TypeError("event must be a LedgerEvent")
        self._commit_candidates((event,))

    def append_many(self, events: tuple[LedgerEvent, ...]) -> None:
        if type(events) is not tuple:
            raise TypeError("events must be a tuple")
        if not events:
            return
        if any(type(event) not in _LEDGER_EVENT_TYPES for event in events):
            raise TypeError("events must contain exact LedgerEvent values")

        self._commit_candidates(events)

    def _commit_candidates(self, events: tuple[LedgerEvent, ...]) -> None:
        for event in events:
            if type(event) in (OpenExecutionBatchBooked, PortfolioMarked):
                type(event).model_validate(event)
            if event.account_id != self._account_id or event.market is not self._market:
                raise ValueError("event account and market must match the ledger")

        candidate = (*self._events, *events)
        if not isinstance(candidate[0], CashInitialized):
            raise ValueError("the first event must be CashInitialized")
        if sum(isinstance(event, CashInitialized) for event in candidate) != 1:
            raise ValueError("CashInitialized can only occur once")

        event_ids = tuple(event.event_id for event in candidate)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event_id must be unique")
        for previous, current in pairwise(candidate):
            if _utc_instant(current.occurred_at) <= _utc_instant(previous.occurred_at):
                raise ValueError("occurred_at must be strictly increasing")

        fill_ids = tuple(
            fill.fill_id
            for event in candidate
            if isinstance(event, OpenExecutionBatchBooked)
            for fill in event.fills
        )
        if len(fill_ids) != len(set(fill_ids)):
            raise ValueError("fill_id must be globally unique")

        try:
            active_events = self._active_events(candidate)
            cash, realized_pnl, positions, lots, snapshot = self._replay(
                active_events, snapshot_as_of=events[-1].occurred_at
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
        booked_fill_ids: set[str] = set()

        for replayed in events:
            if isinstance(replayed, CashInitialized):
                cash = replayed.amount
                peak_nav = replayed.amount
            elif isinstance(replayed, BuyFilled):
                assert cash is not None
                cash = self._apply_buy(
                    holdings,
                    cash,
                    replayed.symbol,
                    replayed.session_date,
                    replayed.quantity,
                    replayed.price,
                    replayed.fees,
                    context,
                )
            elif isinstance(replayed, SellFilled):
                assert cash is not None
                cash, realized_pnl = self._apply_sell(
                    holdings,
                    cash,
                    realized_pnl,
                    replayed.symbol,
                    replayed.quantity,
                    replayed.price,
                    replayed.fees,
                    context,
                )
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
            elif isinstance(replayed, OpenExecutionBatchBooked):
                assert cash is not None
                for fill in replayed.fills:
                    if fill.fill_id in booked_fill_ids:
                        raise ValueError("fill_id must be globally unique")
                    booked_fill_ids.add(fill.fill_id)
                    if fill.side is Side.BUY:
                        cash = self._apply_buy(
                            holdings,
                            cash,
                            fill.symbol,
                            replayed.session_date,
                            fill.quantity,
                            fill.price,
                            fill.fees,
                            context,
                        )
                    else:
                        cash, realized_pnl = self._apply_sell(
                            holdings,
                            cash,
                            realized_pnl,
                            fill.symbol,
                            fill.quantity,
                            fill.price,
                            fill.fees,
                            context,
                        )
                self._apply_complete_marks(holdings, replayed.marks)
            elif isinstance(replayed, PortfolioMarked):
                self._apply_complete_marks(holdings, replayed.marks)
            else:
                raise ValueError("event replay is not implemented")

            positions = self._public_positions(holdings, context)
            market_value = Decimal(0)
            for position in positions:
                market_value = context.add(market_value, position.market_value)
            assert cash is not None
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

    @classmethod
    def _apply_buy(
        cls,
        holdings: dict[str, _Holding],
        cash: Decimal,
        symbol: str,
        session_date: date,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        context: Context,
    ) -> Decimal:
        cost = context.add(context.multiply(quantity, price), fees)
        new_cash = context.subtract(cash, cost)
        cls._require_finite(cost, new_cash)
        if cost > cash or new_cash < 0:
            raise ValueError("insufficient cash")
        holding = holdings.get(symbol)
        if holding is None:
            holdings[symbol] = _Holding([_Lot(session_date, quantity, cost)], price)
        else:
            holding.lots.append(_Lot(session_date, quantity, cost))
            holding.mark_price = price
        return new_cash

    @classmethod
    def _apply_sell(
        cls,
        holdings: dict[str, _Holding],
        cash: Decimal,
        realized_pnl: Decimal,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        context: Context,
    ) -> tuple[Decimal, Decimal]:
        holding = holdings.get(symbol)
        held_quantity = Decimal(0) if holding is None else cls._sum_lot_quantity(holding, context)
        if holding is None or quantity > held_quantity:
            raise ValueError("cannot sell more than the held quantity")
        proceeds = context.subtract(context.multiply(quantity, price), fees)
        cls._require_finite(proceeds)
        if proceeds < 0:
            raise ValueError("sell proceeds cannot be negative")
        allocated_cost = Decimal(0)
        remaining_to_sell = quantity
        while remaining_to_sell > 0:
            lot = holding.lots[0]
            take = min(remaining_to_sell, lot.quantity)
            if take == lot.quantity:
                lot_cost = lot.cost_basis
                holding.lots.pop(0)
            else:
                old_quantity = lot.quantity
                lot_cost = context.divide(context.multiply(lot.cost_basis, take), old_quantity)
                lot.quantity = context.subtract(old_quantity, take)
                lot.cost_basis = context.subtract(lot.cost_basis, lot_cost)
            allocated_cost = context.add(allocated_cost, lot_cost)
            remaining_to_sell = context.subtract(remaining_to_sell, take)
        new_cash = context.add(cash, proceeds)
        new_realized_pnl = context.add(
            realized_pnl, context.subtract(proceeds, allocated_cost)
        )
        cls._require_finite(allocated_cost, new_cash, new_realized_pnl)
        if not holding.lots:
            del holdings[symbol]
        else:
            holding.mark_price = price
        return new_cash, new_realized_pnl

    @staticmethod
    def _apply_complete_marks(
        holdings: dict[str, _Holding], marks: tuple[PositionMark, ...]
    ) -> None:
        mark_symbols = tuple(mark.symbol for mark in marks)
        if mark_symbols != tuple(sorted(holdings)):
            raise ValueError("marks must exactly match the complete held-symbol set")
        for mark in marks:
            holdings[mark.symbol].mark_price = mark.price

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
