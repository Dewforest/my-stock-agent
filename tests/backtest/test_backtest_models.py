from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal, localcontext

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import stock_agent.backtest as backtest
from stock_agent.account import AcquisitionLot, CashAdjusted, CashInitialized
from stock_agent.backtest import (
    BacktestInputManifest,
    BacktestResult,
    BacktestSession,
    BacktestSpec,
    OrderPlan,
    OrderPlanSource,
    OrderPlanStatus,
    SessionResult,
)
from stock_agent.data import PointInTimeStore, SelectedBarRevision
from stock_agent.domain import (
    Bar,
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Side,
    StrategyIntent,
)
from stock_agent.execution import Fill, FillStatus, OrderIntent
from stock_agent.execution.cn_rules import CnPriceLimitState, CnSessionState
from stock_agent.risk import RiskDecision, RiskDecisionStatus, RiskReductionTarget
from stock_agent.strategies import MarketSnapshot

D1 = date(2026, 7, 27)
D2 = date(2026, 7, 28)
US_OPEN_1 = datetime(2026, 7, 27, 9, 30, tzinfo=timezone(timedelta(hours=-4)))
US_CLOSE_1 = datetime(2026, 7, 27, 16, tzinfo=timezone(timedelta(hours=-4)))
US_OPEN_2 = datetime(2026, 7, 28, 9, 30, tzinfo=timezone(timedelta(hours=-4)))
US_CLOSE_2 = datetime(2026, 7, 28, 16, tzinfo=timezone(timedelta(hours=-4)))


def model_values(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, object]:
    excluded = exclude or set()
    return {
        name: getattr(model, name) for name in model.__class__.model_fields if name not in excluded
    }


def instrument(symbol: str = "AAPL", market: Market = Market.US) -> Instrument:
    return Instrument(
        symbol=symbol,
        market=market,
        currency=Currency.USD if market is Market.US else Currency.CNY,
        sector="Technology",
    )


def open_bar(
    symbol: str = "AAPL",
    *,
    market: Market = Market.US,
    session_date: date = D1,
    open_at: datetime = US_OPEN_1,
) -> Bar:
    return Bar(
        symbol=symbol,
        market=market,
        session_date=session_date,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("0"),
        available_at=open_at,
    )


def session(
    session_date: date = D1,
    open_at: datetime = US_OPEN_1,
    close_at: datetime = US_CLOSE_1,
    *,
    market: Market = Market.US,
    symbols: tuple[str, ...] = ("AAPL",),
) -> BacktestSession:
    bars = tuple(
        open_bar(symbol, market=market, session_date=session_date, open_at=open_at)
        for symbol in symbols
    )
    states = (
        tuple(
            CnSessionState(
                symbol=symbol,
                session_date=session_date,
                suspended=False,
                price_limit_state=CnPriceLimitState.NONE,
            )
            for symbol in symbols
        )
        if market is Market.CN
        else ()
    )
    return BacktestSession(
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        open_bars=bars,
        cn_session_states=states,
    )


def spec() -> BacktestSpec:
    return BacktestSpec(
        run_id="run-1",
        account_id="account-1",
        market=Market.US,
        initial_cash=Decimal("1000.00"),
        instruments=(instrument(),),
        sessions=(session(), session(D2, US_OPEN_2, US_CLOSE_2)),
        strategy_config_version="config-v1",
    )


def order() -> OrderIntent:
    return OrderIntent(
        order_id="order-1",
        account_id="account-1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        quantity=Decimal("2"),
    )


def submission(status: FillStatus = FillStatus.PENDING) -> Fill:
    return Fill(
        status=status,
        order_id="order-1",
        account_id="account-1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        requested_quantity=Decimal("2"),
        filled_quantity=Decimal("0"),
        price=None,
        fees=Decimal("0"),
        session_date=None,
        reason=None if status is FillStatus.PENDING else "blocked",
    )


def execution_result(
    status: FillStatus = FillStatus.FILLED,
    *,
    session_date: date = D2,
    order_id: str = "order-1",
) -> Fill:
    return Fill(
        status=status,
        order_id=order_id,
        account_id="account-1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        requested_quantity=Decimal("2"),
        filled_quantity=Decimal("2") if status is FillStatus.FILLED else Decimal("0"),
        price=Decimal("100") if status is FillStatus.FILLED else None,
        fees=Decimal("1") if status is FillStatus.FILLED else Decimal("0"),
        session_date=session_date if status is FillStatus.FILLED else None,
        reason=None if status is FillStatus.FILLED else "blocked",
    )


def ready_plan() -> OrderPlan:
    return OrderPlan(
        status=OrderPlanStatus.READY,
        source=OrderPlanSource.STRATEGY,
        symbol="AAPL",
        target_weight=Decimal("0.2"),
        raw_quantity=Decimal("2.0001"),
        submitted_quantity=Decimal("2"),
        effective_quantity=None,
        order=order(),
        submission=None,
        reason=None,
    )


