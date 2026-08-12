from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StringConstraints, field_validator, model_validator

from stock_agent.audit.canonical import tagged_sha256
from stock_agent.domain import Market
from stock_agent.runtime.models import RuntimeModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Exchange(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"


class AuthorityStatus(StrEnum):
    TEST_ONLY = "TEST_ONLY"
    RESEARCH_DERIVED = "RESEARCH_DERIVED"
    PARTIAL = "PARTIAL"
    VALIDATED = "VALIDATED"
    OFFICIAL = "OFFICIAL"


class EvidenceClass(StrEnum):
    SYNTHETIC_TEST = "SYNTHETIC_TEST"
    RESEARCH_REPORT = "RESEARCH_REPORT"
    COMMUNITY_LIBRARY = "COMMUNITY_LIBRARY"
    OFFICIAL_ARTIFACT = "OFFICIAL_ARTIFACT"


class CalendarLookupStatus(StrEnum):
    SESSION = "SESSION"
    MARKET_CLOSED = "MARKET_CLOSED"
    CALENDAR_NOT_COVERED = "CALENDAR_NOT_COVERED"


class SourceArtifact(RuntimeModel):
    source_id: NonEmptyStr
    canonical_url: Annotated[str, StringConstraints(pattern=r"^https://[^\s]+$")]
    retrieved_at: datetime
    http_status: int = Field(ge=100, le=599)
    content_type: NonEmptyStr
    raw_sha256: Sha256Hex

    @field_validator("retrieved_at", mode="after")
    @classmethod
    def retrieval_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value


class PrivateInstallProfile(RuntimeModel):
    profile_id: NonEmptyStr
    license_approved: bool
    install_approved: bool

    @property
    def approved(self) -> bool:
        return self.license_approved and self.install_approved


class ScheduleProvenance(RuntimeModel):
    source_id: NonEmptyStr
    source_digest: NonEmptyStr
    parser_id: NonEmptyStr
    license_profile_id: NonEmptyStr
    install_profile_id: NonEmptyStr
    evidence_class: EvidenceClass = EvidenceClass.SYNTHETIC_TEST
    source_artifact_approved: bool = False
    license_approved: bool = False
    install_approved: bool = False
    authority: AuthorityStatus

    @model_validator(mode="after")
    def official_requires_complete_approved_evidence(self) -> Self:
        if self.authority is AuthorityStatus.OFFICIAL and (
            self.evidence_class is not EvidenceClass.OFFICIAL_ARTIFACT
            or not self.source_artifact_approved
            or not self.license_approved
            or not self.install_approved
        ):
            raise ValueError("OFFICIAL authority requires approved official artifact and profiles")
        return self


class SessionSegment(RuntimeModel):
    opened_at: datetime
    closed_at: datetime

    @model_validator(mode="after")
    def instants_are_aware_and_increasing(self) -> Self:
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("opened_at must be timezone-aware")
        if self.closed_at.tzinfo is None or self.closed_at.utcoffset() is None:
            raise ValueError("closed_at must be timezone-aware")
        if self.opened_at >= self.closed_at:
            raise ValueError("segment open must precede close")
        return self


class TradingSession(RuntimeModel):
    session_date: date
    segments: tuple[SessionSegment, ...] = Field(min_length=1)
    is_half_day: bool
    provenance_row_id: NonEmptyStr

    @field_validator("segments", mode="after")
    @classmethod
    def sort_non_overlapping_segments(
        cls, value: tuple[SessionSegment, ...]
    ) -> tuple[SessionSegment, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.opened_at))
        if any(left.closed_at > right.opened_at for left, right in pairwise(ordered)):
            raise ValueError("session segments must not overlap")
        return ordered

    @property
    def opened_at(self) -> datetime:
        return self.segments[0].opened_at

    @property
    def closed_at(self) -> datetime:
        return self.segments[-1].closed_at


