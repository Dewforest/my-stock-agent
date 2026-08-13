from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from enum import StrEnum

from stock_agent.domain import Market
from stock_agent.runtime.capability_gates import CapabilityGates
from stock_agent.runtime.clock import MarketClock
from stock_agent.runtime.models import RuntimeModel
from stock_agent.runtime.store import RuntimeStore


class MarketOutcome(StrEnum):
    SKIPPED_NOT_SESSION = "SKIPPED_NOT_SESSION"
    SKIPPED_TOO_EARLY = "SKIPPED_TOO_EARLY"
    BLOCKED_KILL_SWITCH = "BLOCKED_KILL_SWITCH"
    MISSED_DECISION_DEADLINE = "MISSED_DECISION_DEADLINE"
    PROCESSED = "PROCESSED"


class MarketReport(RuntimeModel):
    market: Market
    outcome: MarketOutcome


class OrchestrationReport(RuntimeModel):
    markets: tuple[MarketReport, ...]


class RunOnceOrchestrator:
    """Top-level unattended dual-market run-once state machine.

    Each market advances independently through a single clocked session per
    wake. Non-session, too-early, and kill-switched markets perform zero
    provider/LLM/secret work. Decision work is injected as a single ``decide``
    callable so the full pipeline is testable without real data sources.
    """

    def __init__(
        self,
        *,
        clocks: Mapping[Market, MarketClock],
        store: RuntimeStore,
        gates: CapabilityGates,
        account_ids: Mapping[Market, str],
        order: tuple[Market, ...] = (Market.US, Market.CN),
        decide: Callable[[Market, date], None] | None = None,
    ) -> None:
        if type(store) is not RuntimeStore:
            raise TypeError("store must be exactly RuntimeStore")
        if type(gates) is not CapabilityGates:
            raise TypeError("gates must be exactly CapabilityGates")
        for market in order:
            if type(market) is not Market:
                raise TypeError("order must contain exact Market values")
            if type(clocks[market]) is not MarketClock:
                raise TypeError("clocks must map every market to an exact MarketClock")
            if type(account_ids[market]) is not str or not account_ids[market]:
                raise ValueError("account_ids must map every market to a nonblank string")
        self._clocks = dict(clocks)
        self._store = store
        self._gates = gates
        self._account_ids = dict(account_ids)
        self._order = order
        self._decide = decide

    def run(self, now: datetime) -> OrchestrationReport:
        if type(now) is not datetime or now.tzinfo is None:
            raise TypeError("now must be a timezone-aware datetime")
        return OrchestrationReport(
            markets=tuple(self._run_market(market, now) for market in self._order)
        )

    def _run_market(self, market: Market, now: datetime) -> MarketReport:
        clock = self._clocks[market]
        today = now.date()

        if self._store.is_blocked(market, self._account_ids[market]):
            return MarketReport(market=market, outcome=MarketOutcome.BLOCKED_KILL_SWITCH)

        if not clock.is_session(today):
            return MarketReport(market=market, outcome=MarketOutcome.SKIPPED_NOT_SESSION)

        if not clock.has_closed(today, now):
            return MarketReport(market=market, outcome=MarketOutcome.SKIPPED_TOO_EARLY)

        if self._decide is None:
            return MarketReport(market=market, outcome=MarketOutcome.MISSED_DECISION_DEADLINE)

        self._decide(market, today)
        return MarketReport(market=market, outcome=MarketOutcome.PROCESSED)