def submitted_plan(status: FillStatus = FillStatus.PENDING) -> OrderPlan:
    return OrderPlan(
        **model_values(ready_plan(), exclude={"status", "submission", "effective_quantity"}),
        status=OrderPlanStatus.SUBMITTED,
        submission=submission(status),
        effective_quantity=Decimal("2"),
    )


def close_bar(
    session_date: date = D2,
    as_of: datetime = US_CLOSE_2,
    *,
    symbol: str = "AAPL",
) -> Bar:
    return Bar(
        **open_bar(symbol, session_date=session_date, open_at=as_of).model_dump(
            exclude={"volume"}
        ),
        volume=Decimal("10"),
    )


def selected_revision(
    session_date: date = D2,
    as_of: datetime = US_CLOSE_2,
    *,
    symbol: str = "AAPL",
) -> SelectedBarRevision:
    return SelectedBarRevision(
        bar=close_bar(session_date, as_of, symbol=symbol),
        ingested_at=as_of,
        source="feed",
        source_record_id=f"{symbol}-{session_date.isoformat()}",
    )


def cumulative_revisions(
    session_date: date,
    *,
    symbols: tuple[str, ...] = ("AAPL",),
) -> tuple[SelectedBarRevision, ...]:
    dated_closes = ((D1, US_CLOSE_1),) if session_date == D1 else (
        (D1, US_CLOSE_1),
        (D2, US_CLOSE_2),
    )
    return tuple(
        selected_revision(bar_date, close_at, symbol=symbol)
        for symbol in symbols
        for bar_date, close_at in dated_closes
    )


def market_snapshot(
    as_of: datetime = US_CLOSE_2,
    *,
    session_date: date = D2,
    symbols: tuple[str, ...] = ("AAPL",),
) -> MarketSnapshot:
    revisions = cumulative_revisions(session_date, symbols=symbols)
    return MarketSnapshot(as_of=as_of, market=Market.US, bars=tuple(r.bar for r in revisions))


def strategy_intent(as_of: datetime = US_CLOSE_2) -> StrategyIntent:
    return StrategyIntent(
        strategy_id="strategy-1",
        symbol="AAPL",
        market=Market.US,
        side=Side.HOLD,
        target_weight=Decimal("0"),
        confidence=50,
        as_of=as_of,
        thesis="wait",
        invalidation="change",
    )


def portfolio_snapshot(as_of: datetime = US_CLOSE_2) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("1000"),
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        positions=(),
        as_of=as_of,
    )


def session_result(
    session_date: date = D2,
    *,
    symbols: tuple[str, ...] = ("AAPL",),
) -> SessionResult:
    close_at = US_CLOSE_1 if session_date == D1 else US_CLOSE_2
    revisions = cumulative_revisions(session_date, symbols=symbols)
    return SessionResult(
        session_date=session_date,
        execution_results=(),
        selected_revisions=revisions,
        market_snapshot=MarketSnapshot(
            as_of=close_at,
            market=Market.US,
            bars=tuple(revision.bar for revision in revisions),
        ),
        portfolio_snapshot=portfolio_snapshot(close_at),
        intents=(),
        risk_decisions=(),
        portfolio_reduction=None,
        order_plans=(),
        submission_results=(),
    )


def session_result_with_execution(session_date: date = D2) -> SessionResult:
    return SessionResult(
        **model_values(
            session_result(session_date),
            exclude={"execution_results"},
        ),
        execution_results=(execution_result(session_date=session_date),),
    )


def session_result_with_revisions(
    session_date: date,
    revisions: tuple[SelectedBarRevision, ...],
) -> SessionResult:
    close_at = US_CLOSE_1 if session_date == D1 else US_CLOSE_2
    return SessionResult(
        **model_values(
            session_result(session_date), exclude={"selected_revisions", "market_snapshot"}
        ),
        selected_revisions=revisions,
        market_snapshot=MarketSnapshot(
            as_of=close_at,
            market=Market.US,
            bars=tuple(revision.bar for revision in revisions),
        ),
    )


def manifest() -> BacktestInputManifest:
    return BacktestInputManifest(
        account_id="account-1",
        market=Market.US,
        initial_cash=Decimal("1000"),
        instruments=(instrument(),),
        calendar_sessions=(D1, D2),
        sessions=(session(), session(D2, US_OPEN_2, US_CLOSE_2)),
        strategy_id="strategy-1",
        strategy_config_version="config-v1",
        transaction_cost_bps=Decimal("5.25"),
        pit_knowledge_policy="business-available-at/v1",
    )


def result() -> BacktestResult:
    return BacktestResult(
        run_id="run-1",
        manifest=manifest(),
        spec_fingerprint="backtest-spec-sha256:" + "a" * 64,
        resolved_data_fingerprint="resolved-data-sha256:" + "b" * 64,
        sessions=(session_result(D1), session_result(D2)),
        ledger_events=(
            CashInitialized(
                event_id="event-1",
                account_id="account-1",
                market=Market.US,
                occurred_at=US_OPEN_1 - timedelta(microseconds=1),
                amount=Decimal("1000"),
            ),
        ),
        final_lots=(),
        final_snapshot=portfolio_snapshot(),
        realized_pnl=Decimal("0.00"),
    )


