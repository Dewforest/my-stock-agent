from __future__ import annotations

from datetime import date, datetime

from stock_agent.runtime.calendars import (
    CalendarLookupStatus,
    RuntimeMarketSchedule,
    TradingSession,
)


class MarketClock:
    """Session-window judgment for a single market schedule."""

    def __init__(self, schedule: RuntimeMarketSchedule) -> None:
        if type(schedule) is not RuntimeMarketSchedule:
            raise TypeError("schedule must be exactly RuntimeMarketSchedule")
        self._schedule = schedule

    def is_session(self, session_date: date) -> bool:
        return self._schedule.lookup(session_date).status is CalendarLookupStatus.SESSION

    def session_for(self, session_date: date) -> TradingSession | None:
        result = self._schedule.lookup(session_date)
        return result.session

    def next_session_after(self, session_date: date) -> TradingSession | None:
        result = self._schedule.next_session(session_date)
        return result.session

    def has_closed(self, session_date: date, now: datetime) -> bool:
        session = self.session_for(session_date)
        if session is None:
            return False
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now >= session.closed_at
