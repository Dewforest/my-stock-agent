from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from stock_agent.domain import Bar, Market, Side
from stock_agent.execution.models import Fill, FillStatus, OrderIntent
from stock_agent.market import NoFutureSession, TradingCalendar


@dataclass(frozen=True)
class _PendingOrder:
    intent: OrderIntent
    eligible_session: date


class ExecutionSimulator:
    def __init__(
        self,
        calendars: Mapping[Market, TradingCalendar],
        transaction_cost_bps: Mapping[Market, Decimal] | None = None,
    ) -> None:
        copied_calendars = dict(calendars)
        for market, calendar in copied_calendars.items():
            if not isinstance(market, Market):
                raise TypeError("calendar keys must be Market values")
            if not isinstance(calendar, TradingCalendar):
                raise TypeError("calendar values must be TradingCalendar instances")
            if market is not calendar.market:
                raise ValueError("calendar key market must match calendar.market")
        self._calendars = copied_calendars

        self._costs = {Market.CN: Decimal("12"), Market.US: Decimal("5")}
        if transaction_cost_bps is not None:
            copied_costs = dict(transaction_cost_bps)
            for market, cost in copied_costs.items():
                if not isinstance(market, Market):
                    raise TypeError("transaction cost keys must be Market values")
                if not isinstance(cost, Decimal):
                    raise TypeError("transaction costs must be Decimal values")
                if not cost.is_finite() or cost < 0:
                    raise ValueError("transaction costs must be finite and nonnegative")
            self._costs.update(copied_costs)
        self._pending: list[_PendingOrder] = []
        self._used_order_ids: set[str] = set()

    @property
    def pending_order_ids(self) -> tuple[str, ...]:
        return tuple(order.intent.order_id for order in self._pending)

    def submit(self, intent: OrderIntent, decision_date: date) -> Fill:
        self._require_plain_date(decision_date, "decision_date")
        if intent.order_id in self._used_order_ids:
            return self._make_fill(
                intent, FillStatus.REJECTED, reason="duplicate order_id"
            )
        self._used_order_ids.add(intent.order_id)

        if intent.side not in (Side.BUY, Side.SELL):
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason=f"side {intent.side.value} is not executable",
            )

        calendar = self._calendars.get(intent.market)
        if calendar is None:
            return self._make_fill(
                intent, FillStatus.REJECTED, reason="missing market calendar"
            )
        if not calendar.is_session(decision_date):
            return self._make_fill(
                intent, FillStatus.REJECTED, reason="decision_date is not a session"
            )
        try:
            eligible_session = calendar.next_session(decision_date)
        except NoFutureSession:
            return self._make_fill(
                intent, FillStatus.REJECTED, reason="no future session"
            )
        self._pending.append(_PendingOrder(intent, eligible_session))
        return self._make_fill(intent, FillStatus.PENDING)

    def process_session(
        self,
        *,
        market: Market,
        session_date: date,
        bars: Iterable[Bar],
    ) -> tuple[Fill, ...]:
        self._require_plain_date(session_date, "session_date")
        calendar = self._calendars.get(market)
        if calendar is None:
            raise ValueError(f"missing calendar for market {market}")
        if not calendar.is_session(session_date):
            raise ValueError(f"{session_date} is not a session for market {market}")

        materialized_bars = tuple(bars)
        bars_by_symbol: dict[str, Bar] = {}
        for bar in materialized_bars:
            if bar.market is not market:
                raise ValueError("bar market does not match process market")
            if bar.session_date != session_date:
                raise ValueError("bar session_date does not match process session_date")
            if bar.symbol in bars_by_symbol:
                raise ValueError(f"duplicate bar symbol {bar.symbol}")
            bars_by_symbol[bar.symbol] = bar

        fills: list[Fill] = []
        remaining: list[_PendingOrder] = []
        for pending in self._pending:
            intent = pending.intent
            bar = bars_by_symbol.get(intent.symbol)
            if (
                intent.market is market
                and session_date >= pending.eligible_session
                and bar is not None
            ):
                fees = intent.quantity * bar.open * self._costs[market] / Decimal("10000")
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.FILLED,
                        filled_quantity=intent.quantity,
                        price=bar.open,
                        fees=fees,
                        session_date=session_date,
                    )
                )
            else:
                remaining.append(pending)
        self._pending = remaining
        return tuple(fills)

    @staticmethod
    def _require_plain_date(value: object, name: str) -> None:
        if type(value) is not date:
            raise TypeError(f"{name} must be a plain date")

    @staticmethod
    def _make_fill(
        intent: OrderIntent,
        status: FillStatus,
        *,
        filled_quantity: Decimal = Decimal("0"),
        price: Decimal | None = None,
        fees: Decimal = Decimal("0"),
        session_date: date | None = None,
        reason: str | None = None,
    ) -> Fill:
        return Fill(
            status=status,
            order_id=intent.order_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            market=intent.market,
            side=intent.side,
            requested_quantity=intent.quantity,
            filled_quantity=filled_quantity,
            price=price,
            fees=fees,
            session_date=session_date,
            reason=reason,
        )
