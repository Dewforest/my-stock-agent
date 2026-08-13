from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from stock_agent.audit.canonical import canonical_decimal, tagged_sha256
from stock_agent.domain import Side, StrategyIntent
from stock_agent.risk.engine import RiskContext, RiskDecision, RiskDecisionStatus, RiskEngine
from stock_agent.runtime.state import PendingOrder, RiskResultEnvelope
from stock_agent.runtime.store import RuntimeStore


def order_id_for(run_id: str, symbol: str, side: Side, target_weight: Decimal, ordinal: int) -> str:
    return tagged_sha256(
        "paper-order",
        (run_id, symbol, side.value, canonical_decimal(target_weight), ordinal),
    )


def order_set_digest_for(run_id: str, orders: tuple[PendingOrder, ...]) -> str:
    return tagged_sha256(
        "paper-order-set",
        (
            run_id,
            [
                [
                    order.order_id,
                    order.symbol,
                    order.side.value,
                    canonical_decimal(order.target_weight),
                    order.intended_session_date.isoformat(),
                    order.ordinal,
                ]
                for order in orders
            ],
        ),
    )


def build_pending_orders(
    run_id: str,
    decisions: tuple[RiskDecision, ...],
    intended_session_date: date,
) -> tuple[PendingOrder, ...]:
    if type(decisions) is not tuple or not decisions:
        raise ValueError("decisions must be a nonempty exact tuple")
    if any(type(item) is not RiskDecision for item in decisions):
        raise TypeError("decisions must contain exact RiskDecision values")
    if type(intended_session_date) is not date:
        raise TypeError("intended_session_date must be a plain date")

    orders: list[PendingOrder] = []
    for ordinal, decision in enumerate(
        sorted(decisions, key=lambda item: item.original_intent.symbol), start=1
    ):
        if decision.status not in (RiskDecisionStatus.APPROVED, RiskDecisionStatus.CLAMPED):
            continue
        if decision.approved_target_weight is None:
            continue
        intent = decision.original_intent
        order = PendingOrder(
            order_id=order_id_for(
                run_id,
                intent.symbol,
                intent.side,
                decision.approved_target_weight,
                ordinal,
            ),
            run_id=run_id,
            symbol=intent.symbol,
            market=intent.market,
            side=intent.side,
            target_weight=decision.approved_target_weight,
            intended_session_date=intended_session_date,
            ordinal=ordinal,
        )
        orders.append(order)
    return tuple(orders)


def build_risk_envelopes(
    run_id: str, decisions: tuple[RiskDecision, ...]
) -> tuple[RiskResultEnvelope, ...]:
    return tuple(
        RiskResultEnvelope(
            run_id=run_id,
            symbol=decision.original_intent.symbol,
            status=decision.status.value,
            approved_target_weight=decision.approved_target_weight,
            rule_ids=decision.rule_ids,
            reasons=decision.reasons,
        )
        for decision in decisions
    )


def persist_risk_and_orders(
    *,
    store: RuntimeStore,
    run_id: str,
    decisions: tuple[RiskDecision, ...],
    intended_session_date: date,
    now: datetime,
) -> tuple[PendingOrder, ...]:
    if type(store) is not RuntimeStore:
        raise TypeError("store must be exactly RuntimeStore")
    envelopes = build_risk_envelopes(run_id, decisions)
    orders = build_pending_orders(run_id, decisions, intended_session_date)
    digest = order_set_digest_for(run_id, orders)

    store.persist_risk_results(run_id, envelopes, now)
    if orders:
        store.persist_orders(run_id=run_id, orders=orders, digest=digest, now=now)
    return orders


def evaluate_and_persist(
    *,
    store: RuntimeStore,
    engine: RiskEngine,
    run_id: str,
    intents: tuple[StrategyIntent, ...],
    risk_context: RiskContext,
    intended_session_date: date,
    now: datetime,
) -> tuple[PendingOrder, ...]:
    decisions = engine.evaluate_many(intents, risk_context)
    return persist_risk_and_orders(
        store=store,
        run_id=run_id,
        decisions=decisions,
        intended_session_date=intended_session_date,
        now=now,
    )
