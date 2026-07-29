from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)
from typing import TypeVar

from pydantic import BaseModel

from stock_agent.account import (
    BookedFill,
    CashInitialized,
    OpenExecutionBatchBooked,
    PortfolioLedger,
    PortfolioMarked,
    PositionMark,
)
from stock_agent.audit import canonical_datetime, canonical_decimal, tagged_sha256
from stock_agent.backtest.models import (
    BacktestInputManifest,
    BacktestResult,
    BacktestSession,
    BacktestSpec,
    OrderPlan,
    OrderPlanStatus,
    SessionResult,
)
from stock_agent.backtest.planning import plan_orders, record_submission
from stock_agent.data import PointInTimeStore, SelectedBarRevision
from stock_agent.domain import Bar, PortfolioSnapshot, Side, StrategyIntent
from stock_agent.execution import ExecutionSimulator, Fill, FillStatus
from stock_agent.market import TradingCalendar
from stock_agent.risk import RiskContext, RiskEngine
from stock_agent.strategies import MarketSnapshot, Strategy, StrategyContext

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_PIT_POLICY = "business-available-at/v1"


def _arithmetic_context_for(*values: Decimal) -> Context:
    if any(not value.is_finite() for value in values):
        raise ValueError("runner arithmetic values must be finite")
    tuples = tuple(value.as_tuple() for value in values)
    nonzero_exponents: list[int] = []
    for value, value_tuple in zip(values, tuples, strict=True):
        if not isinstance(value_tuple.exponent, int):
            raise ValueError("runner arithmetic values must be finite")
        if value:
            nonzero_exponents.append(value_tuple.exponent)
    highest_adjusted = max((value.adjusted() for value in values if value), default=0)
    lowest_exponent = min(nonzero_exponents, default=0)
    span = highest_adjusted - lowest_exponent + 1
    coefficient_digits = sum(max(1, len(value_tuple.digits)) for value_tuple in tuples)
    context = Context(
        prec=min(MAX_PREC, max(128, span + 32, coefficient_digits + 32)),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    for signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
        context.traps[signal] = False
    return context


def _model_values(model: BaseModel) -> dict[str, object]:
    try:
        return {name: getattr(model, name) for name in model.__class__.model_fields}
    except AttributeError as error:
        raise ValueError("nested model is missing a required field") from error


def _rebuild_exact(value: object, expected: type[_ModelT], name: str) -> _ModelT:
    if type(value) is not expected:
        raise TypeError(f"{name} must be exactly {expected.__name__}")
    return expected.model_validate(_model_values(value), strict=True)


def _canonical_decimal_value(value: Decimal) -> Decimal:
    return Decimal(canonical_decimal(value))


def _canonical_bar(bar: Bar) -> Bar:
    return Bar.model_validate(
        {
            **_model_values(bar),
            "open": _canonical_decimal_value(bar.open),
            "high": _canonical_decimal_value(bar.high),
            "low": _canonical_decimal_value(bar.low),
            "close": _canonical_decimal_value(bar.close),
            "volume": _canonical_decimal_value(bar.volume),
            "available_at": bar.available_at.astimezone(UTC),
        },
        strict=True,
    )


def _canonical_spec(spec: BacktestSpec) -> BacktestSpec:
    sessions = tuple(
        BacktestSession.model_validate(
            {
                **_model_values(session),
                "open_at": session.open_at.astimezone(UTC),
                "close_at": session.close_at.astimezone(UTC),
                "open_bars": tuple(_canonical_bar(bar) for bar in session.open_bars),
            },
            strict=True,
        )
        for session in spec.sessions
    )
    return BacktestSpec.model_validate(
        {
            **_model_values(spec),
            "initial_cash": _canonical_decimal_value(spec.initial_cash),
            "sessions": sessions,
        },
        strict=True,
    )


def _bar_payload(bar: Bar) -> list[object]:
    return [
        bar.symbol,
        bar.market.value,
        bar.session_date.isoformat(),
        canonical_decimal(bar.open),
        canonical_decimal(bar.high),
        canonical_decimal(bar.low),
        canonical_decimal(bar.close),
        canonical_decimal(bar.volume),
        canonical_datetime(bar.available_at),
    ]


def _session_payload(session: BacktestSession) -> list[object]:
    return [
        session.session_date.isoformat(),
        canonical_datetime(session.open_at),
        canonical_datetime(session.close_at),
        [_bar_payload(bar) for bar in session.open_bars],
        [
            [
                state.symbol,
                state.session_date.isoformat(),
                state.suspended,
                state.price_limit_state.value,
            ]
            for state in session.cn_session_states
        ],
    ]


def _manifest_payload(manifest: BacktestInputManifest) -> tuple[object, ...]:
    return (
        manifest.account_id,
        manifest.market.value,
        canonical_decimal(manifest.initial_cash),
        [
            [item.symbol, item.market.value, item.currency.value, item.sector]
            for item in manifest.instruments
        ],
        [item.isoformat() for item in manifest.calendar_sessions],
        [_session_payload(item) for item in manifest.sessions],
        manifest.strategy_id,
        manifest.strategy_config_version,
        canonical_decimal(manifest.transaction_cost_bps),
        manifest.pit_knowledge_policy,
    )


def _revision_payload(revision: SelectedBarRevision) -> list[object]:
    return [
        _bar_payload(revision.bar),
        canonical_datetime(revision.ingested_at),
        revision.source,
        revision.source_record_id,
    ]


def _event_id(run_id: str, *fields: object) -> str:
    return tagged_sha256("ledger-event", (run_id, *fields))


class ChronologicalBacktestRunner:
    def __init__(
        self,
        *,
        store: PointInTimeStore,
        calendar: TradingCalendar,
        strategy: Strategy,
        risk_engine: RiskEngine | None = None,
        transaction_cost_bps: Decimal = Decimal("0"),
    ) -> None:
        if type(store) is not PointInTimeStore:
            raise TypeError("store must be exactly PointInTimeStore")
        if type(calendar) is not TradingCalendar:
            raise TypeError("calendar must be exactly TradingCalendar")
        if not isinstance(strategy, Strategy):
            raise TypeError("strategy must implement Strategy")
        if type(strategy.strategy_id) is not str or not strategy.strategy_id.strip():
            raise ValueError("strategy_id must be nonblank")
        if type(strategy.config_version) is not str or not strategy.config_version.strip():
            raise ValueError("strategy config_version must be nonblank")
        if risk_engine is not None and not isinstance(risk_engine, RiskEngine):
            raise TypeError("risk_engine must be a RiskEngine or None")
        if type(transaction_cost_bps) is not Decimal:
            raise TypeError("transaction_cost_bps must be exactly Decimal")
        if not transaction_cost_bps.is_finite() or transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps must be finite and nonnegative")
        self._store = store
        self._calendar = calendar
        self._strategy = strategy
        self._risk_engine = RiskEngine() if risk_engine is None else risk_engine
        self._transaction_cost_bps = _canonical_decimal_value(transaction_cost_bps)
        self._registry: dict[str, tuple[str, BacktestResult]] = {}

    def run(self, spec: BacktestSpec) -> BacktestResult:
        clean_spec = _canonical_spec(_rebuild_exact(spec, BacktestSpec, "spec"))
        arithmetic_values = [self._transaction_cost_bps, clean_spec.initial_cash]
        arithmetic_values.extend(
            value
            for session in clean_spec.sessions
            for bar in session.open_bars
            for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
        )
        with localcontext(_arithmetic_context_for(*arithmetic_values)):
            return self._run_isolated(clean_spec)

    def _run_isolated(self, clean_spec: BacktestSpec) -> BacktestResult:
        calendar_slice = self._validate_spec(clean_spec)
        manifest = BacktestInputManifest(
            account_id=clean_spec.account_id,
            market=clean_spec.market,
            initial_cash=clean_spec.initial_cash,
            instruments=clean_spec.instruments,
            calendar_sessions=calendar_slice,
            sessions=clean_spec.sessions,
            strategy_id=self._strategy.strategy_id.strip(),
            strategy_config_version=clean_spec.strategy_config_version,
            transaction_cost_bps=self._transaction_cost_bps,
            pit_knowledge_policy=_PIT_POLICY,
        )
        spec_fingerprint = tagged_sha256("backtest-spec", _manifest_payload(manifest))

        cached = self._registry.get(clean_spec.run_id)
        if cached is not None:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint != spec_fingerprint:
                raise ValueError(
                    "run_id conflicts with a different backtest specification"
                )
            return cached_result

        ledger = PortfolioLedger(clean_spec.account_id, clean_spec.market)
        simulator = ExecutionSimulator(
            {clean_spec.market: self._calendar},
            transaction_cost_bps={clean_spec.market: self._transaction_cost_bps},
        )
        first = clean_spec.sessions[0]
        ledger.append(
            CashInitialized(
                event_id=_event_id(
                    clean_spec.run_id,
                    clean_spec.account_id,
                    clean_spec.market.value,
                    "init",
                ),
                account_id=clean_spec.account_id,
                market=clean_spec.market,
                occurred_at=first.open_at - timedelta(microseconds=1),
                amount=clean_spec.initial_cash,
            )
        )

        session_results: list[SessionResult] = []
        prior_pending: tuple[Fill, ...] = ()
        for index, session in enumerate(clean_spec.sessions):
            day_start_cash = ledger.cash
            pre_open_symbols = {item.symbol for item in ledger.positions}
            execution_results = simulator.process_session(
                market=clean_spec.market,
                session_date=session.session_date,
                bars=session.open_bars,
                session_states=session.cn_session_states,
                account_lots={clean_spec.account_id: ledger.lots},
                available_cash_by_account={clean_spec.account_id: ledger.cash},
            )
            self._require_prior_orders_terminal(prior_pending, execution_results, simulator)
            filled = tuple(
                item for item in execution_results if item.status is FillStatus.FILLED
            )
            if filled:
                self._book_open_batch(
                    spec=clean_spec,
                    session=session,
                    ledger=ledger,
                    filled=filled,
                )
            new_position_notional = self._new_position_notional(
                filled, pre_open_symbols
            )
            selected_revisions = self._resolve_revisions(
                spec=clean_spec,
                through=index,
                as_of=session.close_at,
            )
            market_snapshot = MarketSnapshot(
                as_of=session.close_at,
                market=clean_spec.market,
                bars=tuple(item.bar for item in selected_revisions),
            )
            current_closes = {
                item.bar.symbol: item.bar.close
                for item in selected_revisions
                if item.bar.session_date == session.session_date
            }
            held_symbols = tuple(item.symbol for item in ledger.positions)
            missing_current = tuple(
                symbol for symbol in held_symbols if symbol not in current_closes
            )
            if missing_current:
                raise ValueError(
                    f"missing current close for symbols: {', '.join(missing_current)}"
                )
            ledger.append(
                PortfolioMarked(
                    event_id=_event_id(
                        clean_spec.run_id,
                        session.session_date.isoformat(),
                        "portfolio-mark",
                    ),
                    account_id=clean_spec.account_id,
                    market=clean_spec.market,
                    occurred_at=session.close_at,
                    session_date=session.session_date,
                    marks=tuple(
                        PositionMark(symbol=symbol, price=current_closes[symbol])
                        for symbol in held_symbols
                    ),
                )
            )
            portfolio = ledger.snapshot()

            if index == len(clean_spec.sessions) - 1:
                intents: tuple[StrategyIntent, ...] = ()
                risk_decisions = ()
                portfolio_reduction = None
                order_plans: tuple[OrderPlan, ...] = ()
                submission_results: tuple[Fill, ...] = ()
            else:
                intents = self._evaluate_strategy(
                    clean_spec, market_snapshot, portfolio
                )
                risk_context = RiskContext(
                    portfolio=portfolio,
                    instruments=clean_spec.instruments,
                    day_start_available_cash=day_start_cash,
                    new_position_notional_committed_today=new_position_notional,
                )
                portfolio_reduction = self._risk_engine.assess_portfolio(risk_context)
                risk_decisions = self._risk_engine.evaluate_many(intents, risk_context)
                if any(
                    item.risk_reduction != portfolio_reduction
                    for item in risk_decisions
                ):
                    raise ValueError(
                        "risk decision reduction must match portfolio assessment"
                    )
                planned = plan_orders(
                    run_id=clean_spec.run_id,
                    decision_session=session.session_date,
                    strategy_id=self._strategy.strategy_id.strip(),
                    market_snapshot=market_snapshot,
                    portfolio=portfolio,
                    risk_decisions=risk_decisions,
                    portfolio_reduction=portfolio_reduction,
                )
                completed_plans: list[OrderPlan] = []
                submissions: list[Fill] = []
                for plan in planned:
                    if plan.status is OrderPlanStatus.READY:
                        assert plan.order is not None
                        submission = simulator.submit(
                            plan.order, decision_date=session.session_date
                        )
                        completed_plans.append(record_submission(plan, submission))
                        submissions.append(submission)
                    else:
                        completed_plans.append(plan)
                order_plans = tuple(completed_plans)
                submission_results = tuple(submissions)
            prior_pending = tuple(
                item
                for item in submission_results
                if item.status is FillStatus.PENDING
            )
            session_results.append(
                SessionResult(
                    session_date=session.session_date,
                    execution_results=execution_results,
                    selected_revisions=selected_revisions,
                    market_snapshot=market_snapshot,
                    portfolio_snapshot=portfolio,
                    intents=intents,
                    risk_decisions=risk_decisions,
                    portfolio_reduction=portfolio_reduction,
                    order_plans=order_plans,
                    submission_results=submission_results,
                )
            )

        revisions_payload = tuple(
            _revision_payload(revision)
            for result in session_results
            for revision in result.selected_revisions
        )
        resolved_fingerprint = tagged_sha256("resolved-data", revisions_payload)
        final_snapshot = session_results[-1].portfolio_snapshot
        result = BacktestResult(
            run_id=clean_spec.run_id,
            manifest=manifest,
            spec_fingerprint=spec_fingerprint,
            resolved_data_fingerprint=resolved_fingerprint,
            sessions=tuple(session_results),
            ledger_events=ledger.events,
            final_lots=ledger.lots,
            final_snapshot=final_snapshot,
            realized_pnl=ledger.realized_pnl,
        )
        self._registry[clean_spec.run_id] = (spec_fingerprint, result)
        return result

    def _validate_spec(self, spec: BacktestSpec) -> tuple[date, ...]:
        if spec.market is not self._calendar.market:
            raise ValueError("spec market must match runner calendar")
        if spec.strategy_config_version != self._strategy.config_version:
            raise ValueError("strategy config version must match exactly")
        dates = tuple(item.session_date for item in spec.sessions)
        try:
            start = self._calendar.sessions.index(dates[0])
        except ValueError as error:
            raise ValueError("spec dates must be a contiguous calendar slice") from error
        expected = self._calendar.sessions[start : start + len(dates)]
        if dates != expected:
            raise ValueError("spec dates must be a contiguous calendar slice")
        return expected

    @staticmethod
    def _require_prior_orders_terminal(
        prior: tuple[Fill, ...],
        results: tuple[Fill, ...],
        simulator: ExecutionSimulator,
    ) -> None:
        expected_ids = tuple(item.order_id for item in prior)
        actual_ids = tuple(item.order_id for item in results)
        if expected_ids != actual_ids or any(
            item.status not in (FillStatus.FILLED, FillStatus.REJECTED)
            for item in results
        ):
            raise ValueError("all prior pending orders must terminate at the next open")
        if any(order_id in simulator.pending_order_ids for order_id in expected_ids):
            raise ValueError("all prior pending orders must terminate at the next open")

    @staticmethod
    def _new_position_notional(
        fills: tuple[Fill, ...], pre_open_symbols: set[str]
    ) -> Decimal:
        total = Decimal(0)
        for fill in fills:
            if fill.side is Side.BUY and fill.symbol not in pre_open_symbols:
                assert fill.price is not None
                product_context = _arithmetic_context_for(fill.filled_quantity, fill.price)
                notional = product_context.multiply(fill.filled_quantity, fill.price)
                total = _arithmetic_context_for(total, notional).add(total, notional)
        return total

    @staticmethod
    def _book_open_batch(
        *,
        spec: BacktestSpec,
        session: BacktestSession,
        ledger: PortfolioLedger,
        filled: tuple[Fill, ...],
    ) -> None:
        quantities: dict[str, Decimal] = {}
        for lot in ledger.lots:
            current = quantities.get(lot.symbol, Decimal(0))
            quantities[lot.symbol] = _arithmetic_context_for(current, lot.quantity).add(
                current, lot.quantity
            )
        booked: list[BookedFill] = []
        for fill in filled:
            assert fill.price is not None
            quantity = fill.filled_quantity
            current = quantities.get(fill.symbol, Decimal(0))
            arithmetic = _arithmetic_context_for(current, quantity)
            quantities[fill.symbol] = (
                arithmetic.add(current, quantity)
                if fill.side is Side.BUY
                else arithmetic.subtract(current, quantity)
            )
            booked.append(
                BookedFill(
                    fill_id=tagged_sha256(
                        "booked-fill",
                        (
                            spec.run_id,
                            session.session_date.isoformat(),
                            fill.order_id,
                            "booked-fill",
                        ),
                    ),
                    symbol=fill.symbol,
                    side=fill.side,
                    quantity=quantity,
                    price=fill.price,
                    fees=fill.fees,
                )
            )
        open_prices = {item.symbol: item.open for item in session.open_bars}
        held_symbols = tuple(
            sorted(symbol for symbol, quantity in quantities.items() if quantity > 0)
        )
        ledger.append(
            OpenExecutionBatchBooked(
                event_id=_event_id(
                    spec.run_id,
                    session.session_date.isoformat(),
                    "open-execution-batch",
                ),
                account_id=spec.account_id,
                market=spec.market,
                occurred_at=session.open_at,
                session_date=session.session_date,
                fills=tuple(booked),
                marks=tuple(
                    PositionMark(symbol=symbol, price=open_prices[symbol])
                    for symbol in held_symbols
                ),
            )
        )

    def _resolve_revisions(
        self, *, spec: BacktestSpec, through: int, as_of: datetime
    ) -> tuple[SelectedBarRevision, ...]:
        selected: list[SelectedBarRevision] = []
        for instrument in spec.instruments:
            for session in spec.sessions[: through + 1]:
                revision = self._store.latest_bar_revision_as_of(
                    market=spec.market,
                    symbol=instrument.symbol,
                    session_date=session.session_date,
                    as_of=as_of,
                )
                if revision is None:
                    raise ValueError(
                        "missing point-in-time bar for "
                        f"{instrument.symbol} on {session.session_date.isoformat()}"
                    )
                selected.append(revision)
        return tuple(selected)

    def _evaluate_strategy(
        self,
        spec: BacktestSpec,
        market_snapshot: MarketSnapshot,
        portfolio: PortfolioSnapshot,
    ) -> tuple[StrategyIntent, ...]:
        raw = self._strategy.evaluate(
            StrategyContext(
                market_snapshot=market_snapshot,
                portfolio=portfolio,
                strategy_config_version=spec.strategy_config_version,
            )
        )
        if type(raw) is not tuple:
            raise TypeError("strategy output must be an exact tuple")
        intents = tuple(
            _rebuild_exact(item, StrategyIntent, "strategy intent") for item in raw
        )
        universe = {item.symbol for item in spec.instruments}
        symbols: list[str] = []
        close_at = market_snapshot.as_of.astimezone(UTC)
        for item in intents:
            if item.strategy_id != self._strategy.strategy_id.strip():
                raise ValueError("strategy intent strategy_id must match the strategy")
            if item.market is not spec.market:
                raise ValueError("strategy intent market must match the spec")
            if item.as_of.astimezone(UTC) != close_at:
                raise ValueError("strategy intent as_of must match the session close")
            if item.symbol not in universe:
                raise ValueError("strategy intent symbol must belong to the fixed universe")
            symbols.append(item.symbol)
        if len(symbols) != len(set(symbols)):
            raise ValueError("strategy intent symbols must be unique")
        return intents
