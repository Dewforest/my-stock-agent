from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, localcontext

import pytest
from pydantic import ConfigDict, ValidationError

import stock_agent.risk as risk
from stock_agent.domain import (
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Side,
    StrategyIntent,
)
from stock_agent.risk import (
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskEngine,
    RiskReductionTarget,
)


def test_risk_public_contract_is_importable() -> None:
    assert RiskDecisionStatus.APPROVED == "APPROVED"
    assert RiskDecisionStatus.CLAMPED == "CLAMPED"
    assert RiskDecisionStatus.REJECTED == "REJECTED"
    assert all(
        risk_type is not None
        for risk_type in (RiskContext, RiskDecision, RiskEngine, RiskReductionTarget)
    )


def full_cash_portfolio(market: Market = Market.US) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_id="account-1",
        market=market,
        cash=Decimal("1000"),
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        as_of=datetime(2026, 7, 28, 12, tzinfo=UTC),
    )


def us_instrument(symbol: str = "AAPL") -> Instrument:
    return Instrument(
        symbol=symbol,
        market=Market.US,
        currency=Currency.USD,
        sector="Technology",
    )


def test_risk_context_accepts_a_full_cash_portfolio() -> None:
    context = RiskContext(
        portfolio=full_cash_portfolio(),
        instruments=(us_instrument(),),
        day_start_available_cash=Decimal("1000.00"),
        new_position_notional_committed_today=Decimal("0.000"),
    )

    assert context.portfolio.cash == Decimal("1000")
    assert context.instruments == (us_instrument(),)
    assert context.day_start_available_cash == Decimal("1000.00")
    assert context.new_position_notional_committed_today == Decimal("0.000")


def context_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "portfolio": full_cash_portfolio(),
        "instruments": (us_instrument(),),
        "day_start_available_cash": Decimal("1000"),
        "new_position_notional_committed_today": Decimal("0"),
    }
    values.update(overrides)
    return values


def test_risk_context_requires_tuple_instruments() -> None:
    with pytest.raises(ValidationError):
        RiskContext(**context_values(instruments=[us_instrument()]))


def test_risk_context_rejects_portfolio_subclasses() -> None:
    class MutablePortfolioSnapshot(PortfolioSnapshot):
        model_config = ConfigDict(frozen=False)

    portfolio = MutablePortfolioSnapshot(**full_cash_portfolio().model_dump())

    with pytest.raises(ValidationError):
        RiskContext(**context_values(portfolio=portfolio))


def test_risk_context_rejects_tuple_subclasses() -> None:
    class MutableTuple(tuple[Instrument, ...]):
        pass

    with pytest.raises(ValidationError):
        RiskContext(**context_values(instruments=MutableTuple((us_instrument(),))))


def test_risk_context_rejects_instrument_subclasses() -> None:
    class MutableInstrument(Instrument):
        model_config = ConfigDict(frozen=False)

    instrument = MutableInstrument(**us_instrument().model_dump())

    with pytest.raises(ValidationError):
        RiskContext(**context_values(instruments=(instrument,)))


@pytest.mark.parametrize("field", [
    "day_start_available_cash",
    "new_position_notional_committed_today",
])
@pytest.mark.parametrize("value", [0, 0.0, "0", False])
def test_risk_context_requires_strict_decimal_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RiskContext(**context_values(**{field: value}))


