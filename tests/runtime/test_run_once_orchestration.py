from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from _market_fixtures import _session, make_clock

from stock_agent.domain import Market
from stock_agent.runtime.capability_gates import CapabilityGates
from stock_agent.runtime.orchestrator import (
    MarketOutcome,
    RunOnceOrchestrator,
)
from stock_agent.runtime.state import KillSwitchScope
from stock_agent.runtime.store import RuntimeStore

US = "paper-us-v1"
CN = "paper-cn-v1"
SESSION_DAY = date(2026, 8, 14)  # a Friday


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    decide=None,
    blocked: bool = False,
) -> tuple[RunOnceOrchestrator, RuntimeStore]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    if blocked:
        store.set_kill_switch(KillSwitchScope.GLOBAL, "", set_by="test", now=now)
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


def test_non_session_day_skips_without_deciding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Market, date]] = []
    now = datetime(2026, 8, 15, 21, 0, tzinfo=UTC)  # a Saturday
    orchestrator, _ = build(tmp_path, monkeypatch, now, decide=lambda m, d: calls.append((m, d)))
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.SKIPPED_NOT_SESSION for item in report.markets)
    assert calls == []


def test_too_early_skips_without_deciding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Market, date]] = []
    now = datetime(2026, 8, 14, 5, 0, tzinfo=UTC)  # before both US and CN close
    orchestrator, _ = build(tmp_path, monkeypatch, now, decide=lambda m, d: calls.append((m, d)))
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.SKIPPED_TOO_EARLY for item in report.markets)
    assert calls == []


def test_kill_switch_blocks_without_deciding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Market, date]] = []
    now = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
    orchestrator, _ = build(
        tmp_path, monkeypatch, now, decide=lambda m, d: calls.append((m, d)), blocked=True
    )
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.BLOCKED_KILL_SWITCH for item in report.markets)
    assert calls == []


def test_closed_session_decides_once_per_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Market, date]] = []
    now = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
    orchestrator, _ = build(tmp_path, monkeypatch, now, decide=lambda m, d: calls.append((m, d)))
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.PROCESSED for item in report.markets)
    assert sorted(m for m, _ in calls) == [Market.CN, Market.US]
    assert all(d == SESSION_DAY for _, d in calls)


def test_no_decide_emits_missed_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)
    orchestrator, _ = build(tmp_path, monkeypatch, now, decide=None)
    report = orchestrator.run(now)
    assert all(item.outcome is MarketOutcome.MISSED_DECISION_DEADLINE for item in report.markets)