def test_backtest_package_exports_exact_contract() -> None:
    assert backtest.__all__ == [
        "BacktestInputManifest",
        "BacktestResult",
        "BacktestRunner",
        "BacktestSession",
        "BacktestSpec",
        "OrderPlan",
        "OrderPlanSource",
        "OrderPlanStatus",
        "SessionResult",
        "plan_orders",
        "record_submission",
    ]


def test_backtest_session_accepts_valid_us_and_cn_frames() -> None:
    us = session(symbols=("AAPL", "MSFT"))
    cn_open = datetime(2026, 7, 27, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    cn_close = datetime(2026, 7, 27, 15, tzinfo=timezone(timedelta(hours=8)))
    cn = session(D1, cn_open, cn_close, market=Market.CN, symbols=("600000", "600519"))

    assert tuple(bar.symbol for bar in us.open_bars) == ("AAPL", "MSFT")
    assert tuple(state.symbol for state in cn.cn_session_states) == ("600000", "600519")


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_date": datetime(2026, 7, 27, tzinfo=UTC)},
        {"open_at": datetime(2026, 7, 27, 13, 30)},
        {"close_at": US_OPEN_1},
        {"open_bars": []},
        {"open_bars": ()},
        {"open_bars": (open_bar("MSFT"), open_bar("AAPL"))},
        {"open_bars": (open_bar(), open_bar())},
        {"open_bars": (open_bar(market=Market.CN),)},
        {"open_bars": (open_bar(session_date=D2, open_at=US_OPEN_2),)},
        {"open_bars": (open_bar(open_at=US_OPEN_1 + timedelta(seconds=1)),)},
        {
            "open_bars": (
                Bar.model_construct(**open_bar().model_dump(exclude={"high"}), high=Decimal("101")),
            )
        },
        {
            "open_bars": (
                Bar.model_construct(
                    **open_bar().model_dump(exclude={"volume"}), volume=Decimal("1")
                ),
            )
        },
        {"cn_session_states": []},
        {
            "cn_session_states": (
                CnSessionState(
                    symbol="AAPL",
                    session_date=D1,
                    suspended=False,
                    price_limit_state=CnPriceLimitState.NONE,
                ),
            )
        },
    ],
)
def test_backtest_session_rejects_invalid_frames(overrides: dict[str, object]) -> None:
    values = {
        "session_date": D1,
        "open_at": US_OPEN_1,
        "close_at": US_CLOSE_1,
        "open_bars": (open_bar(),),
        "cn_session_states": (),
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        BacktestSession(**values)


def test_backtest_session_compares_cross_timezone_timestamps_by_instant() -> None:
    with pytest.raises(ValidationError):
        BacktestSession(
            session_date=D1,
            open_at=datetime(2026, 7, 27, 9, tzinfo=timezone(timedelta(hours=8))),
            close_at=datetime(2026, 7, 27, 1, tzinfo=UTC),
            open_bars=(
                open_bar(open_at=datetime(2026, 7, 27, 9, tzinfo=timezone(timedelta(hours=8)))),
            ),
            cn_session_states=(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": " "},
        {"account_id": ""},
        {"market": "US"},
        {"initial_cash": 1000},
        {"initial_cash": Decimal("-1")},
        {"initial_cash": Decimal("0.0000000000001")},
        {"initial_cash": Decimal("1E26")},
        {"instruments": []},
        {"instruments": ()},
        {"instruments": (instrument("MSFT"), instrument("AAPL"))},
        {"instruments": (instrument(), instrument("aapl"))},
        {"instruments": (instrument(market=Market.CN),)},
        {"sessions": [session(), session(D2, US_OPEN_2, US_CLOSE_2)]},
        {"sessions": (session(),)},
        {"sessions": (session(D2, US_OPEN_2, US_CLOSE_2), session())},
        {"sessions": (session(), session())},
        {"sessions": (session(D1, US_OPEN_1, US_OPEN_2), session(D2, US_OPEN_2, US_CLOSE_2))},
        {"strategy_config_version": "  "},
    ],
)
def test_backtest_spec_rejects_invalid_contract(overrides: dict[str, object]) -> None:
    values = model_values(spec())
    values.update(overrides)
    with pytest.raises(ValidationError):
        BacktestSpec(**values)


def test_backtest_spec_requires_open_frame_to_match_fixed_universe() -> None:
    with pytest.raises(ValidationError):
        BacktestSpec(
            **model_values(spec(), exclude={"instruments"}),
            instruments=(instrument(), instrument("MSFT")),
        )


def test_backtest_spec_rejects_unrepresentable_initialization_instant() -> None:
    first = session(
        date.min,
        datetime.min.replace(tzinfo=UTC),
        datetime.min.replace(tzinfo=UTC) + timedelta(hours=1),
    )
    second = session(
        date.min + timedelta(days=1),
        datetime.min.replace(tzinfo=UTC) + timedelta(days=1),
        datetime.min.replace(tzinfo=UTC) + timedelta(days=1, hours=1),
    )
    with pytest.raises(ValidationError):
        BacktestSpec(**model_values(spec(), exclude={"sessions"}), sessions=(first, second))


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        (OrderPlanStatus.READY, {}),
        (
            OrderPlanStatus.SUBMITTED,
            {"submission": submission(), "effective_quantity": Decimal("2")},
        ),
        (
            OrderPlanStatus.SKIPPED,
            {"order": None, "raw_quantity": None, "submitted_quantity": None, "reason": "dust"},
        ),
        (
            OrderPlanStatus.REJECTED,
            {"order": None, "submitted_quantity": None, "reason": "side mismatch"},
        ),
    ],
)
def test_order_plan_accepts_each_valid_state(
    status: OrderPlanStatus, overrides: dict[str, object]
) -> None:
    values = model_values(ready_plan())
    values.update(status=status, **overrides)
    assert OrderPlan(**values).status is status


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": OrderPlanStatus.READY, "order": None},
        {"status": OrderPlanStatus.READY, "submission": submission()},
        {"status": OrderPlanStatus.READY, "reason": "unexpected"},
        {"status": OrderPlanStatus.READY, "raw_quantity": None},
        {"status": OrderPlanStatus.READY, "submitted_quantity": None},
        {
            "status": OrderPlanStatus.SUBMITTED,
            "submission": None,
            "effective_quantity": Decimal("2"),
        },
        {
            "status": OrderPlanStatus.SUBMITTED,
            "submission": submission(),
            "effective_quantity": Decimal("1"),
        },
        {
            "status": OrderPlanStatus.SUBMITTED,
            "submission": submission(),
            "effective_quantity": Decimal("2"),
            "reason": "bad",
        },
        {"status": OrderPlanStatus.SKIPPED, "reason": "dust"},
        {"status": OrderPlanStatus.REJECTED, "order": None, "reason": None},
        {"target_weight": Decimal("1.01")},
        {"raw_quantity": Decimal("NaN")},
        {"submitted_quantity": Decimal("-1")},
    ],
)
def test_order_plan_rejects_invalid_states(overrides: dict[str, object]) -> None:
    values = model_values(ready_plan())
    values.update(overrides)
    with pytest.raises(ValidationError):
        OrderPlan(**values)