@pytest.mark.parametrize("field", [
    "day_start_available_cash",
    "new_position_notional_committed_today",
])
@pytest.mark.parametrize(
    "value",
    [
        Decimal("-0.01"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_risk_context_rejects_invalid_decimal_fields(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        RiskContext(**context_values(**{field: value}))


def test_risk_context_rejects_duplicate_instrument_symbols() -> None:
    with pytest.raises(ValidationError):
        RiskContext(**context_values(instruments=(us_instrument("AAPL"), us_instrument("aapl"))))


def test_risk_context_rejects_cross_market_instruments() -> None:
    cn_instrument = Instrument(
        symbol="600519",
        market=Market.CN,
        currency=Currency.CNY,
        sector="Consumer Staples",
    )
    with pytest.raises(ValidationError):
        RiskContext(**context_values(instruments=(cn_instrument,)))


def test_risk_context_allows_incomplete_metadata_and_commitment_over_budget() -> None:
    context = RiskContext(
        **context_values(
            instruments=(),
            new_position_notional_committed_today=Decimal("2000"),
        )
    )

    assert context.instruments == ()
    assert context.new_position_notional_committed_today == Decimal("2000")


def test_risk_context_is_frozen_and_forbids_extra_fields() -> None:
    context = RiskContext(**context_values())
    with pytest.raises(ValidationError):
        context.day_start_available_cash = Decimal("1")
    with pytest.raises(ValidationError):
        RiskContext(**context_values(unknown=True))


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("1"), Decimal("0.2500")])
def test_risk_reduction_target_accepts_unit_interval_boundaries(value: Decimal) -> None:
    target = RiskReductionTarget(
        current_gross_exposure=value,
        target_gross_exposure=value,
    )

    assert target.current_gross_exposure is value
    assert target.target_gross_exposure is value
    assert target.review_required is True


@pytest.mark.parametrize("field", ["current_gross_exposure", "target_gross_exposure"])
@pytest.mark.parametrize(
    "value",
    [
        0,
        0.0,
        "0",
        False,
        Decimal("-0.01"),
        Decimal("1.01"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_risk_reduction_target_rejects_invalid_decimals(field: str, value: object) -> None:
    values: dict[str, object] = {
        "current_gross_exposure": Decimal("0.5"),
        "target_gross_exposure": Decimal("0.25"),
    }
    values[field] = value
    with pytest.raises(ValidationError):
        RiskReductionTarget(**values)


@pytest.mark.parametrize("review_required", [False, 1, Decimal("1"), "true"])
def test_risk_reduction_target_requires_exact_true(review_required: object) -> None:
    with pytest.raises(ValidationError):
        RiskReductionTarget(
            current_gross_exposure=Decimal("0.5"),
            target_gross_exposure=Decimal("0.25"),
            review_required=review_required,
        )


def test_risk_reduction_target_is_frozen_and_forbids_extra_fields() -> None:
    target = RiskReductionTarget(
        current_gross_exposure=Decimal("0.5"),
        target_gross_exposure=Decimal("0.25"),
    )
    with pytest.raises(ValidationError):
        target.target_gross_exposure = Decimal("0.1")
    with pytest.raises(ValidationError):
        RiskReductionTarget(
            current_gross_exposure=Decimal("0.5"),
            target_gross_exposure=Decimal("0.25"),
            unknown=True,
        )


def buy_intent() -> StrategyIntent:
    return StrategyIntent(
        strategy_id="momentum-v1",
        symbol="AAPL",
        market=Market.US,
        side=Side.BUY,
        target_weight=Decimal("0.4"),
        confidence=80,
        as_of=datetime(2026, 7, 28, 12, tzinfo=UTC),
        thesis="Earnings momentum",
        invalidation="Guidance cut",
    )


def decision_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "original_intent": buy_intent(),
        "status": RiskDecisionStatus.APPROVED,
        "approved_target_weight": Decimal("0.4000"),
    }
    values.update(overrides)
    return values


def test_risk_decision_rejects_strategy_intent_subclasses() -> None:
    class MutableStrategyIntent(StrategyIntent):
        model_config = ConfigDict(frozen=False)

    intent = MutableStrategyIntent(**buy_intent().model_dump())

    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(original_intent=intent))


@pytest.mark.parametrize("status", [RiskDecisionStatus.APPROVED, RiskDecisionStatus.CLAMPED])
def test_non_rejected_decision_requires_and_preserves_target(status: RiskDecisionStatus) -> None:
    intent = buy_intent()
    target = Decimal("0.4000")
    decision = RiskDecision(
        **decision_values(
            original_intent=intent,
            status=status,
            approved_target_weight=target,
            rule_ids=(" RULE_1 ",),
            reasons=(" reason one ",),
        )
    )

    assert decision.original_intent is intent
    assert decision.approved_target_weight is target
    assert decision.rule_ids == ("RULE_1",)
    assert decision.reasons == ("reason one",)
    assert decision.risk_reduction is None


def test_rejected_decision_requires_none_target() -> None:
    decision = RiskDecision(
        **decision_values(
            status=RiskDecisionStatus.REJECTED,
            approved_target_weight=None,
            rule_ids=("RISK_BLOCK",),
            reasons=("blocked",),
        )
    )
    assert decision.approved_target_weight is None


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (RiskDecisionStatus.REJECTED, Decimal("0")),
        (RiskDecisionStatus.APPROVED, None),
        (RiskDecisionStatus.CLAMPED, None),
    ],
)
def test_risk_decision_rejects_status_target_mismatch(
    status: RiskDecisionStatus, target: Decimal | None
) -> None:
    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(status=status, approved_target_weight=target))


