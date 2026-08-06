from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from stock_agent.data.providers import BoundedSessionSchedule, SessionScheduleRow
from stock_agent.domain import Currency, Market

STRATEGY_A_SCHEDULE_ID = "strategy-a-us-ibm-2026-07-provider-fixture-v1"
_DATASET_DIGEST = "dataset-sha256:5183efd941801b2aacba9fcf7aa8a01425a79845218f5c8bfad0ef62e83128f6"
_SOURCE_RECORD_IDS = (
    "provider-payload-sha256:a5ba707d77a42f1c983a499a06a0cbae694ecc891fb4068be144198052fe71c4",
    "provider-payload-sha256:c626226d66f5ffbd0cb07c0eaf12f2d4324bf25776fb3c1af04f754fa11643e9",
    "provider-payload-sha256:6349c31db3f2e70ba44c3803848a78c8e33c0f7433ed00bd52484a91f98ea46e",
    "provider-payload-sha256:75907f19e58f9c994217ec5142ea2ff23764cb9068f505b31f2974f08c80608f",
    "provider-payload-sha256:00207900fad507e6ed817b2aeb9212c2051faeb057a6fdd3ba2ea00588ccb982",
)
_DATES = tuple(date(2026, 7, day) for day in (24, 27, 28, 29, 30))
_TIMEZONE = "America/New_York"
_GENERATED_ON = date(2026, 8, 6)
_PROVENANCE_ID = "nyse-core-hours-and-nasdaq-ibm-fixture-reviewed-2026-08-06"
_PROVENANCE = (
    "NYSE 09:30-16:00 core session schedule; Nasdaq IBM historical fixture observed 2026-08-06"
)


@dataclass(frozen=True, slots=True)
class StrategyAProviderSchedule:
    schedule_id: str
    provenance_id: str
    fixture_dataset_digest: str
    expected_source_record_ids: tuple[str, ...]
    provider_id: str
    symbol: str
    market: Market
    currency: Currency
    sector: str
    schedule: BoundedSessionSchedule


_ZONE = ZoneInfo(_TIMEZONE)
_ROWS = tuple(
    SessionScheduleRow(
        session_date=session_date,
        open_at=datetime.combine(session_date, time(9, 30), _ZONE),
        close_at=datetime.combine(session_date, time(16), _ZONE),
        timezone=_TIMEZONE,
        provenance=_PROVENANCE,
        generated_on=_GENERATED_ON,
    )
    for session_date in _DATES
)

STRATEGY_A_SCHEDULE = StrategyAProviderSchedule(
    schedule_id=STRATEGY_A_SCHEDULE_ID,
    provenance_id=_PROVENANCE_ID,
    fixture_dataset_digest=_DATASET_DIGEST,
    expected_source_record_ids=_SOURCE_RECORD_IDS,
    provider_id="nasdaq-historical",
    symbol="IBM",
    market=Market.US,
    currency=Currency.USD,
    sector="Technology",
    schedule=BoundedSessionSchedule(
        market=Market.US,
        start=_DATES[0],
        end=_DATES[-1],
        sessions=_ROWS,
    ),
)
