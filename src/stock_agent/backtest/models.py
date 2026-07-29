from collections.abc import Mapping
from datetime import UTC, date, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, TypeVar

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

from stock_agent.account import (
    AcquisitionLot,
    BuyFilled,
    CashAdjusted,
    CashInitialized,
    EventReversed,
    OpenExecutionBatchBooked,
    PortfolioMarked,
    PositionMarked,
    SellFilled,
)
from stock_agent.data import SelectedBarRevision
from stock_agent.domain import Bar, Instrument, Market, PortfolioSnapshot, StrategyIntent
from stock_agent.execution import Fill, FillStatus, OrderIntent
from stock_agent.execution.cn_rules import CnSessionState
from stock_agent.risk import RiskDecision, RiskReductionTarget
from stock_agent.strategies import MarketSnapshot

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Symbol = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=1)]
T = TypeVar("T", bound=BaseModel)
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


def _finite_decimal(value: object) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    return value


def _supported_decimal(value: object) -> Decimal:
    decimal_value = _finite_decimal(value)
    decimal_tuple = decimal_value.as_tuple()
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError("value must be finite")
    trailing_zeroes = 0
    for digit in reversed(decimal_tuple.digits):
        if digit:
            break
        trailing_zeroes += 1
    effective_scale = (
        0 if trailing_zeroes == len(decimal_tuple.digits) else max(0, -exponent - trailing_zeroes)
    )
    if effective_scale > 12 or decimal_value.copy_abs() >= Decimal("1E26"):
        raise ValueError("value must be supported by the ledger Decimal boundary")
    return decimal_value


FiniteNonNegativeDecimal = Annotated[Decimal, BeforeValidator(_finite_decimal), Field(ge=0)]
FiniteUnitDecimal = Annotated[Decimal, BeforeValidator(_finite_decimal), Field(ge=0, le=1)]
SupportedDecimal = Annotated[Decimal, BeforeValidator(_supported_decimal)]
SupportedNonNegativeDecimal = Annotated[Decimal, BeforeValidator(_supported_decimal), Field(ge=0)]
SpecFingerprint = Annotated[
    str,
    StringConstraints(
        pattern=r"^backtest-spec-sha256:[0-9a-f]{64}$",
    ),
]
ResolvedDataFingerprint = Annotated[
    str,
    StringConstraints(
        pattern=r"^resolved-data-sha256:[0-9a-f]{64}$",
    ),
]


def _plain_date(value: object) -> object:
    if type(value) is not date:
        raise ValueError("value must be a plain date")
    return value


def _exact_enum(value: object, expected: type[StrEnum], name: str) -> object:
    if type(value) is not expected:
        raise ValueError(f"{name} must be exactly {expected.__name__}")
    return value


def _model_values(value: BaseModel) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in value.__class__.model_fields:
        try:
            values[name] = getattr(value, name)
        except AttributeError as error:
            raise ValueError(f"nested model is missing field {name!r}") from error
    return values


def _rebuild_exact(value: object, expected: type[T], name: str) -> T:
    if type(value) is not expected:
        raise ValueError(f"{name} must be exactly {expected.__name__}")
    with localcontext() as context:
        context.prec = max(context.prec, 256)
        context.Emin = min(context.Emin, -999999)
        context.Emax = max(context.Emax, 999999)
        return expected(**_model_values(value))


def _rebuild_risk_decision(value: object) -> RiskDecision:
    rebuilt = _rebuild_exact(value, RiskDecision, "risk decision")
    original = _rebuild_exact(rebuilt.original_intent, StrategyIntent, "original intent")
    reduction = (
        None
        if rebuilt.risk_reduction is None
        else _rebuild_exact(rebuilt.risk_reduction, RiskReductionTarget, "risk reduction")
    )
    values = _model_values(rebuilt)
    values["original_intent"] = original
    values["risk_reduction"] = reduction
    return RiskDecision(**values)


class _ImmutableBacktestModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @model_validator(mode="wrap")
    @classmethod
    def decimal_context_is_private(cls, value: Any, handler: Any) -> Self:
        with localcontext() as context:
            context.prec = max(context.prec, 256)
            context.Emin = min(context.Emin, -999999)
            context.Emax = max(context.Emax, 999999)
            return handler(value)

    def __init_subclass__(cls, **kwargs: object) -> None:
        if cls.__bases__ != (_ImmutableBacktestModel,):
            raise TypeError(f"{cls.__name__} does not support subclasses")
        super().__init_subclass__(**kwargs)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise TypeError("immutable backtest models do not support copy projections or updates")
        return super().copy(deep=deep)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if update is not None:
            raise TypeError("immutable backtest models do not support copy updates")
        return super().model_copy(deep=deep)


