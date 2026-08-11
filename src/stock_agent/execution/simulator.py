from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException

from stock_agent.account import AcquisitionLot
from stock_agent.domain import Bar, Market, Side
from stock_agent.execution.cn_rules import CnSessionState
from stock_agent.execution.models import Fill, FillStatus, OrderIntent
from stock_agent.execution.rules import (
    ChinaAShareRules,
    MarketRuleSet,
    USCashEquityRules,
)
from stock_agent.market import NoFutureSession, TradingCalendar

_MAX_SUPPORTED_DECIMAL = Decimal("1E26")
_MAX_DECIMAL_PLACES = 12
_FEE_QUANTUM = Decimal("0.000000000001")
_UNSUPPORTED_NUMERIC_REASON = "unsupported numeric range/precision"


@dataclass(frozen=True)
class _PendingOrder:
    intent: OrderIntent
    effective_quantity: Decimal
    eligible_session: date


class ExecutionSimulator:
    def __init__(
        self,
        calendars: Mapping[Market, TradingCalendar],
        transaction_cost_bps: Mapping[Market, Decimal] | None = None,
        rule_sets: Mapping[Market, MarketRuleSet] | None = None,
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

        defaults: dict[Market, MarketRuleSet] = {}
        for market, calendar in copied_calendars.items():
            defaults[market] = (
                ChinaAShareRules(calendar) if market is Market.CN else USCashEquityRules()
            )
        if rule_sets is not None:
            copied_rules = dict(rule_sets)
            for market, rule in copied_rules.items():
                if not isinstance(market, Market):
                    raise TypeError("rule set keys must be Market values")
                if not isinstance(rule, MarketRuleSet):
                    raise TypeError("rule set values must implement MarketRuleSet")
                if market not in copied_calendars:
                    raise ValueError("rule set market must have a configured calendar")
                if rule.market is not market:
                    raise ValueError("rule set key must match rule.market and calendar")
                if (
                    isinstance(rule, ChinaAShareRules)
                    and rule.calendar != copied_calendars[market]
                ):
                    raise ValueError("China rule calendar must match configured calendar")
            defaults.update(copied_rules)
        self._rule_sets = defaults

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
                if not self._is_supported_decimal(cost):
                    raise ValueError(
                        f"transaction costs have {_UNSUPPORTED_NUMERIC_REASON}"
                    )
            self._costs.update(copied_costs)
        self._pending: list[_PendingOrder] = []
        self._used_order_ids: set[str] = set()
        self._processed_through: dict[Market, date] = {}

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

        if not self._is_supported_decimal(intent.quantity):
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason=f"quantity has {_UNSUPPORTED_NUMERIC_REASON}",
            )

        processed_through = self._processed_through.get(intent.market)
        if processed_through is not None and decision_date < processed_through:
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason="decision_date is backdated before processed timeline",
            )

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
        try:
            effective_quantity = self._rule_sets[intent.market].normalize_quantity(
                side=intent.side, quantity=intent.quantity
            )
        except Exception:
            return self._make_fill(
                intent, FillStatus.REJECTED, reason="market quantity rule rejected order"
            )
        if not isinstance(effective_quantity, Decimal) or not self._is_supported_decimal(
            effective_quantity
        ):
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason="market quantity rule returned an invalid quantity",
            )
        if effective_quantity <= 0:
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason="quantity is below the 100 share buy lot",
            )
        if effective_quantity > intent.quantity:
            return self._make_fill(
                intent,
                FillStatus.REJECTED,
                reason="market quantity rule may not increase requested quantity",
            )
        self._pending.append(_PendingOrder(intent, effective_quantity, eligible_session))
        return self._make_fill(
            intent, FillStatus.PENDING, requested_quantity=effective_quantity
        )

    def process_session(
        self,
        *,
        market: Market,
        session_date: date,
        bars: Iterable[Bar],
        session_states: Iterable[CnSessionState] = (),
        account_lots: Mapping[str, Iterable[AcquisitionLot]] | None = None,
        available_cash_by_account: Mapping[str, Decimal] | None = None,
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
            if not isinstance(bar, Bar):
                raise TypeError("bars must contain only Bar values")
            if bar.market is not market:
                raise ValueError("bar market does not match process market")
            if bar.session_date != session_date:
                raise ValueError("bar session_date does not match process session_date")
            if bar.symbol in bars_by_symbol:
                raise ValueError(f"duplicate bar symbol {bar.symbol}")
            bars_by_symbol[bar.symbol] = bar

        materialized_states = tuple(session_states)
        if market is not Market.CN and materialized_states:
            raise ValueError("session states are only valid for the China market")
        states_by_symbol: dict[str, CnSessionState] = {}
        for state in materialized_states:
            if not isinstance(state, CnSessionState):
                raise TypeError("session_states must contain only CnSessionState values")
            if state.session_date != session_date:
                raise ValueError("state session_date does not match process session_date")
            if state.symbol in states_by_symbol:
                raise ValueError(f"duplicate state symbol {state.symbol}")
            states_by_symbol[state.symbol] = state

        lots_by_account: dict[str, tuple[AcquisitionLot, ...]] = {}
        if account_lots is not None:
            if not isinstance(account_lots, Mapping):
                raise TypeError("account_lots must be a mapping")
            for account_id, lots in account_lots.items():
                if not isinstance(account_id, str):
                    raise TypeError("account_lots keys must be strings")
                normalized_account_id = account_id.strip()
                if not normalized_account_id:
                    raise ValueError("account_lots account id must be non-blank")
                if normalized_account_id in lots_by_account:
                    raise ValueError("duplicate normalized account_lots account id")
                materialized_lots = tuple(lots)
                if any(not isinstance(lot, AcquisitionLot) for lot in materialized_lots):
                    raise TypeError("account_lots values must contain only AcquisitionLot values")
                lots_by_account[normalized_account_id] = materialized_lots

        remaining_cash: dict[str, Decimal] | None = None
        if available_cash_by_account is not None:
            if not isinstance(available_cash_by_account, Mapping):
                raise TypeError("available_cash_by_account must be a mapping")
            remaining_cash = {}
            for account_id, cash in available_cash_by_account.items():
                if not isinstance(account_id, str):
                    raise TypeError("available_cash_by_account keys must be strings")
                normalized_account_id = account_id.strip()
                if not normalized_account_id:
                    raise ValueError(
                        "available_cash_by_account account id must be non-blank"
                    )
                if normalized_account_id in remaining_cash:
                    raise ValueError(
                        "duplicate normalized available_cash_by_account account id"
                    )
                if type(cash) is not Decimal:
                    raise TypeError(
                        "available_cash_by_account values must be exact Decimal values"
                    )
                if not cash.is_finite() or cash < 0:
                    raise ValueError(
                        "available_cash_by_account values must be finite and nonnegative"
                    )
                if not self._is_supported_decimal(cash):
                    raise ValueError(
                        "available_cash_by_account values have "
                        f"{_UNSUPPORTED_NUMERIC_REASON}"
                    )
                remaining_cash[normalized_account_id] = cash

            required_accounts = {
                pending.intent.account_id
                for pending in self._pending
                if pending.intent.market is market
                and session_date >= pending.eligible_session
            }
            missing_accounts = sorted(required_accounts - remaining_cash.keys())
            if missing_accounts:
                raise ValueError(
                    "available_cash_by_account missing eligible account "
                    f"{missing_accounts[0]}"
                )

        processed_through = self._processed_through.get(market)
        if processed_through is not None and session_date < processed_through:
            raise ValueError(
                f"cannot process {market.value} timeline backward from "
                f"{processed_through} to {session_date}"
            )

        fills: list[Fill] = []
        remaining: list[_PendingOrder] = []
        remaining_sellable: dict[tuple[str, str], Decimal] = {}
        rule = self._rule_sets[market]
        for pending in self._pending:
            intent = pending.intent
            bar = bars_by_symbol.get(intent.symbol)
            is_eligible = (
                intent.market is market and session_date >= pending.eligible_session
            )
            if not is_eligible or bar is None:
                remaining.append(pending)
                continue

            state = states_by_symbol.get(intent.symbol)
            try:
                requires_session_state = rule.requires_session_state
                if not isinstance(requires_session_state, bool):
                    raise TypeError
            except Exception:
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason="market session-state rule rejected order",
                    )
                )
                continue
            if requires_session_state and state is None:
                remaining.append(pending)
                continue
            lots = lots_by_account.get(intent.account_id, ())
            try:
                block_reason = rule.execution_block_reason(
                    side=intent.side,
                    symbol=intent.symbol,
                    session_date=session_date,
                    quantity=pending.effective_quantity,
                    state=state,
                    acquisition_lots=lots,
                )
                if block_reason is not None and (
                    not isinstance(block_reason, str) or not block_reason.strip()
                ):
                    raise TypeError
            except Exception:
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason="market execution rule rejected order",
                    )
                )
                continue
            sellable_key = (intent.account_id, intent.symbol)
            if intent.side is Side.SELL and block_reason is None:
                if sellable_key not in remaining_sellable:
                    try:
                        remaining_sellable[sellable_key] = rule.sellable_quantity(
                            symbol=intent.symbol,
                            session_date=session_date,
                            acquisition_lots=lots,
                        )
                        sellable = remaining_sellable[sellable_key]
                        if (
                            not isinstance(sellable, Decimal)
                            or not sellable.is_finite()
                            or sellable < 0
                        ):
                            raise ValueError
                    except Exception:
                        fills.append(
                            self._make_fill(
                                intent,
                                FillStatus.REJECTED,
                                requested_quantity=pending.effective_quantity,
                                reason="market sellable quantity rule rejected order",
                            )
                        )
                        continue
                if pending.effective_quantity > remaining_sellable[sellable_key]:
                    block_reason = (
                        "sell quantity exceeds remaining T+1 settled quantity"
                        if market is Market.CN
                        else (
                            "short sale blocked: sell quantity exceeds remaining "
                            "held/available quantity"
                        )
                    )
            if block_reason is not None:
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason=block_reason,
                    )
                )
                continue

            if not self._is_supported_decimal(bar.open):
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason=f"bar open has {_UNSUPPORTED_NUMERIC_REASON}",
                    )
                )
                continue

            try:
                fees = self._calculate_fees(
                    pending.effective_quantity, bar.open, self._costs[market]
                )
            except DecimalException:
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason="fee calculation failed for unsupported numeric result",
                    )
                )
                continue
            if not isinstance(fees, Decimal) or not self._is_supported_decimal(fees):
                fills.append(
                    self._make_fill(
                        intent,
                        FillStatus.REJECTED,
                        requested_quantity=pending.effective_quantity,
                        reason=f"fee has {_UNSUPPORTED_NUMERIC_REASON}",
                    )
                )
                continue

            if remaining_cash is not None:
                try:
                    cash_change = self._calculate_cash_change(
                        pending.effective_quantity,
                        bar.open,
                        fees,
                        intent.side,
                    )
                    if not self._is_supported_decimal(cash_change):
                        raise ValueError
                    if intent.side is Side.SELL:
                        if cash_change < 0:
                            fills.append(
                                self._make_fill(
                                    intent,
                                    FillStatus.REJECTED,
                                    requested_quantity=pending.effective_quantity,
                                    reason="fees exceed sell proceeds",
                                )
                            )
                            continue
                        new_cash = self._add_cash(
                            remaining_cash[intent.account_id], cash_change
                        )
                    else:
                        if cash_change > remaining_cash[intent.account_id]:
                            fills.append(
                                self._make_fill(
                                    intent,
                                    FillStatus.REJECTED,
                                    requested_quantity=pending.effective_quantity,
                                    reason="insufficient available cash",
                                )
                            )
                            continue
                        new_cash = self._subtract_quantity(
                            remaining_cash[intent.account_id], cash_change
                        )
                    if not self._is_supported_decimal(new_cash):
                        raise ValueError
                except (DecimalException, ValueError):
                    fills.append(
                        self._make_fill(
                            intent,
                            FillStatus.REJECTED,
                            requested_quantity=pending.effective_quantity,
                            reason=f"cash change has {_UNSUPPORTED_NUMERIC_REASON}",
                        )
                    )
                    continue
                remaining_cash[intent.account_id] = new_cash

            fills.append(
                self._make_fill(
                    intent,
                    FillStatus.FILLED,
                    requested_quantity=pending.effective_quantity,
                    filled_quantity=pending.effective_quantity,
                    price=bar.open,
                    fees=fees,
                    session_date=session_date,
                )
            )
            if intent.side is Side.SELL:
                remaining_sellable[sellable_key] = self._subtract_quantity(
                    remaining_sellable[sellable_key], pending.effective_quantity
                )
        self._pending = remaining
        if processed_through is None or session_date > processed_through:
            self._processed_through[market] = session_date
        return tuple(fills)

    @staticmethod
    def _require_plain_date(value: object, name: str) -> None:
        if type(value) is not date:
            raise TypeError(f"{name} must be a plain date")

    @staticmethod
    def _is_supported_decimal(value: Decimal) -> bool:
        if not value.is_finite() or value.copy_abs() >= _MAX_SUPPORTED_DECIMAL:
            return False
        decimal_tuple = value.as_tuple()
        exponent = decimal_tuple.exponent
        if not isinstance(exponent, int) or exponent >= -_MAX_DECIMAL_PLACES:
            return True
        excess_places = -_MAX_DECIMAL_PLACES - exponent
        return all(digit == 0 for digit in decimal_tuple.digits[-excess_places:])

    @staticmethod
    def _calculate_fees(quantity: Decimal, price: Decimal, bps: Decimal) -> Decimal:
        coefficient_digits = sum(
            len(value.as_tuple().digits) for value in (quantity, price, bps)
        )
        context = Context(
            prec=max(128, coefficient_digits + 16),
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
        )
        fee = context.divide(
            context.multiply(context.multiply(quantity, price), bps), Decimal("10000")
        )
        return context.quantize(fee, _FEE_QUANTUM)

    @staticmethod
    def _calculate_cash_change(
        quantity: Decimal, price: Decimal, fees: Decimal, side: Side
    ) -> Decimal:
        coefficient_digits = sum(
            len(value.as_tuple().digits) for value in (quantity, price, fees)
        )
        context = Context(
            prec=max(128, coefficient_digits + 16),
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
        )
        gross = context.multiply(quantity, price)
        return (
            context.subtract(gross, fees)
            if side is Side.SELL
            else context.add(gross, fees)
        )

    @staticmethod
    def _add_cash(left: Decimal, right: Decimal) -> Decimal:
        coefficient_digits = sum(len(value.as_tuple().digits) for value in (left, right))
        context = Context(
            prec=max(128, coefficient_digits + 4),
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
        )
        return context.add(left, right)

    @staticmethod
    def _subtract_quantity(left: Decimal, right: Decimal) -> Decimal:
        coefficient_digits = sum(len(value.as_tuple().digits) for value in (left, right))
        context = Context(prec=max(128, coefficient_digits + 4))
        return context.subtract(left, right)

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
        requested_quantity: Decimal | None = None,
    ) -> Fill:
        return Fill(
            status=status,
            order_id=intent.order_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            market=intent.market,
            side=intent.side,
            requested_quantity=(
                intent.quantity if requested_quantity is None else requested_quantity
            ),
            filled_quantity=filled_quantity,
            price=price,
            fees=fees,
            session_date=session_date,
            reason=reason,
        )
