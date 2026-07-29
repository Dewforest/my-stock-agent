from __future__ import annotations

from datetime import UTC, date
from decimal import ROUND_DOWN, Decimal, DecimalException, localcontext
from typing import TypeVar

from pydantic import BaseModel

from stock_agent.audit import tagged_sha256
from stock_agent.backtest.models import OrderPlan, OrderPlanSource, OrderPlanStatus
from stock_agent.domain import PortfolioSnapshot, Position, Side, StrategyIntent
from stock_agent.execution import Fill, OrderIntent
from stock_agent.risk import RiskDecision, RiskDecisionStatus, RiskReductionTarget
from stock_agent.strategies import MarketSnapshot

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_QUANTITY_QUANTUM = Decimal("0.000000000001")
_MAX_QUANTITY = Decimal("1E26")


def _model_values(model: BaseModel) -> dict[str, object]:
    try:
        return {name: getattr(model, name) for name in model.__class__.model_fields}
    except AttributeError as error:
        raise ValueError("nested model is missing a required field") from error


def _revalidate_exact(value: object, expected: type[_ModelT], name: str) -> _ModelT:
    if type(value) is not expected:
        raise TypeError(f"{name} must be exactly {expected.__name__}")
    with localcontext() as context:
        context.prec = max(context.prec, 256)
        context.Emin = min(context.Emin, -999999)
        context.Emax = max(context.Emax, 999999)
        return expected(**_model_values(value))


def _validate_inputs(
    *,
    run_id: object,
    decision_session: object,
    strategy_id: object,
    market_snapshot: object,
    portfolio: object,
    risk_decisions: object,
    portfolio_reduction: object,
) -> tuple[
    str,
    date,
    str,
    MarketSnapshot,
    PortfolioSnapshot,
    tuple[RiskDecision, ...],
    RiskReductionTarget | None,
]:
    if type(run_id) is not str or not run_id.strip():
        raise TypeError("run_id must be a nonblank string")
    if type(decision_session) is not date:
        raise TypeError("decision_session must be a plain date")
    if type(strategy_id) is not str or not strategy_id.strip():
        raise TypeError("strategy_id must be a nonblank string")
    snapshot = _revalidate_exact(market_snapshot, MarketSnapshot, "market_snapshot")
    rebuilt_portfolio = _revalidate_exact(portfolio, PortfolioSnapshot, "portfolio")
    if type(risk_decisions) is not tuple:
        raise TypeError("risk_decisions must be an exact tuple")
    decisions = tuple(
        _revalidate_exact(item, RiskDecision, "risk decision") for item in risk_decisions
    )
    reduction = (
        None
        if portfolio_reduction is None
        else _revalidate_exact(portfolio_reduction, RiskReductionTarget, "portfolio_reduction")
    )
    if snapshot.market is not rebuilt_portfolio.market:
        raise ValueError("market snapshot and portfolio markets must match")
    if snapshot.as_of.astimezone(UTC) != rebuilt_portfolio.as_of.astimezone(UTC):
        raise ValueError("market snapshot and portfolio as_of values must match")
    symbols: list[str] = []
    for item in decisions:
        original = _revalidate_exact(item.original_intent, StrategyIntent, "original intent")
        if original.strategy_id != strategy_id.strip():
            raise ValueError("risk decision strategy_id must match strategy_id")
        if original.market is not snapshot.market:
            raise ValueError("risk decision market must match market snapshot")
        if original.as_of.astimezone(UTC) != snapshot.as_of.astimezone(UTC):
            raise ValueError("risk decision as_of must match market snapshot")
        if item.risk_reduction != reduction:
            raise ValueError("risk decision reduction must match portfolio_reduction")
        symbols.append(original.symbol)
    if len(symbols) != len(set(symbols)):
        raise ValueError("risk decision symbols must be unique")
    return (
        run_id.strip(),
        decision_session,
        strategy_id.strip(),
        snapshot,
        rebuilt_portfolio,
        decisions,
        reduction,
    )