class ExchangeSchedule(RuntimeModel):
    schedule_id: NonEmptyStr
    schedule_digest: NonEmptyStr
    exchange: Exchange
    market: Market
    timezone: NonEmptyStr
    year: int = Field(ge=2000, le=9999)
    coverage_from: date
    coverage_through: date
    provenance: ScheduleProvenance
    sessions: tuple[TradingSession, ...]
    closures: tuple[date, ...]

    @field_validator("sessions", mode="after")
    @classmethod
    def sort_unique_sessions(cls, value: tuple[TradingSession, ...]) -> tuple[TradingSession, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.session_date))
        if any(left.session_date == right.session_date for left, right in pairwise(ordered)):
            raise ValueError("schedule sessions must have unique dates")
        return ordered

    @field_validator("closures", mode="after")
    @classmethod
    def sort_unique_closures(cls, value: tuple[date, ...]) -> tuple[date, ...]:
        if any(type(item) is not date for item in value):
            raise TypeError("closures must contain only plain date values")
        ordered = tuple(sorted(value))
        if any(left == right for left, right in pairwise(ordered)):
            raise ValueError("schedule closures must have unique dates")
        return ordered

    @model_validator(mode="after")
    def exchange_market_and_year_match(self) -> Self:
        expected_market = {
            Exchange.SSE: Market.CN,
            Exchange.SZSE: Market.CN,
            Exchange.NYSE: Market.US,
            Exchange.NASDAQ: Market.US,
        }[self.exchange]
        if self.market is not expected_market:
            raise ValueError("exchange does not belong to market")
        expected_timezone = {
            Market.CN: "Asia/Shanghai",
            Market.US: "America/New_York",
        }[self.market]
        if self.timezone != expected_timezone:
            raise ValueError("schedule timezone does not match market")
        try:
            local_timezone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("schedule timezone must be a valid IANA timezone") from None
        if any(item.session_date.year != self.year for item in self.sessions):
            raise ValueError("all sessions must belong to schedule year")
        if any(
            segment.opened_at.astimezone(local_timezone).date() != session.session_date
            or segment.closed_at.astimezone(local_timezone).date() != session.session_date
            for session in self.sessions
            for segment in session.segments
        ):
            raise ValueError("session instant local date does not match session_date")
        if any(item.year != self.year for item in self.closures):
            raise ValueError("all closures must belong to schedule year")
        session_dates = {session.session_date for session in self.sessions}
        if any(item in session_dates for item in self.closures):
            raise ValueError("closure dates must not overlap sessions")
        if (
            self.coverage_from.year != self.year
            or self.coverage_through.year != self.year
            or self.coverage_from > self.coverage_through
        ):
            raise ValueError("coverage bounds must be ordered within schedule year")
        covered_dates = session_dates | set(self.closures)
        expected_dates: set[date] = set()
        cursor = self.coverage_from
        while cursor <= self.coverage_through:
            expected_dates.add(cursor)
            cursor += timedelta(days=1)
        if covered_dates != expected_dates:
            raise ValueError("every covered date must be exactly one session or closure")
        self._validate_market_wall_clock(local_timezone)
        if canonical_schedule_digest(self) != self.schedule_digest:
            raise ValueError("schedule digest does not match canonical content")
        return self

    def _validate_market_wall_clock(self, local_timezone: ZoneInfo) -> None:
        for session in self.sessions:
            actual = tuple(
                (
                    segment.opened_at.astimezone(local_timezone).time().replace(tzinfo=None),
                    segment.closed_at.astimezone(local_timezone).time().replace(tzinfo=None),
                )
                for segment in session.segments
            )
            if self.market is Market.US:
                expected = ((time(9, 30), time(13 if session.is_half_day else 16)),)
            else:
                if session.is_half_day:
                    raise ValueError("CN v1 does not approve half-day sessions")
                expected = ((time(9, 30), time(11, 30)), (time(13), time(15)))
            if actual != expected:
                raise ValueError("session wall-clock does not match market schedule rules")


def canonical_schedule_digest(schedule: ExchangeSchedule) -> str:
    return tagged_sha256(
        "schedule",
        (
            schedule.schedule_id,
            schedule.exchange.value,
            schedule.market.value,
            schedule.timezone,
            schedule.year,
            schedule.coverage_from.isoformat(),
            schedule.coverage_through.isoformat(),
            (
                schedule.provenance.source_id,
                schedule.provenance.source_digest,
                schedule.provenance.parser_id,
                schedule.provenance.license_profile_id,
                schedule.provenance.install_profile_id,
                schedule.provenance.evidence_class.value,
                schedule.provenance.source_artifact_approved,
                schedule.provenance.license_approved,
                schedule.provenance.install_approved,
                schedule.provenance.authority.value,
            ),
            tuple(
                (
                    item.session_date.isoformat(),
                    item.is_half_day,
                    item.provenance_row_id,
                    tuple(
                        (
                            segment.opened_at.astimezone(UTC).isoformat(),
                            segment.closed_at.astimezone(UTC).isoformat(),
                        )
                        for segment in sorted(item.segments, key=lambda value: value.opened_at)
                    ),
                )
                for item in sorted(schedule.sessions, key=lambda value: value.session_date)
            ),
            tuple(item.isoformat() for item in sorted(schedule.closures)),
        ),
    )


class CalendarLookup(RuntimeModel):
    status: CalendarLookupStatus
    session: TradingSession | None

    @model_validator(mode="after")
    def status_matches_session(self) -> Self:
        if (self.status is CalendarLookupStatus.SESSION) != (self.session is not None):
            raise ValueError("calendar lookup status and session do not match")
        return self


APPROVED_OFFICIAL_SCHEDULES: frozenset[tuple[str, str]] = frozenset()