def test_submitted_order_plan_requires_submission_identity_to_match_order() -> None:
    bad_fill = submission().model_copy(update={"order_id": "other"})
    with pytest.raises(ValidationError):
        OrderPlan(
            **model_values(ready_plan(), exclude={"status", "submission", "effective_quantity"}),
            status=OrderPlanStatus.SUBMITTED,
            submission=bad_fill,
            effective_quantity=Decimal("2"),
        )


def test_session_result_accepts_auditable_exact_contract() -> None:
    revision = selected_revision()
    intent = strategy_intent()
    reduction = RiskReductionTarget(
        current_gross_exposure=Decimal("0.5"),
        target_gross_exposure=Decimal("0.25"),
    )
    decision = RiskDecision(
        original_intent=intent,
        status=RiskDecisionStatus.APPROVED,
        approved_target_weight=Decimal("0"),
        risk_reduction=reduction,
    )
    value = SessionResult(
        **model_values(
            session_result(),
            exclude={
                "selected_revisions",
                "market_snapshot",
                "intents",
                "risk_decisions",
                "portfolio_reduction",
            },
        ),
        selected_revisions=(revision,),
        market_snapshot=MarketSnapshot(
            as_of=US_CLOSE_2, market=Market.US, bars=(revision.bar,)
        ),
        intents=(intent,),
        risk_decisions=(decision,),
        portfolio_reduction=reduction,
    )
    assert value.selected_revisions == (revision,)


@pytest.mark.parametrize(
    "field",
    [
        "execution_results",
        "selected_revisions",
        "intents",
        "risk_decisions",
        "order_plans",
        "submission_results",
    ],
)
def test_session_result_requires_exact_tuples(field: str) -> None:
    values = model_values(session_result())
    values[field] = []
    with pytest.raises(ValidationError):
        SessionResult(**values)


def test_session_result_requires_close_identity_and_as_of_consistency() -> None:
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"portfolio_snapshot"}),
            portfolio_snapshot=portfolio_snapshot(US_CLOSE_1),
        )


def test_session_result_rejects_nonterminal_or_wrong_session_execution_results() -> None:
    for fill in (submission(), execution_result(session_date=D1)):
        with pytest.raises(ValidationError):
            SessionResult(
                **model_values(session_result(), exclude={"execution_results"}),
                execution_results=(fill,),
            )


def test_session_result_rejects_execution_symbol_outside_close_snapshot() -> None:
    fill = execution_result().model_copy(update={"symbol": "MSFT"})
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"execution_results"}),
            execution_results=(fill,),
        )


