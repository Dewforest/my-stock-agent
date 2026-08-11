from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from types import MappingProxyType
from zoneinfo import ZoneInfo

from stock_agent.data.providers import (
    BoundedSessionSchedule,
    DailyBarRequest,
    SessionScheduleRow,
)
from stock_agent.domain import Currency, Market

CN_SCHEDULE_ID = "cn-600000-2025-07"
US_SCHEDULE_ID = "us-ibm-current-compact"

_GENERATED_ON = date(2026, 8, 4)
_CN_PROVENANCE = (
    "Shanghai Stock Exchange 2025 Trading Calendar and stock trading hours, "
    "retrieved 2026-08-04: https://english.sse.com.cn/start/trading/schedule/"
)
_US_PROVENANCE = (
    "NYSE 2026 Holidays & Trading Hours and 09:30-16:00 core session hours, "
    "retrieved 2026-08-04: https://www.nyse.com/trade/hours-calendars"
)
_CN_PROVENANCE_ID = "sse-2025-trading-calendar-and-hours-reviewed-2026-08-04"
_US_PROVENANCE_ID = "nyse-2026-holidays-and-core-hours-reviewed-2026-08-04"


@dataclass(frozen=True)
class LiveMarketSchedule:
    schedule_id: str
    provenance_id: str
    symbol: str
    request: DailyBarRequest
    schedule: BoundedSessionSchedule
    currency: Currency
    sector: str
    account_id: str
    evidence_filename: str

    @property
    def market(self) -> Market:
        return self.request.market

    def __post_init__(self) -> None:
        if not self.schedule_id or self.schedule_id != self.schedule_id.strip():
            raise ValueError("schedule_id must be nonblank and stripped")
        if not self.provenance_id or self.provenance_id != self.provenance_id.strip():
            raise ValueError("provenance_id must be nonblank and stripped")
        if "http://" in self.provenance_id.lower() or "https://" in self.provenance_id.lower():
            raise ValueError("provenance_id must not contain a URL")
        if self.request.symbol != self.symbol:
            raise ValueError("request symbol must match frozen symbol")
        if self.request.market is not self.schedule.market:
            raise ValueError("request market must match frozen schedule")
        if (
            self.request.start != self.schedule.start
            or self.request.end != self.schedule.end
        ):
            raise ValueError("request bounds must match frozen schedule")
        if self.request.price_mode != "RAW":
            raise ValueError("live schedules support only raw prices")


def _schedule_rows(
    *,
    session_dates: tuple[date, ...],
    timezone: str,
    open_time: time,
    close_time: time,
    provenance: str,
) -> tuple[SessionScheduleRow, ...]:
    zone = ZoneInfo(timezone)
    return tuple(
        SessionScheduleRow(
            session_date=session_date,
            open_at=datetime.combine(session_date, open_time, zone),
            close_at=datetime.combine(session_date, close_time, zone),
            timezone=timezone,
            provenance=provenance,
            generated_on=_GENERATED_ON,
        )
        for session_date in session_dates
    )


_CN_DATES = tuple(date(2025, 7, day) for day in (1, 2, 3, 4, 7, 8, 9, 10))
_US_DATES = tuple(date(2026, 7, day) for day in (27, 28, 29, 30, 31))

_CN = LiveMarketSchedule(
    schedule_id=CN_SCHEDULE_ID,
    provenance_id=_CN_PROVENANCE_ID,
    symbol="600000",
    request=DailyBarRequest(
        market=Market.CN,
        symbol="600000",
        start=_CN_DATES[0],
        end=_CN_DATES[-1],
        price_mode="RAW",
    ),
    schedule=BoundedSessionSchedule(
        market=Market.CN,
        start=_CN_DATES[0],
        end=_CN_DATES[-1],
        sessions=_schedule_rows(
            session_dates=_CN_DATES,
            timezone="Asia/Shanghai",
            open_time=time(9, 30),
            close_time=time(15),
            provenance=_CN_PROVENANCE,
        ),
    ),
    currency=Currency.CNY,
    sector="Financials",
    account_id="live-cn-verification",
    evidence_filename="real-market-data-cn.json",
)

_US = LiveMarketSchedule(
    schedule_id=US_SCHEDULE_ID,
    provenance_id=_US_PROVENANCE_ID,
    symbol="IBM",
    request=DailyBarRequest(
        market=Market.US,
        symbol="IBM",
        start=_US_DATES[0],
        end=_US_DATES[-1],
        price_mode="RAW",
    ),
    schedule=BoundedSessionSchedule(
        market=Market.US,
        start=_US_DATES[0],
        end=_US_DATES[-1],
        sessions=_schedule_rows(
            session_dates=_US_DATES,
            timezone="America/New_York",
            open_time=time(9, 30),
            close_time=time(16),
            provenance=_US_PROVENANCE,
        ),
    ),
    currency=Currency.USD,
    sector="Technology",
    account_id="live-us-verification",
    evidence_filename="real-market-data-us.json",
)

LIVE_MARKET_SCHEDULES: Mapping[str, LiveMarketSchedule] = MappingProxyType(
    {
        CN_SCHEDULE_ID: _CN,
        US_SCHEDULE_ID: _US,
    }
)