class BacktestSession(_ImmutableBacktestModel):
    session_date: date
    open_at: AwareDatetime
    close_at: AwareDatetime
    open_bars: tuple[Bar, ...]
    cn_session_states: tuple[CnSessionState, ...] = ()

    _session_date_is_plain = field_validator("session_date", mode="before")(_plain_date)

    @field_validator("open_bars", mode="before")
    @classmethod
    def open_bars_are_exact(cls, value: object) -> tuple[Bar, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("open_bars must be a nonempty exact tuple")
        return tuple(_rebuild_exact(item, Bar, "open bar") for item in value)

    @field_validator("cn_session_states", mode="before")
    @classmethod
    def states_are_exact(cls, value: object) -> tuple[CnSessionState, ...]:
        if type(value) is not tuple:
            raise ValueError("cn_session_states must be an exact tuple")
        return tuple(_rebuild_exact(item, CnSessionState, "CN session state") for item in value)

    @model_validator(mode="after")
    def frame_is_consistent(self) -> Self:
        if self.open_at.astimezone(UTC) >= self.close_at.astimezone(UTC):
            raise ValueError("open_at must be before close_at")
        symbols = tuple(item.symbol for item in self.open_bars)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("open bars must be symbol-sorted and unique")
        market = self.open_bars[0].market
        for item in self.open_bars:
            if item.market is not market or item.session_date != self.session_date:
                raise ValueError("open bars must share market and session date")
            if item.available_at.astimezone(UTC) > self.open_at.astimezone(UTC):
                raise ValueError("open bars must be available by open_at")
            if not (item.open == item.high == item.low == item.close):
                raise ValueError("open bars must contain only the open price")
            if item.volume != 0:
                raise ValueError("open bars must have zero volume")
        state_symbols = tuple(item.symbol for item in self.cn_session_states)
        if state_symbols != tuple(sorted(state_symbols)) or len(state_symbols) != len(
            set(state_symbols)
        ):
            raise ValueError("CN states must be symbol-sorted and unique")
        if market is Market.US and self.cn_session_states:
            raise ValueError("US sessions must not contain CN states")
        if market is Market.CN:
            if state_symbols != symbols:
                raise ValueError("CN state symbols must exactly match open bars")
            if any(item.session_date != self.session_date for item in self.cn_session_states):
                raise ValueError("CN state dates must match the session date")
        return self


class BacktestSpec(_ImmutableBacktestModel):
    run_id: NonBlankText
    account_id: NonBlankText
    market: Market
    initial_cash: SupportedNonNegativeDecimal
    instruments: tuple[Instrument, ...]
    sessions: tuple[BacktestSession, ...]
    strategy_config_version: NonBlankText

    @field_validator("market", mode="before")
    @classmethod
    def market_is_exact(cls, value: object) -> object:
        return _exact_enum(value, Market, "market")

    @field_validator("instruments", mode="before")
    @classmethod
    def instruments_are_exact(cls, value: object) -> tuple[Instrument, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("instruments must be a nonempty exact tuple")
        return tuple(_rebuild_exact(item, Instrument, "instrument") for item in value)

    @field_validator("sessions", mode="before")
    @classmethod
    def sessions_are_exact(cls, value: object) -> tuple[BacktestSession, ...]:
        if type(value) is not tuple or len(value) < 2:
            raise ValueError("sessions must be an exact tuple with at least two items")
        return tuple(_rebuild_exact(item, BacktestSession, "session") for item in value)

    @model_validator(mode="after")
    def spec_is_consistent(self) -> Self:
        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("instruments must be symbol-sorted and unique")
        if any(item.market is not self.market for item in self.instruments):
            raise ValueError("instrument markets must match the spec market")
        dates = tuple(item.session_date for item in self.sessions)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise ValueError("sessions must have sorted unique dates")
        for item in self.sessions:
            if item.open_bars[0].market is not self.market:
                raise ValueError("session markets must match the spec market")
            if tuple(bar.symbol for bar in item.open_bars) != symbols:
                raise ValueError("session frames must exactly match the fixed universe")
        for previous, following in zip(self.sessions, self.sessions[1:], strict=False):
            if previous.close_at.astimezone(UTC) >= following.open_at.astimezone(UTC):
                raise ValueError("each close instant must be before the next open")
        try:
            self.sessions[0].open_at - timedelta(microseconds=1)
        except OverflowError as error:
            raise ValueError("first open cannot represent the initialization instant") from error
        return self


class OrderPlanStatus(StrEnum):
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"


class OrderPlanSource(StrEnum):
    STRATEGY = "STRATEGY"
    RISK_REDUCTION = "RISK_REDUCTION"


class OrderPlan(_ImmutableBacktestModel):
    status: OrderPlanStatus
    source: OrderPlanSource
    symbol: Symbol
    target_weight: FiniteUnitDecimal
    raw_quantity: FiniteNonNegativeDecimal | None
    submitted_quantity: FiniteNonNegativeDecimal | None
    effective_quantity: FiniteNonNegativeDecimal | None
    order: OrderIntent | None
    submission: Fill | None
    reason: NonBlankText | None

    @field_validator("status", mode="before")
    @classmethod
    def status_is_exact(cls, value: object) -> object:
        return _exact_enum(value, OrderPlanStatus, "status")

    @field_validator("source", mode="before")
    @classmethod
    def source_is_exact(cls, value: object) -> object:
        return _exact_enum(value, OrderPlanSource, "source")

    @field_validator("order", mode="before")
    @classmethod
    def order_is_exact(cls, value: object) -> OrderIntent | None:
        return None if value is None else _rebuild_exact(value, OrderIntent, "order")

    @field_validator("submission", mode="before")
    @classmethod
    def submission_is_exact(cls, value: object) -> Fill | None:
        return None if value is None else _rebuild_exact(value, Fill, "submission")

    @model_validator(mode="after")
    def plan_state_is_consistent(self) -> Self:
        if self.submitted_quantity is not None:
            if self.raw_quantity is None or self.submitted_quantity > self.raw_quantity:
                raise ValueError("submitted quantity requires and cannot exceed raw quantity")
        if (
            self.effective_quantity is not None
            and self.submitted_quantity is not None
            and self.effective_quantity > self.submitted_quantity
        ):
            raise ValueError("effective quantity cannot exceed submitted quantity")
        if self.order is not None:
            if self.order.symbol != self.symbol or self.order.quantity != self.submitted_quantity:
                raise ValueError("order identity and quantity must match the plan")
        if self.status is OrderPlanStatus.READY:
            if (
                self.order is None
                or self.raw_quantity is None
                or self.submitted_quantity is None
                or self.submission is not None
                or self.effective_quantity is not None
                or self.reason is not None
            ):
                raise ValueError("READY plan has invalid state")
        elif self.status is OrderPlanStatus.SUBMITTED:
            if (
                self.order is None
                or self.submission is None
                or self.raw_quantity is None
                or self.submitted_quantity is None
                or self.effective_quantity != self.submission.requested_quantity
                or self.reason is not None
            ):
                raise ValueError("SUBMITTED plan has invalid state")
            identity = ("order_id", "account_id", "symbol", "market", "side")
            if any(
                getattr(self.order, name) != getattr(self.submission, name) for name in identity
            ):
                raise ValueError("submission identity must match the order")
        elif (
            self.order is not None
            or self.submission is not None
            or self.effective_quantity is not None
            or self.submitted_quantity is not None
            or self.reason is None
        ):
            raise ValueError("terminal unsubmitted plans require only a reason")
        return self


class SessionResult(_ImmutableBacktestModel):
    session_date: date
    execution_results: tuple[Fill, ...]
    selected_revisions: tuple[SelectedBarRevision, ...]
    market_snapshot: MarketSnapshot
    portfolio_snapshot: PortfolioSnapshot
    intents: tuple[StrategyIntent, ...]
    risk_decisions: tuple[RiskDecision, ...]
    portfolio_reduction: RiskReductionTarget | None
    order_plans: tuple[OrderPlan, ...]
    submission_results: tuple[Fill, ...]

    _session_date_is_plain = field_validator("session_date", mode="before")(_plain_date)

    @field_validator("execution_results", "submission_results", mode="before")
    @classmethod
    def fills_are_exact(cls, value: object) -> tuple[Fill, ...]:
        if type(value) is not tuple:
            raise ValueError("fill collections must be exact tuples")
        return tuple(_rebuild_exact(item, Fill, "fill") for item in value)

    @field_validator("selected_revisions", mode="before")
    @classmethod
    def revisions_are_exact(cls, value: object) -> tuple[SelectedBarRevision, ...]:
        if type(value) is not tuple:
            raise ValueError("selected_revisions must be an exact tuple")
        return tuple(
            _rebuild_exact(item, SelectedBarRevision, "selected revision") for item in value
        )

    @field_validator("market_snapshot", mode="before")
    @classmethod
    def market_snapshot_is_exact(cls, value: object) -> MarketSnapshot:
        return _rebuild_exact(value, MarketSnapshot, "market snapshot")

    @field_validator("portfolio_snapshot", mode="before")
    @classmethod
    def portfolio_snapshot_is_exact(cls, value: object) -> PortfolioSnapshot:
        return _rebuild_exact(value, PortfolioSnapshot, "portfolio snapshot")

    @field_validator("intents", mode="before")
    @classmethod
    def intents_are_exact(cls, value: object) -> tuple[StrategyIntent, ...]:
        if type(value) is not tuple:
            raise ValueError("intents must be an exact tuple")
        return tuple(_rebuild_exact(item, StrategyIntent, "intent") for item in value)

    @field_validator("risk_decisions", mode="before")
    @classmethod
    def decisions_are_exact(cls, value: object) -> tuple[RiskDecision, ...]:
        if type(value) is not tuple:
            raise ValueError("risk_decisions must be an exact tuple")
        return tuple(_rebuild_risk_decision(item) for item in value)

    @field_validator("portfolio_reduction", mode="before")
    @classmethod
    def reduction_is_exact(cls, value: object) -> RiskReductionTarget | None:
        return (
            None
            if value is None
            else _rebuild_exact(value, RiskReductionTarget, "portfolio reduction")
        )

    @field_validator("order_plans", mode="before")
    @classmethod
    def plans_are_exact(cls, value: object) -> tuple[OrderPlan, ...]:
        if type(value) is not tuple:
            raise ValueError("order_plans must be an exact tuple")
        return tuple(_rebuild_exact(item, OrderPlan, "order plan") for item in value)

    @model_validator(mode="after")
    def close_identity_is_consistent(self) -> Self:
        if self.market_snapshot.market is not self.portfolio_snapshot.market:
            raise ValueError("close snapshot markets must match")
        if self.market_snapshot.as_of.astimezone(UTC) != self.portfolio_snapshot.as_of.astimezone(
            UTC
        ):
            raise ValueError("close snapshot instants must match")
        market = self.market_snapshot.market
        account = self.portfolio_snapshot.account_id
        close_instant = self.market_snapshot.as_of.astimezone(UTC)
        if tuple(item.bar for item in self.selected_revisions) != self.market_snapshot.bars:
            raise ValueError("selected revision bars must exactly match the close market bars")
        if any(item.session_date > self.session_date for item in self.market_snapshot.bars):
            raise ValueError("close market bars cannot be from a future session")
        if any(item.market is not market for item in self.intents):
            raise ValueError("intent markets must match the close market")
        if any(
            item.as_of.astimezone(UTC) != close_instant
            for item in self.intents
        ):
            raise ValueError("intent as_of values must match the close instant")
        if tuple(item.original_intent for item in self.risk_decisions) != self.intents:
            raise ValueError("risk decisions must exactly correspond to the strategy intents")
        if self.risk_decisions and any(
            item.risk_reduction != self.portfolio_reduction for item in self.risk_decisions
        ):
            raise ValueError("risk decision reductions must match the portfolio reduction")
        if any(
            item.market is not market or item.account_id != account
            for item in (*self.execution_results, *self.submission_results)
        ):
            raise ValueError("fill identity must match the close snapshots")
        close_symbols = {item.symbol for item in self.market_snapshot.bars}
        if any(item.symbol not in close_symbols for item in self.execution_results):
            raise ValueError("execution symbols must occur in the close market snapshot")
        if any(
            item.status not in (FillStatus.FILLED, FillStatus.REJECTED)
            or (item.status is FillStatus.FILLED and item.session_date != self.session_date)
            for item in self.execution_results
        ):
            raise ValueError("execution results must be terminal for the session date")
        if any(item.status is OrderPlanStatus.READY for item in self.order_plans):
            raise ValueError("completed session results must not contain READY plans")
        for plan in self.order_plans:
            if plan.order is not None and (
                plan.order.account_id != account
                or plan.order.market is not market
                or plan.order.symbol != plan.symbol
            ):
                raise ValueError("planned order identity must match the close snapshots")
        planned_submissions = tuple(
            plan.submission
            for plan in self.order_plans
            if plan.status is OrderPlanStatus.SUBMITTED
        )
        if planned_submissions != self.submission_results:
            raise ValueError("submission results must exactly match submitted plans in order")
        if any(
            item.status not in (FillStatus.PENDING, FillStatus.REJECTED)
            for item in self.submission_results
        ):
            raise ValueError("submission results must be pending or immediately rejected")
        return self


class BacktestInputManifest(_ImmutableBacktestModel):
    account_id: NonBlankText
    market: Market
    initial_cash: SupportedNonNegativeDecimal
    instruments: tuple[Instrument, ...]
    calendar_sessions: tuple[date, ...]
    sessions: tuple[BacktestSession, ...]
    strategy_id: NonBlankText
    strategy_config_version: NonBlankText
    transaction_cost_bps: SupportedNonNegativeDecimal
    pit_knowledge_policy: Literal["business-available-at/v1"]

    @field_validator("market", mode="before")
    @classmethod
    def market_is_exact(cls, value: object) -> object:
        return _exact_enum(value, Market, "market")

    @field_validator("instruments", mode="before")
    @classmethod
    def instruments_are_exact(cls, value: object) -> tuple[Instrument, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("instruments must be a nonempty exact tuple")
        return tuple(_rebuild_exact(item, Instrument, "instrument") for item in value)

    @field_validator("calendar_sessions", mode="before")
    @classmethod
    def calendar_is_exact(cls, value: object) -> tuple[date, ...]:
        if type(value) is not tuple or len(value) < 2:
            raise ValueError("calendar_sessions must be an exact tuple with at least two items")
        for item in value:
            _plain_date(item)
        return value

    @field_validator("sessions", mode="before")
    @classmethod
    def sessions_are_exact(cls, value: object) -> tuple[BacktestSession, ...]:
        if type(value) is not tuple or len(value) < 2:
            raise ValueError("sessions must be an exact tuple with at least two items")
        return tuple(_rebuild_exact(item, BacktestSession, "session") for item in value)

    @model_validator(mode="after")
    def manifest_is_consistent(self) -> Self:
        symbols = tuple(item.symbol for item in self.instruments)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("instruments must be symbol-sorted and unique")
        if any(item.market is not self.market for item in self.instruments):
            raise ValueError("instrument markets must match manifest market")
        if self.calendar_sessions != tuple(sorted(self.calendar_sessions)) or len(
            self.calendar_sessions
        ) != len(set(self.calendar_sessions)):
            raise ValueError("calendar sessions must be sorted and unique")
        session_dates = tuple(item.session_date for item in self.sessions)
        if session_dates != tuple(sorted(session_dates)) or len(session_dates) != len(
            set(session_dates)
        ):
            raise ValueError("sessions must have sorted unique dates")
        if session_dates != self.calendar_sessions:
            raise ValueError("session dates must exactly match the calendar slice")
        for item in self.sessions:
            if item.open_bars[0].market is not self.market:
                raise ValueError("session markets must match manifest market")
            if tuple(bar.symbol for bar in item.open_bars) != symbols:
                raise ValueError("session frames must match the manifest universe")
        for previous, following in zip(self.sessions, self.sessions[1:], strict=False):
            if previous.close_at.astimezone(UTC) >= following.open_at.astimezone(UTC):
                raise ValueError("each close instant must be before the next open")
        return self


class BacktestResult(_ImmutableBacktestModel):
    run_id: NonBlankText
    manifest: BacktestInputManifest
    spec_fingerprint: SpecFingerprint
    resolved_data_fingerprint: ResolvedDataFingerprint
    sessions: tuple[SessionResult, ...]
    ledger_events: tuple[
        CashInitialized
        | BuyFilled
        | SellFilled
        | CashAdjusted
        | PositionMarked
        | OpenExecutionBatchBooked
        | PortfolioMarked
        | EventReversed,
        ...,
    ]
    final_lots: tuple[AcquisitionLot, ...]
    final_snapshot: PortfolioSnapshot
    realized_pnl: SupportedDecimal

    @field_validator("manifest", mode="before")
    @classmethod
    def manifest_is_exact(cls, value: object) -> BacktestInputManifest:
        return _rebuild_exact(value, BacktestInputManifest, "manifest")

    @field_validator("sessions", mode="before")
    @classmethod
    def sessions_are_exact(cls, value: object) -> tuple[SessionResult, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("sessions must be a nonempty exact tuple")
        return tuple(_rebuild_exact(item, SessionResult, "session result") for item in value)

    @field_validator("ledger_events", mode="before")
    @classmethod
    def events_are_exact(cls, value: object) -> tuple[BaseModel, ...]:
        if type(value) is not tuple or not value:
            raise ValueError("ledger_events must be a nonempty exact tuple")
        if type(value[0]) is not CashInitialized:
            raise ValueError("ledger_events must begin with exact CashInitialized")
        rebuilt: list[BaseModel] = []
        for item in value:
            if type(item) not in _LEDGER_EVENT_TYPES:
                raise ValueError("ledger_events must contain exact concrete LedgerEvent values")
            rebuilt.append(_rebuild_exact(item, type(item), "ledger event"))
        return tuple(rebuilt)

    @field_validator("final_lots", mode="before")
    @classmethod
    def lots_are_exact(cls, value: object) -> tuple[AcquisitionLot, ...]:
        if type(value) is not tuple:
            raise ValueError("final_lots must be an exact tuple")
        return tuple(_rebuild_exact(item, AcquisitionLot, "acquisition lot") for item in value)

    @field_validator("final_snapshot", mode="before")
    @classmethod
    def final_snapshot_is_exact(cls, value: object) -> PortfolioSnapshot:
        return _rebuild_exact(value, PortfolioSnapshot, "final snapshot")

    @model_validator(mode="after")
    def result_is_consistent(self) -> Self:
        account = self.manifest.account_id
        market = self.manifest.market
        if self.final_snapshot.account_id != account or self.final_snapshot.market is not market:
            raise ValueError("final snapshot identity must match the manifest")
        if any(
            item.portfolio_snapshot.account_id != account
            or item.portfolio_snapshot.market is not market
            for item in self.sessions
        ):
            raise ValueError("session result identity must match the manifest")
        if any(
            item.account_id != account or item.market is not market for item in self.ledger_events
        ):
            raise ValueError("ledger event identity must match the manifest")
        result_dates = tuple(item.session_date for item in self.sessions)
        if result_dates != self.manifest.calendar_sessions:
            raise ValueError("session result dates must exactly match the manifest calendar")
        for index, result in enumerate(self.sessions):
            expected_keys = tuple(
                (instrument.symbol, session_date)
                for instrument in self.manifest.instruments
                for session_date in self.manifest.calendar_sessions[: index + 1]
            )
            actual_keys = tuple(
                (bar.symbol, bar.session_date) for bar in result.market_snapshot.bars
            )
            if actual_keys != expected_keys:
                raise ValueError("session market snapshots must match the cumulative PIT grid")
        if any(
            result.portfolio_snapshot.as_of.astimezone(UTC)
            != manifest_session.close_at.astimezone(UTC)
            for result, manifest_session in zip(
                self.sessions, self.manifest.sessions, strict=True
            )
        ):
            raise ValueError("session snapshots must match manifest close instants")
        if self.sessions[-1].portfolio_snapshot != self.final_snapshot:
            raise ValueError("final snapshot must equal the final session snapshot")
        if self.sessions[0].execution_results:
            raise ValueError("the first session must not contain execution results")
        execution_identity = (
            "order_id",
            "account_id",
            "symbol",
            "side",
            "requested_quantity",
        )
        for previous, current in zip(self.sessions, self.sessions[1:], strict=False):
            pending = tuple(
                item
                for item in previous.submission_results
                if item.status is FillStatus.PENDING
            )
            if len(pending) != len(current.execution_results) or any(
                any(
                    getattr(submission, name) != getattr(execution, name)
                    for name in execution_identity
                )
                for submission, execution in zip(pending, current.execution_results, strict=True)
            ):
                raise ValueError("pending submissions must exactly match next-session executions")
        final = self.sessions[-1]
        if (
            final.intents
            or final.risk_decisions
            or final.portfolio_reduction is not None
            or final.order_plans
            or final.submission_results
        ):
            raise ValueError("the final session must skip strategy, risk, and submission")
        return self