def test_session_result_requires_selected_revision_bars_to_exactly_match_close_bars() -> None:
    revision = selected_revision()
    for revisions, bars in (((revision,), ()), ((), (revision.bar,))):
        with pytest.raises(ValidationError):
            SessionResult(
                **model_values(
                    session_result(), exclude={"selected_revisions", "market_snapshot"}
                ),
                selected_revisions=revisions,
                market_snapshot=MarketSnapshot(as_of=US_CLOSE_2, market=Market.US, bars=bars),
            )


def test_session_result_accepts_historical_close_bars_but_rejects_future_bars() -> None:
    historical = selected_revision(D1, US_CLOSE_1)
    value = SessionResult(
        **model_values(session_result(), exclude={"selected_revisions", "market_snapshot"}),
        selected_revisions=(historical,),
        market_snapshot=MarketSnapshot(
            as_of=US_CLOSE_2, market=Market.US, bars=(historical.bar,)
        ),
    )
    assert value.market_snapshot.bars == (historical.bar,)

    future = selected_revision(D2, US_CLOSE_2)
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(
                session_result(D1), exclude={"selected_revisions", "market_snapshot"}
            ),
            selected_revisions=(future,),
            market_snapshot=MarketSnapshot(
                as_of=US_CLOSE_2, market=Market.US, bars=(future.bar,)
            ),
        )


def test_session_result_requires_risk_decisions_to_match_intents_and_reduction() -> None:
    intent = strategy_intent()
    other = StrategyIntent(**model_values(intent, exclude={"strategy_id"}), strategy_id="other")
    reduction = RiskReductionTarget(
        current_gross_exposure=Decimal("0.5"), target_gross_exposure=Decimal("0.25")
    )
    wrong_reduction = RiskReductionTarget(
        current_gross_exposure=Decimal("0.5"), target_gross_exposure=Decimal("0.2")
    )
    for decision in (
        RiskDecision(
            original_intent=other,
            status=RiskDecisionStatus.APPROVED,
            approved_target_weight=Decimal("0"),
            risk_reduction=reduction,
        ),
        RiskDecision(
            original_intent=intent,
            status=RiskDecisionStatus.APPROVED,
            approved_target_weight=Decimal("0"),
            risk_reduction=wrong_reduction,
        ),
    ):
        with pytest.raises(ValidationError):
            SessionResult(
                **model_values(
                    session_result(),
                    exclude={"intents", "risk_decisions", "portfolio_reduction"},
                ),
                intents=(intent,),
                risk_decisions=(decision,),
                portfolio_reduction=reduction,
            )


def test_session_result_rejects_ready_plans_and_requires_submissions_in_plan_order() -> None:
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"order_plans"}),
            order_plans=(ready_plan(),),
        )

    plan = submitted_plan()
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"order_plans", "submission_results"}),
            order_plans=(plan,),
            submission_results=(),
        )

    filled_submission = execution_result()
    filled_plan = OrderPlan(
        **model_values(ready_plan(), exclude={"status", "submission", "effective_quantity"}),
        status=OrderPlanStatus.SUBMITTED,
        submission=filled_submission,
        effective_quantity=Decimal("2"),
    )
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"order_plans", "submission_results"}),
            order_plans=(filled_plan,),
            submission_results=(filled_plan.submission,),
        )


def test_session_result_rejects_invalid_order_identity_and_accepts_immediate_rejection() -> None:
    bad_order = order().model_copy(update={"account_id": "other"})
    plan = OrderPlan(**model_values(ready_plan(), exclude={"order"}), order=bad_order)
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"order_plans"}),
            order_plans=(plan,),
        )

    rejected = submitted_plan(FillStatus.REJECTED)
    value = SessionResult(
        **model_values(session_result(), exclude={"order_plans", "submission_results"}),
        order_plans=(rejected,),
        submission_results=(rejected.submission,),
    )
    assert value.submission_results[0].status is FillStatus.REJECTED


def test_manifest_contains_only_frozen_resolved_inputs() -> None:
    value = manifest()
    assert value.pit_knowledge_policy == "business-available-at/v1"
    with pytest.raises(ValidationError):
        BacktestInputManifest(**value.model_dump(), run_id="forbidden")
    with pytest.raises(ValidationError):
        BacktestInputManifest(**value.model_dump(), store=PointInTimeStore())


@pytest.mark.parametrize(
    "overrides",
    [
        {"calendar_sessions": [D1, D2]},
        {"calendar_sessions": ()},
        {"calendar_sessions": (D1,)},
        {"calendar_sessions": (D2, D1)},
        {"calendar_sessions": (D1, D1)},
        {"sessions": [session(), session(D2, US_OPEN_2, US_CLOSE_2)]},
        {"sessions": ()},
        {"sessions": (session(),)},
        {
            "sessions": (
                session(D1, US_OPEN_1, US_OPEN_2),
                session(D2, US_OPEN_2, US_CLOSE_2),
            )
        },
        {"pit_knowledge_policy": "other"},
        {"transaction_cost_bps": -1},
        {"transaction_cost_bps": Decimal("-1")},
    ],
)
def test_manifest_rejects_invalid_resolved_inputs(overrides: dict[str, object]) -> None:
    values = model_values(manifest())
    values.update(overrides)
    with pytest.raises(ValidationError):
        BacktestInputManifest(**values)


