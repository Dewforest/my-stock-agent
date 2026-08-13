from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from stock_agent.domain import Market
from stock_agent.runtime import calendars as calendar_module
from stock_agent.runtime.calendars import (
    AuthorityStatus,
    EvidenceClass,
    Exchange,
    ExchangeSchedule,
    RuntimeMarketSchedule,
    ScheduleProvenance,
    SessionSegment,
    TradingSession,
)
from stock_agent.runtime.clock import MarketClock


def _session(day: date, market: Market) -> TradingSession:
    if market is Market.US:
        tz = ZoneInfo("America/New_York")
        segments = (
            SessionSegment(
                opened_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=tz),
                closed_at=datetime(day.year, day.month, day.day, 16, 0, tzinfo=tz),
            ),
        )
    else:
        tz = ZoneInfo("Asia/Shanghai")
        segments = (
            SessionSegment(
                opened_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=tz),
                closed_at=datetime(day.year, day.month, day.day, 11, 30, tzinfo=tz),
            ),
            SessionSegment(
                opened_at=datetime(day.year, day.month, day.day, 13, 0, tzinfo=tz),
                closed_at=datetime(day.year, day.month, day.day, 15, 0, tzinfo=tz),
            ),
        )
    return TradingSession(
        session_date=day,
        segments=segments,
        is_half_day=False,
        provenance_row_id=f"{market.value}-{day.isoformat()}",
    )


def _schedule(
    exchange: Exchange, market: Market, sessions: tuple[TradingSession, ...]
) -> ExchangeSchedule:
    year = sessions[0].session_date.year
    tz = "America/New_York" if market is Market.US else "Asia/Shanghai"
    provenance = ScheduleProvenance(
        source_id=f"synthetic-{exchange.value.lower()}-official-test/v1",
        source_digest="source-sha256:" + exchange.value[0].lower() * 64,
        parser_id="synthetic-parser/v1",
        license_profile_id="approved-test-license/v1",
        install_profile_id="approved-test-install/v1",
        evidence_class=EvidenceClass.SYNTHETIC_TEST,
        source_artifact_approved=False,
        license_approved=False,
        install_approved=False,
        authority=AuthorityStatus.TEST_ONLY,
    )
    session_dates = {item.session_date for item in sessions}
    from datetime import timedelta

    cursor = date(year, 1, 1)
    end = date(year, 12, 31)
    closures = [
        d
        for d in (cursor + timedelta(n) for n in range((end - cursor).days + 1))
        if d not in session_dates
    ]
    from stock_agent.runtime.calendars import canonical_schedule_digest

    values = {
        "schedule_id": f"{exchange.value.lower()}-{year}/test-v1",
        "schedule_digest": "schedule-sha256:" + "0" * 64,
        "exchange": exchange,
        "market": market,
        "timezone": tz,
        "year": year,
        "coverage_from": date(year, 1, 1),
        "coverage_through": date(year, 12, 31),
        "provenance": provenance,
        "sessions": sessions,
        "closures": tuple(closures),
    }
    provisional = ExchangeSchedule.model_construct(**values)
    return ExchangeSchedule(**{**values, "schedule_digest": canonical_schedule_digest(provisional)})


def make_clock(
    monkeypatch: pytest.MonkeyPatch, market: Market, sessions: tuple[TradingSession, ...]
) -> MarketClock:
    exchanges = (
        (Exchange.NYSE, Exchange.NASDAQ) if market is Market.US else (Exchange.SSE, Exchange.SZSE)
    )
    schedules = tuple(_schedule(exchange, market, sessions) for exchange in exchanges)
    current = getattr(calendar_module, "APPROVED_OFFICIAL_SCHEDULES", frozenset())
    monkeypatch.setattr(
        calendar_module,
        "APPROVED_OFFICIAL_SCHEDULES",
        frozenset(current | {(item.schedule_id, item.schedule_digest) for item in schedules}),
    )
    runtime = RuntimeMarketSchedule(
        market=market,
        represented_exchanges=exchanges,
        schedules=schedules,
    )
    return MarketClock(runtime)