def _order_id(
    run_id: str, decision_session: date, strategy_id: str, symbol: str, side: Side
) -> str:
    return tagged_sha256(
        "order", (run_id, decision_session.isoformat(), strategy_id, symbol, side.value)
    )


def _ready_plan(
    *,
    run_id: str,
    decision_session: date,
    strategy_id: str,
    portfolio: PortfolioSnapshot,
    source: OrderPlanSource,
    symbol: str,
    target_weight: Decimal,
    side: Side,
    raw_quantity: Decimal,
    submitted_quantity: Decimal,
) -> OrderPlan:
    order = OrderIntent(
        order_id=_order_id(run_id, decision_session, strategy_id, symbol, side),
        account_id=portfolio.account_id,
        symbol=symbol,
        market=portfolio.market,
        side=side,
        quantity=submitted_quantity,
    )
    return OrderPlan(
        status=OrderPlanStatus.READY,
        source=source,
        symbol=symbol,
        target_weight=target_weight,
        raw_quantity=raw_quantity,
        submitted_quantity=submitted_quantity,
        effective_quantity=None,
        order=order,
        submission=None,
        reason=None,
    )


def _current_close_map(
    snapshot: MarketSnapshot, decision_session: date
) -> dict[str, Decimal]:
    return {
        item.symbol: item.close
        for item in snapshot.bars
        if item.session_date == decision_session
    }


def _terminal_plan(
    *,
    status: OrderPlanStatus,
    source: OrderPlanSource,
    symbol: str,
    target_weight: Decimal,
    reason: str,
    raw_quantity: Decimal | None = None,
) -> OrderPlan:
    return OrderPlan(
        status=status,
        source=source,
        symbol=symbol,
        target_weight=target_weight,
        raw_quantity=raw_quantity,
        submitted_quantity=None,
        effective_quantity=None,
        order=None,
        submission=None,
        reason=reason,
    )


def _strategy_plan(
    *,
    run_id: str,
    decision_session: date,
    strategy_id: str,
    portfolio: PortfolioSnapshot,
    decision: RiskDecision,
    close: Decimal,
    position: Position | None,
) -> OrderPlan:
    intent = decision.original_intent
    source = OrderPlanSource.STRATEGY
    if decision.status is RiskDecisionStatus.REJECTED:
        detail = f": {'; '.join(decision.reasons)}" if decision.reasons else ""
        return _terminal_plan(
            status=OrderPlanStatus.REJECTED,
            source=source,
            symbol=intent.symbol,
            target_weight=intent.target_weight,
            reason=f"risk decision rejected{detail}",
        )
    target_weight = decision.approved_target_weight
    if target_weight is None:
        return _terminal_plan(
            status=OrderPlanStatus.REJECTED,
            source=source,
            symbol=intent.symbol,
            target_weight=intent.target_weight,
            reason="approved risk decision has no target weight",
        )
    if intent.side is Side.HOLD:
        return _terminal_plan(
            status=OrderPlanStatus.SKIPPED,
            source=source,
            symbol=intent.symbol,
            target_weight=target_weight,
            reason="HOLD intent has no order",
        )
    if intent.side is Side.SELL:
        if position is None:
            return _terminal_plan(
                status=OrderPlanStatus.SKIPPED,
                source=source,
                symbol=intent.symbol,
                target_weight=target_weight,
                raw_quantity=Decimal(0),
                reason="SELL intent has no held quantity",
            )
        return _ready_plan(
            run_id=run_id,
            decision_session=decision_session,
            strategy_id=strategy_id,
            portfolio=portfolio,
            source=source,
            symbol=intent.symbol,
            target_weight=target_weight,
            side=Side.SELL,
            raw_quantity=position.quantity,
            submitted_quantity=position.quantity,
        )

    current_notional = Decimal(0) if position is None else position.market_value
    try:
        with localcontext() as context:
            context.prec = max(context.prec, 256)
            context.Emin = min(context.Emin, -999999)
            context.Emax = max(context.Emax, 999999)
            context.rounding = ROUND_DOWN
            delta = target_weight * portfolio.nav - current_notional
            if delta == 0:
                return _terminal_plan(
                    status=OrderPlanStatus.SKIPPED,
                    source=source,
                    symbol=intent.symbol,
                    target_weight=target_weight,
                    raw_quantity=Decimal(0),
                    reason="target delta is zero",
                )
            if intent.side is Side.BUY and delta < 0:
                return _terminal_plan(
                    status=OrderPlanStatus.REJECTED,
                    source=source,
                    symbol=intent.symbol,
                    target_weight=target_weight,
                    reason="BUY requires a positive target delta",
                )
            if intent.side is Side.REDUCE and delta > 0:
                return _terminal_plan(
                    status=OrderPlanStatus.REJECTED,
                    source=source,
                    symbol=intent.symbol,
                    target_weight=target_weight,
                    reason="REDUCE requires a negative target delta",
                )
            raw = delta.copy_abs() / close
            if not raw.is_finite() or raw >= _MAX_QUANTITY:
                raise ArithmeticError("calculated quantity is outside the supported range")
            submitted = raw.quantize(_QUANTITY_QUANTUM, rounding=ROUND_DOWN)
    except (ArithmeticError, DecimalException):
        return _terminal_plan(
            status=OrderPlanStatus.REJECTED,
            source=source,
            symbol=intent.symbol,
            target_weight=target_weight,
            reason="planning arithmetic failed",
        )
    if submitted == 0:
        return _terminal_plan(
            status=OrderPlanStatus.SKIPPED,
            source=source,
            symbol=intent.symbol,
            target_weight=target_weight,
            raw_quantity=raw,
            reason="calculated quantity is zero after 12-place floor",
        )
    executable_side = Side.BUY if intent.side is Side.BUY else Side.SELL
    return _ready_plan(
        run_id=run_id,
        decision_session=decision_session,
        strategy_id=strategy_id,
        portfolio=portfolio,
        source=source,
        symbol=intent.symbol,
        target_weight=target_weight,
        side=executable_side,
        raw_quantity=raw,
        submitted_quantity=submitted,
    )