def test_backtest_result_accepts_concrete_ledger_events_and_final_lots() -> None:
    lot = AcquisitionLot(
        symbol="AAPL", acquired_session=D1, quantity=Decimal("1"), cost_basis=Decimal("100")
    )
    value = BacktestResult(**model_values(result(), exclude={"final_lots"}), final_lots=(lot,))
    assert type(value.ledger_events[0]) is CashInitialized
    assert value.final_lots == (lot,)


def test_backtest_result_accepts_symbol_major_cumulative_pit_grid() -> None:
    symbols = ("AAPL", "MSFT")
    multi_manifest = BacktestInputManifest(
        **model_values(manifest(), exclude={"instruments", "sessions"}),
        instruments=tuple(instrument(symbol) for symbol in symbols),
        sessions=(
            session(symbols=symbols),
            session(D2, US_OPEN_2, US_CLOSE_2, symbols=symbols),
        ),
    )
    value = BacktestResult(
        **model_values(result(), exclude={"manifest", "sessions"}),
        manifest=multi_manifest,
        sessions=(
            session_result(D1, symbols=symbols),
            session_result(D2, symbols=symbols),
        ),
    )

    assert tuple(
        (bar.symbol, bar.session_date) for bar in value.sessions[0].market_snapshot.bars
    ) == (("AAPL", D1), ("MSFT", D1))
    assert tuple(
        (bar.symbol, bar.session_date) for bar in value.sessions[1].market_snapshot.bars
    ) == (("AAPL", D1), ("AAPL", D2), ("MSFT", D1), ("MSFT", D2))


@pytest.mark.parametrize(
    ("first_revisions", "second_revisions"),
    [
        ((), cumulative_revisions(D2)),
        (cumulative_revisions(D1), (selected_revision(D2, US_CLOSE_2),)),
        (cumulative_revisions(D1), (selected_revision(D1, US_CLOSE_1),)),
        (
            cumulative_revisions(D1),
            (
                selected_revision(D1 - timedelta(days=1), US_CLOSE_1 - timedelta(days=1)),
                *cumulative_revisions(D2),
            ),
        ),
        (
            (selected_revision(D1, US_CLOSE_1, symbol="MSFT"),),
            cumulative_revisions(D2, symbols=("MSFT",)),
        ),
    ],
    ids=("empty", "only-current", "missing", "extra-past", "wrong-symbol"),
)
def test_backtest_result_rejects_nonexact_cumulative_pit_grid(
    first_revisions: tuple[SelectedBarRevision, ...],
    second_revisions: tuple[SelectedBarRevision, ...],
) -> None:
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"sessions"}),
            sessions=(
                session_result_with_revisions(D1, first_revisions),
                session_result_with_revisions(D2, second_revisions),
            ),
        )


def test_backtest_result_rejects_future_and_noncanonical_grid_order() -> None:
    with pytest.raises(ValidationError):
        future = selected_revision(D2, US_CLOSE_2)
        BacktestResult(
            **model_values(result(), exclude={"sessions"}),
            sessions=(
                session_result_with_revisions(D1, (future,)),
                session_result(D2),
            ),
        )

    date_major = (
        selected_revision(D1, US_CLOSE_1, symbol="AAPL"),
        selected_revision(D1, US_CLOSE_1, symbol="MSFT"),
        selected_revision(D2, US_CLOSE_2, symbol="AAPL"),
        selected_revision(D2, US_CLOSE_2, symbol="MSFT"),
    )
    with pytest.raises(ValidationError):
        session_result_with_revisions(D2, date_major)


def test_backtest_result_links_pending_submission_to_next_session_execution() -> None:
    pending_plan = submitted_plan()
    first = SessionResult(
        **model_values(session_result(D1), exclude={"order_plans", "submission_results"}),
        order_plans=(pending_plan,),
        submission_results=(pending_plan.submission,),
    )
    second = session_result_with_execution()

    value = BacktestResult(
        **model_values(result(), exclude={"sessions"}), sessions=(first, second)
    )

    assert value.sessions[1].execution_results[0].order_id == "order-1"


def test_backtest_result_does_not_carry_immediate_rejection() -> None:
    rejected_plan = submitted_plan(FillStatus.REJECTED)
    first = SessionResult(
        **model_values(session_result(D1), exclude={"order_plans", "submission_results"}),
        order_plans=(rejected_plan,),
        submission_results=(rejected_plan.submission,),
    )
    BacktestResult(
        **model_values(result(), exclude={"sessions"}), sessions=(first, session_result())
    )