class MarketScheduleProjection(RuntimeModel):
    market: Market
    represented_exchanges: tuple[Exchange, ...]
    schedules: tuple[ExchangeSchedule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def schedules_match_profile_and_cover_complete_consecutive_years(self) -> Self:
        expected = {
            Market.CN: (Exchange.SSE, Exchange.SZSE),
            Market.US: (Exchange.NYSE, Exchange.NASDAQ),
        }[self.market]
        if self.represented_exchanges != expected:
            raise ValueError("represented exchanges do not match market profile")
        identities = tuple((item.exchange, item.year) for item in self.schedules)
        if len(identities) != len(set(identities)):
            raise ValueError("schedule exchange/year identities must be unique")
        if any(
            item.market is not self.market or item.exchange not in self.represented_exchanges
            for item in self.schedules
        ):
            raise ValueError("schedules do not match market profile")
        if any(
            item.coverage_from != date(item.year, 1, 1)
            or item.coverage_through != date(item.year, 12, 31)
            for item in self.schedules
        ):
            raise ValueError("annual projection requires full-year coverage")
        authorities = {item.provenance.authority for item in self.schedules}
        if len(authorities) != 1:
            raise ValueError("projection schedules must share one authority status")
        by_year_exchange = {(item.year, item.exchange): item for item in self.schedules}
        years = sorted({item.year for item in self.schedules})
        if years != list(range(years[0], years[-1] + 1)):
            raise ValueError("schedule years must be consecutive")
        if any(
            (year, exchange) not in by_year_exchange
            for year in years
            for exchange in self.represented_exchanges
        ):
            raise ValueError("every year requires every represented exchange")
        self._require_consistent_rows(by_year_exchange, years)
        return self

    def _require_consistent_rows(
        self,
        by_year_exchange: dict[tuple[int, Exchange], ExchangeSchedule],
        years: list[int],
    ) -> None:
        for year in years:
            schedules = tuple(
                by_year_exchange[(year, exchange)] for exchange in self.represented_exchanges
            )
            rows = tuple(
                (
                    item.coverage_from,
                    item.coverage_through,
                    tuple(
                        (session.session_date, session.segments, session.is_half_day)
                        for session in item.sessions
                    ),
                    item.closures,
                )
                for item in schedules
            )
            if any(row != rows[0] for row in rows[1:]):
                raise ValueError("represented exchange session rows conflict")


class RuntimeMarketSchedule(MarketScheduleProjection):
    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        raise TypeError("runtime market schedules require validated construction")

    @model_validator(mode="after")
    def schedules_match_approved_official_manifest(self) -> Self:
        self._require_approved_manifest(ValueError)
        return self

    def _require_approved_manifest(
        self, error_type: type[ValueError] | type[RuntimeError] = RuntimeError
    ) -> None:
        actual = frozenset((item.schedule_id, item.schedule_digest) for item in self.schedules)
        if not actual or not actual.issubset(APPROVED_OFFICIAL_SCHEDULES):
            raise error_type("runtime schedules do not match approved manifest")

    @property
    def sessions(self) -> tuple[TradingSession, ...]:
        self._require_approved_manifest()
        primary = self.represented_exchanges[0]
        return tuple(
            session
            for schedule in sorted(self.schedules, key=lambda item: item.year)
            if schedule.exchange is primary
            for session in schedule.sessions
        )

    @property
    def closures(self) -> tuple[date, ...]:
        self._require_approved_manifest()
        primary = self.represented_exchanges[0]
        return tuple(
            closed
            for schedule in sorted(self.schedules, key=lambda item: item.year)
            if schedule.exchange is primary
            for closed in schedule.closures
        )

    @property
    def covered_years(self) -> frozenset[int]:
        self._require_approved_manifest()
        return frozenset(item.year for item in self.schedules)

    def lookup(self, session_date: date) -> CalendarLookup:
        self._require_approved_manifest()
        if type(session_date) is not date:
            raise TypeError("session_date must be a plain date")
        if session_date.year not in self.covered_years:
            return CalendarLookup(status=CalendarLookupStatus.CALENDAR_NOT_COVERED, session=None)
        match = next((item for item in self.sessions if item.session_date == session_date), None)
        if match is not None:
            return CalendarLookup(status=CalendarLookupStatus.SESSION, session=match)
        return CalendarLookup(status=CalendarLookupStatus.MARKET_CLOSED, session=None)

    def next_session(self, after: date) -> CalendarLookup:
        self._require_approved_manifest()
        if type(after) is not date:
            raise TypeError("after must be a plain date")
        if after.year not in self.covered_years:
            return CalendarLookup(status=CalendarLookupStatus.CALENDAR_NOT_COVERED, session=None)
        match = next((item for item in self.sessions if item.session_date > after), None)
        if match is None or match.session_date.year > after.year + 1:
            return CalendarLookup(status=CalendarLookupStatus.CALENDAR_NOT_COVERED, session=None)
        return CalendarLookup(status=CalendarLookupStatus.SESSION, session=match)

    def requests_for_session(self, session_date: date, *, symbol_count: int) -> int:
        self._require_approved_manifest()
        if type(symbol_count) is not int or symbol_count <= 0:
            raise ValueError("symbol_count must be a positive integer")
        result = self.lookup(session_date)
        return symbol_count if result.status is CalendarLookupStatus.SESSION else 0
