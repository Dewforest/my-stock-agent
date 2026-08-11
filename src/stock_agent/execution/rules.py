from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Protocol, runtime_checkable

from stock_agent.account import AcquisitionLot
from stock_agent.domain import Market, Side
from stock_agent.execution.cn_rules import (
    CnSessionState,
    _validate_decimal,
    apply_lot_size,
    can_sell_t1,
)
from stock_agent.execution.cn_rules import (
    execution_block_reason as cn_execution_block_reason,
)
from stock_agent.market import TradingCalendar

__all__ = ["ChinaAShareRules", "MarketRuleSet", "USCashEquityRules"]

_SUM_CONTEXT = Context(
    prec=128,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
)


def _canonical_symbol(value: object) -> str:
    if type(value) is not str:
        raise TypeError("symbol must be a string")
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("symbol must be non-blank")
    return symbol


def _validate_session_date(value: object) -> date:
    if type(value) is not date:
        raise TypeError("session_date must be a plain date")
    return value


def _validate_lots(value: object) -> tuple[AcquisitionLot, ...]:
    if type(value) is not tuple:
        raise TypeError("acquisition_lots must be a tuple")
    if any(not isinstance(lot, AcquisitionLot) for lot in value):
        raise TypeError("acquisition_lots must contain only AcquisitionLot values")
    return value


def _sum_quantities(lots: tuple[AcquisitionLot, ...]) -> Decimal:
    context = _SUM_CONTEXT.copy()
    total = Decimal(0)
    for lot in lots:
        total = context.add(total, lot.quantity)
    return total


@runtime_checkable
class MarketRuleSet(Protocol):
    @property
    def market(self) -> Market: ...

    @property
    def requires_session_state(self) -> bool: ...

    def normalize_quantity(self, *, side: Side, quantity: Decimal) -> Decimal: ...

    def execution_block_reason(
        self,
        *,
        side: Side,
        symbol: str,
        session_date: date,
        quantity: Decimal,
        state: CnSessionState | None,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> str | None: ...

    def sellable_quantity(
        self,
        *,
        symbol: str,
        session_date: date,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> Decimal: ...


@dataclass(frozen=True)
class ChinaAShareRules:
    calendar: TradingCalendar

    def __post_init__(self) -> None:
        if type(self.calendar) is not TradingCalendar:
            raise TypeError("calendar must be a TradingCalendar")
        if self.calendar.market is not Market.CN:
            raise ValueError("China A-share rules require a China market calendar")

    @property
    def market(self) -> Market:
        return Market.CN

    @property
    def requires_session_state(self) -> bool:
        return True

    def normalize_quantity(self, *, side: Side, quantity: Decimal) -> Decimal:
        return apply_lot_size(side=side, quantity=quantity)

    def execution_block_reason(
        self,
        *,
        side: Side,
        symbol: str,
        session_date: date,
        quantity: Decimal,
        state: CnSessionState | None,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> str | None:
        if side is not Side.BUY and side is not Side.SELL:
            raise ValueError("side must be BUY or SELL")
        symbol = _canonical_symbol(symbol)
        session_date = _validate_session_date(session_date)
        quantity = _validate_decimal(quantity, name="quantity")
        acquisition_lots = _validate_lots(acquisition_lots)
        if not self.calendar.is_session(session_date):
            raise ValueError("session_date must be an explicit calendar session")
        if state is None:
            raise ValueError("state is required for China A-share execution")
        if not isinstance(state, CnSessionState):
            raise TypeError("state must be a CnSessionState")
        if state.symbol != symbol or state.session_date != session_date:
            raise ValueError("state must match symbol and session_date")

        reason = cn_execution_block_reason(side=side, state=state)
        if reason is not None:
            return reason
        if side is Side.SELL and self.sellable_quantity(
            symbol=symbol,
            session_date=session_date,
            acquisition_lots=acquisition_lots,
        ) < quantity:
            return "sell quantity exceeds T+1 settled quantity"
        return None

    def sellable_quantity(
        self,
        *,
        symbol: str,
        session_date: date,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> Decimal:
        symbol = _canonical_symbol(symbol)
        session_date = _validate_session_date(session_date)
        acquisition_lots = _validate_lots(acquisition_lots)
        if not self.calendar.is_session(session_date):
            raise ValueError("session_date must be an explicit calendar session")
        sellable_lots = tuple(
            lot
            for lot in acquisition_lots
            if lot.symbol == symbol
            and can_sell_t1(
                acquired_session=lot.acquired_session,
                sell_session=session_date,
                calendar=self.calendar,
            )
        )
        return _sum_quantities(sellable_lots)


@dataclass(frozen=True)
class USCashEquityRules:
    @property
    def market(self) -> Market:
        return Market.US

    @property
    def requires_session_state(self) -> bool:
        return False

    def normalize_quantity(self, *, side: Side, quantity: Decimal) -> Decimal:
        quantity = _validate_decimal(quantity, name="quantity")
        if side is not Side.BUY and side is not Side.SELL:
            raise ValueError("side must be BUY or SELL")
        return quantity

    def execution_block_reason(
        self,
        *,
        side: Side,
        symbol: str,
        session_date: date,
        quantity: Decimal,
        state: CnSessionState | None,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> str | None:
        if side is not Side.BUY and side is not Side.SELL:
            raise ValueError("side must be BUY or SELL")
        symbol = _canonical_symbol(symbol)
        session_date = _validate_session_date(session_date)
        quantity = _validate_decimal(quantity, name="quantity")
        acquisition_lots = _validate_lots(acquisition_lots)
        if state is not None:
            raise ValueError("state must be None for US cash equity execution")
        if side is Side.SELL:
            available = self.sellable_quantity(
                symbol=symbol,
                session_date=session_date,
                acquisition_lots=acquisition_lots,
            )
            if available < quantity:
                return f"short sale blocked: held/available quantity is {available}"
        return None

    def sellable_quantity(
        self,
        *,
        symbol: str,
        session_date: date,
        acquisition_lots: tuple[AcquisitionLot, ...],
    ) -> Decimal:
        symbol = _canonical_symbol(symbol)
        _validate_session_date(session_date)
        acquisition_lots = _validate_lots(acquisition_lots)
        return _sum_quantities(tuple(lot for lot in acquisition_lots if lot.symbol == symbol))