def test_backtest_result_rejects_broken_cross_session_execution_chain() -> None:
    first_with_execution = session_result_with_execution(D1)
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"sessions"}),
            sessions=(first_with_execution, session_result()),
        )

    pending_plan = submitted_plan()
    first = SessionResult(
        **model_values(session_result(D1), exclude={"order_plans", "submission_results"}),
        order_plans=(pending_plan,),
        submission_results=(pending_plan.submission,),
    )
    wrong_execution = execution_result(order_id="other")
    second = SessionResult(
        **model_values(session_result_with_execution(), exclude={"execution_results"}),
        execution_results=(wrong_execution,),
    )
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"sessions"}), sessions=(first, second)
        )


def test_backtest_result_requires_final_session_to_skip_decisions_and_submissions() -> None:
    pending_plan = submitted_plan()
    final = SessionResult(
        **model_values(session_result(), exclude={"order_plans", "submission_results"}),
        order_plans=(pending_plan,),
        submission_results=(pending_plan.submission,),
    )
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"sessions"}),
            sessions=(session_result(D1), final),
        )

    final_intent = strategy_intent()
    final_with_strategy = SessionResult(
        **model_values(session_result(), exclude={"intents", "risk_decisions"}),
        intents=(final_intent,),
        risk_decisions=(
            RiskDecision(
                original_intent=final_intent,
                status=RiskDecisionStatus.APPROVED,
                approved_target_weight=Decimal("0"),
            ),
        ),
    )
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"sessions"}),
            sessions=(session_result(D1), final_with_strategy),
        )


@pytest.mark.parametrize(
    "sessions",
    [
        (session_result(D2), session_result(D2)),
        (
            SessionResult(
                **model_values(
                    session_result(D1),
                    exclude={"market_snapshot", "portfolio_snapshot"},
                ),
                market_snapshot=market_snapshot(US_CLOSE_2, session_date=D1),
                portfolio_snapshot=portfolio_snapshot(US_CLOSE_2),
            ),
            session_result(D2),
        ),
    ],
)
def test_backtest_result_requires_manifest_dates_and_close_snapshots(
    sessions: tuple[SessionResult, ...],
) -> None:
    with pytest.raises(ValidationError):
        BacktestResult(**model_values(result(), exclude={"sessions"}), sessions=sessions)


@pytest.mark.parametrize(
    "ledger_events",
    [
        (),
        (
            CashAdjusted(
                event_id="event-2",
                account_id="account-1",
                market=Market.US,
                occurred_at=US_CLOSE_1,
                amount=Decimal("1"),
                reason="correction",
            ),
        ),
    ],
)
def test_backtest_result_requires_initial_cash_ledger_event(
    ledger_events: tuple[object, ...],
) -> None:
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"ledger_events"}),
            ledger_events=ledger_events,
        )


def test_backtest_result_accepts_signed_realized_pnl() -> None:
    value = BacktestResult(
        **model_values(result(), exclude={"realized_pnl"}),
        realized_pnl=Decimal("-12.340000000000"),
    )

    assert value.realized_pnl == Decimal("-12.340000000000")


@pytest.mark.parametrize(
    "realized_pnl",
    [
        0,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1E26"),
        Decimal("0.0000000000001"),
    ],
)
def test_backtest_result_rejects_unsupported_realized_pnl(realized_pnl: object) -> None:
    with pytest.raises(ValidationError):
        BacktestResult(
            **model_values(result(), exclude={"realized_pnl"}),
            realized_pnl=realized_pnl,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"spec_fingerprint": "resolved-data-sha256:" + "a" * 64},
        {"resolved_data_fingerprint": "backtest-spec-sha256:" + "b" * 64},
        {"spec_fingerprint": "unknown-sha256:" + "a" * 64},
        {"resolved_data_fingerprint": "resolved-data-sha256:" + "B" * 64},
        {"spec_fingerprint": "backtest-spec-sha256:" + "a" * 63},
        {"resolved_data_fingerprint": "resolved-data-sha256:" + "b" * 65},
        {"spec_fingerprint": " backtest-spec-sha256:" + "a" * 64},
        {"resolved_data_fingerprint": "resolved-data-sha256:" + "b" * 64 + " "},
    ],
)
def test_backtest_result_requires_field_specific_exact_fingerprints(
    overrides: dict[str, object],
) -> None:
    values = model_values(result())
    values.update(overrides)
    with pytest.raises(ValidationError):
        BacktestResult(**values)


@pytest.mark.parametrize("field", ["sessions", "ledger_events", "final_lots"])
def test_backtest_result_requires_exact_tuples(field: str) -> None:
    values = model_values(result())
    values[field] = list(values[field])
    with pytest.raises(ValidationError):
        BacktestResult(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": " "},
        {"spec_fingerprint": "sha256:abc"},
        {"resolved_data_fingerprint": " "},
        {"realized_pnl": 0},
        {
            "final_snapshot": PortfolioSnapshot(
                **portfolio_snapshot().model_dump(exclude={"account_id"}), account_id="other"
            )
        },
        {"sessions": (session_result(D1),)},
    ],
)
def test_backtest_result_rejects_identity_and_final_consistency_errors(
    overrides: dict[str, object],
) -> None:
    values = model_values(result())
    values.update(overrides)
    with pytest.raises(ValidationError):
        BacktestResult(**values)