def _reduction_plan(
    *,
    run_id: str,
    decision_session: date,
    strategy_id: str,
    portfolio: PortfolioSnapshot,
    position: Position,
    close: Decimal,
    decision: RiskDecision | None,
) -> OrderPlan:
    source = OrderPlanSource.RISK_REDUCTION
    try:
        with localcontext() as context:
            context.prec = max(context.prec, 256)
            context.Emin = min(context.Emin, -999999)
            context.Emax = max(context.Emax, 999999)
            context.rounding = ROUND_DOWN
            half_weight = (
                Decimal(0)
                if portfolio.nav == 0
                else (position.market_value / portfolio.nav / Decimal(2)).quantize(
                    _QUANTITY_QUANTUM, rounding=ROUND_DOWN
                )
            )
            approved = (
                decision.approved_target_weight
                if decision is not None and decision.status is not RiskDecisionStatus.REJECTED
                else None
            )
            target_weight = (
                approved if approved is not None and approved < half_weight else half_weight
            )
            delta = position.market_value - target_weight * portfolio.nav
            if delta <= 0:
                return _terminal_plan(
                    status=OrderPlanStatus.SKIPPED,
                    source=source,
                    symbol=position.symbol,
                    target_weight=target_weight,
                    raw_quantity=Decimal(0),
                    reason="risk reduction target delta is zero",
                )
            raw = delta / close
            if not raw.is_finite() or raw >= _MAX_QUANTITY:
                raise ArithmeticError
            submitted = raw.quantize(_QUANTITY_QUANTUM, rounding=ROUND_DOWN)
    except (ArithmeticError, DecimalException):
        safe_target = Decimal(0)
        return _terminal_plan(
            status=OrderPlanStatus.REJECTED,
            source=source,
            symbol=position.symbol,
            target_weight=safe_target,
            reason="risk reduction planning arithmetic failed",
        )
    if submitted == 0:
        return _terminal_plan(
            status=OrderPlanStatus.SKIPPED,
            source=source,
            symbol=position.symbol,
            target_weight=target_weight,
            raw_quantity=raw,
            reason="calculated quantity is zero after 12-place floor",
        )
    return _ready_plan(
        run_id=run_id,
        decision_session=decision_session,
        strategy_id=strategy_id,
        portfolio=portfolio,
        source=source,
        symbol=position.symbol,
        target_weight=target_weight,
        side=Side.SELL,
        raw_quantity=raw,
        submitted_quantity=submitted,
    )


