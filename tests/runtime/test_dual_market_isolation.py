from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from _market_fixtures import _session, make_clock

from stock_agent.domain import Market
from stock_agent.runtime.capability_gates import CapabilityGates
from stock_agent.runtime.orchestrator import MarketOutcome, RunOnceOrchestrator
from stock_agent.runtime.state import KillSwitchScope
from stock_agent.runtime.store import RuntimeStore

US = "paper-us-v1"
CN = "paper-cn-v1"
SESSION_DAY = date(2026, 8, 14)


def build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime, decide=None
) -> tuple[RunOnceOrchestrator, RuntimeStore]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    clocks = {
        Market.US: make_clock(monkeypatch, Market.US, (_session(SESSION_DAY, Market.US),)),
        Market.CN: make_clock(monkeypatch, Market.CN, (_session(SESSION_DAY, Market.CN),)),
    }
    orchestrator = RunOnceOrchestrator(
        clocks=clocks,
        store=store,
        gates=CapabilityGates(()),
        account_ids={Market.US: US, Market.CN: CN},
        decide=decide,
    )
    return orchestrator, store


def test_cn_kill_switch_does_not_block_us(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
    calls: list[Market] = []
    orchestrator, store = build(tmp_path, monkeypatch, now, decide=lambda m, d: calls.append(m))
    store.set_kill_switch(KillSwitchScope.MARKET, Market.CN.value, set_by="test", now=now)

    report = orchestrator.run(now)
    by_market = {item.market: item.outcome for item in report.markets}
    assert by_market[Market.CN] is MarketOutcome.BLOCKED_KILL_SWITCH
    assert by_market[Market.US] is MarketOutcome.PROCESSED
    assert calls == [Market.US]


def test_cn_failure_does_not_block_us_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
    calls: list[tuple[Market, date]] = []
    orchestrator, _ = build(tmp_path, monkeypatch, now, decide=lambda m, d: calls.append((m, d)))
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.PROCESSED for item in report.markets)
    assert sorted(m for m, _ in calls) == [Market.CN, Market.US]