def all_models() -> tuple[BaseModel, ...]:
    return (
        session(),
        spec(),
        ready_plan(),
        session_result(),
        manifest(),
        result(),
    )


def test_all_models_are_frozen_extra_forbidden_strict_and_copy_update_safe() -> None:
    for model in all_models():
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["strict"] is True
        assert model.model_config["revalidate_instances"] == "always"
        with pytest.raises(ValidationError):
            model.unexpected = True
        with pytest.raises(TypeError):
            model.model_copy(update={"unexpected": True})
        with pytest.raises(TypeError):
            model.model_copy(update={})
        with pytest.raises(TypeError):
            model.copy(update={"unexpected": True})
        with pytest.raises(TypeError):
            model.copy(update={})
        with pytest.raises(TypeError):
            model.copy(include={})
        with pytest.raises(TypeError):
            model.copy(exclude=set())
        with pytest.warns(DeprecationWarning):
            assert model.copy() == model
        with pytest.warns(DeprecationWarning):
            assert model.copy(deep=True) == model
        assert model.model_copy() == model
        assert model.model_copy(deep=True) == model

    baseline = result()
    with pytest.warns(DeprecationWarning):
        deprecated_deep_copy = baseline.copy(deep=True)
    modern_deep_copy = baseline.model_copy(deep=True)
    assert deprecated_deep_copy.manifest is not baseline.manifest
    assert modern_deep_copy.manifest is not baseline.manifest


def test_all_models_forbid_subclasses() -> None:
    for model_type in (
        BacktestSession,
        BacktestSpec,
        OrderPlan,
        SessionResult,
        BacktestInputManifest,
        BacktestResult,
    ):
        with pytest.raises(TypeError, match="does not support subclasses"):
            type("MutableBoundary", (model_type,), {"model_config": ConfigDict(frozen=False)})


def test_nested_model_construct_missing_field_raises_validation_error() -> None:
    incomplete_bar = Bar.model_construct(**open_bar().model_dump(exclude={"high"}))

    with pytest.raises(ValidationError):
        BacktestSession(
            session_date=D1,
            open_at=US_OPEN_1,
            close_at=US_CLOSE_1,
            open_bars=(incomplete_bar,),
            cn_session_states=(),
        )


def test_nested_polluted_objects_are_fully_revalidated() -> None:
    polluted_bar = Bar.model_construct(
        **open_bar().model_dump(exclude={"volume"}), volume=Decimal("1")
    )
    with pytest.raises(ValidationError):
        BacktestSession(
            session_date=D1,
            open_at=US_OPEN_1,
            close_at=US_CLOSE_1,
            open_bars=(polluted_bar,),
            cn_session_states=(),
        )

    polluted_instrument = Instrument.model_construct(
        **instrument().model_dump(exclude={"currency"}), currency=Currency.CNY
    )
    with pytest.raises(ValidationError):
        BacktestSpec(
            **model_values(spec(), exclude={"instruments"}), instruments=(polluted_instrument,)
        )

    polluted_order = OrderIntent.model_construct(
        **order().model_dump(exclude={"quantity"}), quantity=Decimal("-1")
    )
    with pytest.raises(ValidationError):
        OrderPlan(**model_values(ready_plan(), exclude={"order"}), order=polluted_order)

    polluted_portfolio = PortfolioSnapshot.model_construct(
        **portfolio_snapshot().model_dump(exclude={"nav"}), nav=Decimal("999")
    )
    with pytest.raises(ValidationError):
        SessionResult(
            **model_values(session_result(), exclude={"portfolio_snapshot"}),
            portfolio_snapshot=polluted_portfolio,
        )


def test_nested_subclasses_are_rejected() -> None:
    class MutableBar(Bar):
        model_config = ConfigDict(frozen=False)

    class MutableFill(Fill):
        model_config = ConfigDict(frozen=False)

    with pytest.raises(ValidationError):
        BacktestSession(
            session_date=D1,
            open_at=US_OPEN_1,
            close_at=US_CLOSE_1,
            open_bars=(MutableBar(**open_bar().model_dump()),),
            cn_session_states=(),
        )
    with pytest.raises(ValidationError):
        OrderPlan(
            **model_values(ready_plan(), exclude={"status", "submission", "effective_quantity"}),
            status=OrderPlanStatus.SUBMITTED,
            submission=MutableFill(**submission().model_dump()),
            effective_quantity=Decimal("2"),
        )


def test_validation_does_not_change_hostile_ambient_decimal_context() -> None:
    baseline = BacktestResult(
        **model_values(result(), exclude={"realized_pnl"}),
        realized_pnl=Decimal("-12.340000000000"),
    )
    with localcontext() as context:
        context.prec = 1
        context.Emin = 0
        context.Emax = 0
        context.rounding = ROUND_UP
        before = repr(context)
        value = BacktestResult(**model_values(baseline))
        after = repr(context)

    assert value.realized_pnl == Decimal("-12.340000000000")
    assert after == before