def plan_orders(
    *,
    run_id: str,
    decision_session: date,
    strategy_id: str,
    market_snapshot: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    risk_decisions: tuple[RiskDecision, ...],
    portfolio_reduction: RiskReductionTarget | None,
) -> tuple[OrderPlan, ...]:
    (
        clean_run_id,
        clean_session,
        clean_strategy_id,
        snapshot,
        rebuilt_portfolio,
        decisions,
        reduction,
    ) = _validate_inputs(
        run_id=run_id,
        decision_session=decision_session,
        strategy_id=strategy_id,
        market_snapshot=market_snapshot,
        portfolio=portfolio,
        risk_decisions=risk_decisions,
        portfolio_reduction=portfolio_reduction,
    )
    closes = _current_close_map(snapshot, clean_session)
    positions = {item.symbol: item for item in rebuilt_portfolio.positions}
    required = {item.original_intent.symbol for item in decisions} | set(positions)
    missing = sorted(required - closes.keys())
    if missing:
        raise ValueError(f"missing current close for symbols: {', '.join(missing)}")
    plans: list[OrderPlan] = []
    if reduction is not None:
        planned_holdings: set[str] = set()
        for item in decisions:
            symbol = item.original_intent.symbol
            position = positions.get(symbol)
            if position is not None:
                plans.append(
                    _reduction_plan(
                        run_id=clean_run_id,
                        decision_session=clean_session,
                        strategy_id=clean_strategy_id,
                        portfolio=rebuilt_portfolio,
                        position=position,
                        close=closes[symbol],
                        decision=item,
                    )
                )
                planned_holdings.add(symbol)
            else:
                plans.append(
                    _strategy_plan(
                        run_id=clean_run_id,
                        decision_session=clean_session,
                        strategy_id=clean_strategy_id,
                        portfolio=rebuilt_portfolio,
                        decision=item,
                        close=closes[symbol],
                        position=None,
                    )
                )
        for symbol in sorted(set(positions) - planned_holdings):
            plans.append(
                _reduction_plan(
                    run_id=clean_run_id,
                    decision_session=clean_session,
                    strategy_id=clean_strategy_id,
                    portfolio=rebuilt_portfolio,
                    position=positions[symbol],
                    close=closes[symbol],
                    decision=None,
                )
            )
        return tuple(plans)

    for item in decisions:
        plans.append(
            _strategy_plan(
                run_id=clean_run_id,
                decision_session=clean_session,
                strategy_id=clean_strategy_id,
                portfolio=rebuilt_portfolio,
                decision=item,
                close=closes[item.original_intent.symbol],
                position=positions.get(item.original_intent.symbol),
            )
        )
    return tuple(plans)


def record_submission(plan: OrderPlan, submission: Fill) -> OrderPlan:
    rebuilt_plan = _revalidate_exact(plan, OrderPlan, "plan")
    if rebuilt_plan.status is not OrderPlanStatus.READY:
        raise TypeError("plan must be READY before recording a submission")
    rebuilt_submission = _revalidate_exact(submission, Fill, "submission")
    if (
        rebuilt_plan.submitted_quantity is None
        or rebuilt_submission.requested_quantity > rebuilt_plan.submitted_quantity
    ):
        raise ValueError("submission may not increase the planned quantity")
    return OrderPlan(
        status=OrderPlanStatus.SUBMITTED,
        source=rebuilt_plan.source,
        symbol=rebuilt_plan.symbol,
        target_weight=rebuilt_plan.target_weight,
        raw_quantity=rebuilt_plan.raw_quantity,
        submitted_quantity=rebuilt_plan.submitted_quantity,
        effective_quantity=rebuilt_submission.requested_quantity,
        order=rebuilt_plan.order,
        submission=rebuilt_submission,
        reason=None,
    )