@pytest.mark.parametrize(
    "target",
    [
        0,
        0.0,
        "0",
        False,
        Decimal("-0.01"),
        Decimal("1.01"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_risk_decision_rejects_invalid_target(target: object) -> None:
    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(approved_target_weight=target))


@pytest.mark.parametrize("field", ["rule_ids", "reasons"])
def test_risk_decision_requires_tuple_text_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(**{field: ["RULE"]}))


@pytest.mark.parametrize(
    ("rule_ids", "reasons"),
    [
        (("",), ("reason",)),
        (("   ",), ("reason",)),
        (("RULE",), ("",)),
        (("RULE",), ("   ",)),
        ((" RULE ", "RULE"), ("one", "two")),
        (("RULE",), ()),
        ((), ("reason",)),
    ],
)
def test_risk_decision_rejects_invalid_rule_reason_pairs(
    rule_ids: tuple[str, ...], reasons: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(rule_ids=rule_ids, reasons=reasons))


def test_risk_decision_defaults_to_empty_rule_reason_tuples() -> None:
    decision = RiskDecision(**decision_values())
    assert decision.rule_ids == ()
    assert decision.reasons == ()


def test_risk_decision_accepts_optional_reduction_target() -> None:
    reduction = RiskReductionTarget(
        current_gross_exposure=Decimal("0.6"),
        target_gross_exposure=Decimal("0.3"),
    )
    decision = RiskDecision(**decision_values(risk_reduction=reduction))
    assert decision.risk_reduction is reduction


def test_risk_decision_is_frozen_and_forbids_extra_fields() -> None:
    decision = RiskDecision(**decision_values())
    with pytest.raises(ValidationError):
        decision.status = RiskDecisionStatus.REJECTED
    with pytest.raises(ValidationError):
        RiskDecision(**decision_values(unknown=True))


def risk_model_instances() -> tuple[RiskContext, RiskReductionTarget, RiskDecision]:
    return (
        RiskContext(**context_values()),
        RiskReductionTarget(
            current_gross_exposure=Decimal("0.5"),
            target_gross_exposure=Decimal("0.25"),
        ),
        RiskDecision(**decision_values()),
    )


@pytest.mark.parametrize("deep", [False, True])
def test_risk_model_copies_remain_frozen(deep: bool) -> None:
    for model in risk_model_instances():
        copied = model.model_copy(deep=deep)

        assert copied == model
        with pytest.raises(ValidationError):
            copied.unexpected_state = True


def test_risk_models_allow_empty_copy_updates() -> None:
    for model in risk_model_instances():
        assert model.model_copy(update={}) == model


def test_risk_models_reject_nonempty_copy_updates() -> None:
    for model in risk_model_instances():
        with pytest.raises(TypeError):
            model.model_copy(update={"unexpected_state": True})


def test_decimal_validation_is_safe_under_hostile_ambient_context() -> None:
    portfolio = full_cash_portfolio()
    intent = buy_intent()
    invalid_contexts = [
        context_values(day_start_available_cash=Decimal("sNaN")),
        context_values(new_position_notional_committed_today=Decimal("Infinity")),
    ]
    invalid_reductions = [
        {
            "current_gross_exposure": Decimal("NaN"),
            "target_gross_exposure": Decimal("0.25"),
        },
        {
            "current_gross_exposure": Decimal("0.5"),
            "target_gross_exposure": Decimal("sNaN"),
        },
    ]
    invalid_decisions = [
        decision_values(original_intent=intent, approved_target_weight=Decimal("sNaN")),
        decision_values(original_intent=intent, approved_target_weight=Decimal("Infinity")),
    ]

    with localcontext() as decimal_context:
        decimal_context.prec = 1
        decimal_context.Emin = 0
        decimal_context.Emax = 0
        decimal_context.rounding = ROUND_UP
        for signal in decimal_context.traps:
            decimal_context.traps[signal] = True

        valid_context = RiskContext(
            portfolio=portfolio,
            instruments=(),
            day_start_available_cash=Decimal("1000.00"),
            new_position_notional_committed_today=Decimal("2000.000"),
        )
        valid_reduction = RiskReductionTarget(
            current_gross_exposure=Decimal("1.000"),
            target_gross_exposure=Decimal("0.2500"),
        )
        valid_decision = RiskDecision(
            **decision_values(
                original_intent=intent,
                approved_target_weight=Decimal("0.4000"),
            )
        )
        for values in invalid_contexts:
            with pytest.raises(ValidationError):
                RiskContext(**values)
        for values in invalid_reductions:
            with pytest.raises(ValidationError):
                RiskReductionTarget(**values)
        for values in invalid_decisions:
            with pytest.raises(ValidationError):
                RiskDecision(**values)

    assert valid_context.day_start_available_cash.as_tuple().exponent == -2
    assert valid_reduction.target_gross_exposure.as_tuple().exponent == -4
    assert valid_decision.approved_target_weight is not None
    assert valid_decision.approved_target_weight.as_tuple().exponent == -4


def test_risk_package_exports_exact_public_contract() -> None:
    assert risk.__all__ == [
        "RiskContext",
        "RiskDecision",
        "RiskDecisionStatus",
        "RiskEngine",
        "RiskReductionTarget",
    ]


def test_risk_engine_cannot_store_state() -> None:
    engine = RiskEngine()
    assert RiskEngine.__slots__ == ()
    assert not hasattr(engine, "__dict__")
    with pytest.raises(AttributeError):
        engine.state = "forbidden"
