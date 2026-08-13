import json
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ValidationError

import stock_agent.runtime.calendars as calendar_module
from stock_agent.domain import Market
from stock_agent.runtime.calendars import (
    AuthorityStatus,
    CalendarLookupStatus,
    EvidenceClass,
    Exchange,
    ExchangeSchedule,
    MarketScheduleProjection,
    PrivateInstallProfile,
    RuntimeMarketSchedule,
    ScheduleProvenance,
    SessionSegment,
    SourceArtifact,
    TradingSession,
    canonical_schedule_digest,
)


def test_strict_schedule_sorts_aware_half_day_sessions_and_segments() -> None:
    provenance = ScheduleProvenance(
        source_id="synthetic-nyse-test/v1",
        source_digest="source-sha256:" + "a" * 64,
        parser_id="synthetic-parser/v1",
        license_profile_id="test-only/v1",
        install_profile_id="test-only/v1",
        authority=AuthorityStatus.TEST_ONLY,
    )
    half_day = TradingSession(
        session_date=date(2026, 11, 27),
        segments=(
            SessionSegment(
                opened_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
                closed_at=datetime(2026, 11, 27, 18, tzinfo=UTC),
            ),
        ),
        is_half_day=True,
        provenance_row_id="nyse-2026-11-27",
    )
    regular = TradingSession(
        session_date=date(2026, 11, 30),
        segments=(
            SessionSegment(
                opened_at=datetime(2026, 11, 30, 14, 30, tzinfo=UTC),
                closed_at=datetime(2026, 11, 30, 21, tzinfo=UTC),
            ),
        ),
        is_half_day=False,
        provenance_row_id="nyse-2026-11-30",
    )

    schedule_values = {
        "schedule_id": "nyse-2026/test-v1",
        "exchange": Exchange.NYSE,
        "market": Market.US,
        "timezone": "America/New_York",
        "year": 2026,
        "coverage_from": date(2026, 11, 27),
        "coverage_through": date(2026, 11, 30),
        "provenance": provenance,
        "sessions": (regular, half_day),
        "closures": (date(2026, 11, 28), date(2026, 11, 29)),
    }
    provisional = ExchangeSchedule.model_construct(**schedule_values)
    schedule = ExchangeSchedule(
        **schedule_values,
        schedule_digest=canonical_schedule_digest(provisional),
    )

    assert tuple(item.session_date for item in schedule.sessions) == (
        date(2026, 11, 27),
        date(2026, 11, 30),
    )
    assert schedule.sessions[0].opened_at == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert schedule.sessions[0].closed_at == datetime(2026, 11, 27, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    "opened_at,closed_at",
    [
        (datetime(2026, 11, 27, 14, 30), datetime(2026, 11, 27, 18, tzinfo=UTC)),
        (datetime(2026, 11, 27, 14, 30, tzinfo=UTC), datetime(2026, 11, 27, 18)),
        (
            datetime(2026, 11, 27, 18, tzinfo=UTC),
            datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        ),
    ],
)
def test_segment_rejects_naive_or_non_increasing_instants(
    opened_at: datetime, closed_at: datetime
) -> None:
    with pytest.raises(ValidationError):
        SessionSegment(opened_at=opened_at, closed_at=closed_at)


def _us_session(day: date, *, close_hour: int = 16) -> TradingSession:
    eastern = ZoneInfo("America/New_York")
    return TradingSession(
        session_date=day,
        segments=(
            SessionSegment(
                opened_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=eastern),
                closed_at=datetime(day.year, day.month, day.day, close_hour, tzinfo=eastern),
            ),
        ),
        is_half_day=close_hour == 13,
        provenance_row_id=f"nyse-{day.isoformat()}",
    )


def test_explicit_rows_encode_dst_half_day_and_closures_without_weekday_inference() -> None:
    sessions = (
        _us_session(date(2026, 3, 6)),
        _us_session(date(2026, 3, 9)),
        _us_session(date(2026, 11, 27), close_hour=13),
    )

    assert sessions[0].opened_at.utcoffset() == -timedelta(hours=5)
    assert sessions[1].opened_at.utcoffset() == -timedelta(hours=4)
    assert sessions[2].closed_at.hour == 13
    assert date(2026, 3, 10) not in {item.session_date for item in sessions}


def test_cn_session_preserves_lunch_break_as_two_segments() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    session = TradingSession(
        session_date=date(2026, 8, 12),
        segments=(
            SessionSegment(
                opened_at=datetime(2026, 8, 12, 13, 0, tzinfo=shanghai),
                closed_at=datetime(2026, 8, 12, 15, 0, tzinfo=shanghai),
            ),
            SessionSegment(
                opened_at=datetime(2026, 8, 12, 9, 30, tzinfo=shanghai),
                closed_at=datetime(2026, 8, 12, 11, 30, tzinfo=shanghai),
            ),
        ),
        is_half_day=False,
        provenance_row_id="sse-2026-08-12",
    )

    assert tuple(segment.opened_at.hour for segment in session.segments) == (9, 13)
    assert session.segments[0].closed_at.hour == 11
    assert session.segments[1].opened_at.hour == 13


def test_schedule_rejects_duplicate_dates_and_session_rejects_overlapping_segments() -> None:
    provenance = ScheduleProvenance(
        source_id="synthetic/v1",
        source_digest="source-sha256:" + "b" * 64,
        parser_id="synthetic-parser/v1",
        license_profile_id="test-only/v1",
        install_profile_id="test-only/v1",
        authority=AuthorityStatus.TEST_ONLY,
    )
    session = _us_session(date(2026, 3, 9))
    with pytest.raises(ValidationError, match="unique dates"):
        ExchangeSchedule(
            schedule_id="nyse-2026/test-v1",
            exchange=Exchange.NYSE,
            market=Market.US,
            timezone="America/New_York",
            year=2026,
            coverage_from=date(2026, 3, 9),
            coverage_through=date(2026, 3, 9),
            provenance=provenance,
            sessions=(session, session),
            closures=(),
        )

    with pytest.raises(ValidationError, match="overlap"):
        TradingSession(
            session_date=date(2026, 3, 9),
            segments=(
                SessionSegment(
                    opened_at=datetime(2026, 3, 9, 9, 30, tzinfo=UTC),
                    closed_at=datetime(2026, 3, 9, 12, tzinfo=UTC),
                ),
                SessionSegment(
                    opened_at=datetime(2026, 3, 9, 11, tzinfo=UTC),
                    closed_at=datetime(2026, 3, 9, 16, tzinfo=UTC),
                ),
            ),
            is_half_day=False,
            provenance_row_id="overlap",
        )


def test_schedule_digest_binds_rows_and_provenance() -> None:
    provenance = ScheduleProvenance(
        source_id="synthetic-nyse-test/v1",
        source_digest="source-sha256:" + "c" * 64,
        parser_id="synthetic-parser/v1",
        license_profile_id="test-only/v1",
        install_profile_id="test-only/v1",
        authority=AuthorityStatus.TEST_ONLY,
    )
    values = {
        "schedule_id": "nyse-2026/test-v1",
        "schedule_digest": "schedule-sha256:" + "0" * 64,
        "exchange": Exchange.NYSE,
        "market": Market.US,
        "timezone": "America/New_York",
        "year": 2026,
        "coverage_from": date(2026, 3, 9),
        "coverage_through": date(2026, 3, 9),
        "provenance": provenance,
        "sessions": (_us_session(date(2026, 3, 9)),),
        "closures": (),
    }
    provisional = ExchangeSchedule.model_construct(**values)
    digest = canonical_schedule_digest(provisional)
    schedule = ExchangeSchedule(**{**values, "schedule_digest": digest})

    assert canonical_schedule_digest(schedule) == digest
    with pytest.raises(ValidationError, match="digest"):
        ExchangeSchedule(
            **{
                **values,
                "schedule_digest": digest,
                "provenance": ScheduleProvenance(
                    **{
                        **provenance.model_dump(),
                        "parser_id": "changed-parser/v2",
                    }
                ),
            }
        )


def test_schedule_rejects_wrong_market_timezone_and_session_local_date() -> None:
    provenance = ScheduleProvenance(
        source_id="synthetic/v1",
        source_digest="source-sha256:" + "f" * 64,
        parser_id="parser/v1",
        license_profile_id="test/v1",
        install_profile_id="test/v1",
        authority=AuthorityStatus.TEST_ONLY,
    )
    wrong_day = TradingSession(
        session_date=date(2026, 3, 9),
        segments=(
            SessionSegment(
                opened_at=datetime(2026, 3, 10, 9, 30, tzinfo=ZoneInfo("America/New_York")),
                closed_at=datetime(2026, 3, 10, 16, tzinfo=ZoneInfo("America/New_York")),
            ),
        ),
        is_half_day=False,
        provenance_row_id="wrong-local-day",
    )
    values = {
        "schedule_id": "nyse-2026/test-v1",
        "schedule_digest": "schedule-sha256:" + "0" * 64,
        "exchange": Exchange.NYSE,
        "market": Market.US,
        "timezone": "Asia/Shanghai",
        "year": 2026,
        "coverage_from": date(2026, 3, 9),
        "coverage_through": date(2026, 3, 9),
        "provenance": provenance,
        "sessions": (wrong_day,),
        "closures": (),
    }
    provisional = ExchangeSchedule.model_construct(**values)
    with pytest.raises(ValidationError, match=r"timezone|local date"):
        ExchangeSchedule(**{**values, "schedule_digest": canonical_schedule_digest(provisional)})


def test_us_schedule_rejects_wrong_dst_offset_even_when_local_date_matches() -> None:
    session = TradingSession(
        session_date=date(2026, 7, 6),
        segments=(
            SessionSegment(
                opened_at=datetime.fromisoformat("2026-07-06T09:30:00-05:00"),
                closed_at=datetime.fromisoformat("2026-07-06T16:00:00-05:00"),
            ),
        ),
        is_half_day=False,
        provenance_row_id="wrong-dst",
    )
    with pytest.raises(ValidationError, match="wall-clock"):
        _official_schedule(Exchange.NYSE, (session,))


def test_canonical_digest_normalizes_equivalent_instants_to_utc() -> None:
    local = _us_session(date(2026, 7, 6))
    utc = TradingSession(
        session_date=local.session_date,
        segments=(
            SessionSegment(
                opened_at=local.opened_at.astimezone(UTC),
                closed_at=local.closed_at.astimezone(UTC),
            ),
        ),
        is_half_day=False,
        provenance_row_id=local.provenance_row_id,
    )
    assert _schedule_digest_input(Exchange.NYSE, (local,)) == _schedule_digest_input(
        Exchange.NYSE, (utc,)
    )


@pytest.mark.parametrize(
    "evidence_class,source_approved,license_approved,install_approved",
    [
        (EvidenceClass.RESEARCH_REPORT, True, True, True),
        (EvidenceClass.COMMUNITY_LIBRARY, True, True, True),
        (EvidenceClass.OFFICIAL_ARTIFACT, False, True, True),
        (EvidenceClass.OFFICIAL_ARTIFACT, True, False, True),
        (EvidenceClass.OFFICIAL_ARTIFACT, True, True, False),
    ],
)
def test_official_authority_requires_approved_official_artifact_license_and_install(
    evidence_class: EvidenceClass,
    source_approved: bool,
    license_approved: bool,
    install_approved: bool,
) -> None:
    with pytest.raises(ValidationError, match="OFFICIAL"):
        ScheduleProvenance(
            source_id="candidate/v1",
            source_digest="source-sha256:" + "d" * 64,
            parser_id="parser/v1",
            license_profile_id="license/v1",
            install_profile_id="install/v1",
            evidence_class=evidence_class,
            source_artifact_approved=source_approved,
            license_approved=license_approved,
            install_approved=install_approved,
            authority=AuthorityStatus.OFFICIAL,
        )


def _schedule_values(exchange: Exchange, sessions: tuple[TradingSession, ...]) -> dict[str, Any]:
    year = sessions[0].session_date.year
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
    cursor = date(year, 1, 1)
    end = date(year, 12, 31)
    closures: list[date] = []
    while cursor <= end:
        if cursor not in session_dates:
            closures.append(cursor)
        cursor += timedelta(days=1)
    return {
        "schedule_id": f"{exchange.value.lower()}-{year}/test-v1",
        "schedule_digest": "schedule-sha256:" + "0" * 64,
        "exchange": exchange,
        "market": Market.US,
        "timezone": "America/New_York",
        "year": year,
        "coverage_from": date(year, 1, 1),
        "coverage_through": date(year, 12, 31),
        "provenance": provenance,
        "sessions": sessions,
        "closures": tuple(closures),
    }


def _schedule_digest_input(exchange: Exchange, sessions: tuple[TradingSession, ...]) -> str:
    provisional = ExchangeSchedule.model_construct(**_schedule_values(exchange, sessions))
    return canonical_schedule_digest(provisional)


def _official_schedule(
    exchange: Exchange, sessions: tuple[TradingSession, ...]
) -> ExchangeSchedule:
    digest = _schedule_digest_input(exchange, sessions)
    values = _schedule_values(exchange, sessions)
    return ExchangeSchedule(**{**values, "schedule_digest": digest})


def test_runtime_projection_deduplicates_exchange_rows_and_fails_closed_outside_coverage() -> None:
    shared = (_us_session(date(2026, 12, 31)),)
    runtime = MarketScheduleProjection(
        market=Market.US,
        represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
        schedules=(
            _official_schedule(Exchange.NYSE, shared),
            _official_schedule(Exchange.NASDAQ, shared),
        ),
    )

    assert runtime.schedules
    assert not hasattr(runtime, "lookup")
    assert not hasattr(runtime, "next_session")
    assert not hasattr(runtime, "requests_for_session")


def test_self_asserted_official_schedules_cannot_activate_production_runtime() -> None:
    shared = (_us_session(date(2026, 12, 31)),)
    forged: list[ExchangeSchedule] = []
    for exchange in (Exchange.NYSE, Exchange.NASDAQ):
        values = _schedule_values(exchange, shared)
        values["provenance"] = ScheduleProvenance(
            source_id=f"attacker-{exchange.value.lower()}",
            source_digest="source-sha256:" + "a" * 64,
            parser_id="attacker/v1",
            license_profile_id="self-asserted/v1",
            install_profile_id="self-asserted/v1",
            evidence_class=EvidenceClass.OFFICIAL_ARTIFACT,
            source_artifact_approved=True,
            license_approved=True,
            install_approved=True,
            authority=AuthorityStatus.OFFICIAL,
        )
        provisional = ExchangeSchedule.model_construct(**values)
        forged.append(
            ExchangeSchedule(
                **{**values, "schedule_digest": canonical_schedule_digest(provisional)}
            )
        )
    with pytest.raises(ValidationError, match="approved manifest"):
        RuntimeMarketSchedule(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=tuple(forged),
        )
    with pytest.raises(TypeError, match="validated construction"):
        RuntimeMarketSchedule.model_construct(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=tuple(forged),
        )
    bypasses = (
        MarketScheduleProjection.model_construct.__func__(
            RuntimeMarketSchedule,
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=tuple(forged),
        ),
        BaseModel.model_construct.__func__(
            RuntimeMarketSchedule,
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=tuple(forged),
        ),
    )
    for bypassed in bypasses:
        with pytest.raises(RuntimeError, match="approved manifest"):
            bypassed.lookup(date(2026, 12, 31))
        with pytest.raises(RuntimeError, match="approved manifest"):
            bypassed.requests_for_session(date(2026, 12, 31), symbol_count=7)


def test_projection_rejects_empty_and_incomplete_exchange_years() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=(),
        )

    with pytest.raises(ValidationError, match="every represented exchange"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=(_official_schedule(Exchange.NYSE, (_us_session(date(2026, 12, 31)),)),),
        )


def test_projection_rejects_partial_year_coverage() -> None:
    schedules: list[ExchangeSchedule] = []
    for exchange in (Exchange.NYSE, Exchange.NASDAQ):
        values = _schedule_values(exchange, (_us_session(date(2026, 12, 31)),))
        values["coverage_from"] = date(2026, 12, 31)
        values["closures"] = ()
        provisional = ExchangeSchedule.model_construct(**values)
        schedules.append(
            ExchangeSchedule(
                **{**values, "schedule_digest": canonical_schedule_digest(provisional)}
            )
        )
    with pytest.raises(ValidationError, match="full-year"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=tuple(schedules),
        )


def test_projection_rejects_non_adjacent_schedule_years() -> None:
    schedules = tuple(
        _official_schedule(exchange, (_us_session(date(year, 12, 31)),))
        for year in (2026, 2028)
        for exchange in (Exchange.NYSE, Exchange.NASDAQ)
    )
    with pytest.raises(ValidationError, match="consecutive"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=schedules,
        )


def test_cross_year_next_session_requires_adjacent_official_version_for_every_exchange() -> None:
    current = (_us_session(date(2026, 12, 31)),)
    future = (_us_session(date(2027, 1, 4)),)
    with pytest.raises(ValidationError, match="every represented exchange"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=(
                _official_schedule(Exchange.NYSE, current),
                _official_schedule(Exchange.NASDAQ, current),
                _official_schedule(Exchange.NYSE, future),
            ),
        )


def test_next_session_cannot_jump_from_an_uncovered_year_into_a_complete_future_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = (_us_session(date(2027, 1, 4)),)
    schedules = (
        _official_schedule(Exchange.NYSE, future),
        _official_schedule(Exchange.NASDAQ, future),
    )
    monkeypatch.setattr(
        calendar_module,
        "APPROVED_OFFICIAL_SCHEDULES",
        frozenset((item.schedule_id, item.schedule_digest) for item in schedules),
    )
    runtime = RuntimeMarketSchedule(
        market=Market.US,
        represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
        schedules=schedules,
    )

    result = runtime.next_session(date(2026, 12, 31))
    assert result.status is CalendarLookupStatus.CALENDAR_NOT_COVERED
    assert result.session is None


def test_cross_year_next_session_succeeds_only_with_all_adjacent_exchange_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = (_us_session(date(2026, 12, 31)),)
    future = (_us_session(date(2027, 1, 4)),)
    schedules = (
        _official_schedule(Exchange.NYSE, current),
        _official_schedule(Exchange.NASDAQ, current),
        _official_schedule(Exchange.NYSE, future),
        _official_schedule(Exchange.NASDAQ, future),
    )
    monkeypatch.setattr(
        calendar_module,
        "APPROVED_OFFICIAL_SCHEDULES",
        frozenset((item.schedule_id, item.schedule_digest) for item in schedules),
    )
    runtime = RuntimeMarketSchedule(
        market=Market.US,
        represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
        schedules=schedules,
    )

    result = runtime.next_session(date(2026, 12, 31))
    assert result.status is CalendarLookupStatus.SESSION
    assert result.session is not None
    assert result.session.session_date == date(2027, 1, 4)


def test_partial_schedule_cannot_activate_runtime_projection() -> None:
    official = _official_schedule(Exchange.NYSE, (_us_session(date(2026, 12, 31)),))
    partial_provenance = ScheduleProvenance(
        source_id="research-report/v1",
        source_digest="source-sha256:" + "e" * 64,
        parser_id="research-parser/v1",
        license_profile_id="unapproved/v1",
        install_profile_id="private-only/v1",
        evidence_class=EvidenceClass.RESEARCH_REPORT,
        source_artifact_approved=False,
        license_approved=False,
        install_approved=False,
        authority=AuthorityStatus.PARTIAL,
    )
    values = {
        "schedule_id": "nasdaq-2026/research-v1",
        "schedule_digest": "schedule-sha256:" + "0" * 64,
        "exchange": Exchange.NASDAQ,
        "market": official.market,
        "timezone": official.timezone,
        "year": official.year,
        "coverage_from": official.coverage_from,
        "coverage_through": official.coverage_through,
        "provenance": partial_provenance,
        "sessions": official.sessions,
        "closures": official.closures,
    }
    provisional = ExchangeSchedule.model_construct(**values)
    partial = ExchangeSchedule(
        **{**values, "schedule_digest": canonical_schedule_digest(provisional)}
    )

    with pytest.raises(ValidationError, match="one authority status"):
        MarketScheduleProjection(
            market=Market.US,
            represented_exchanges=(Exchange.NYSE, Exchange.NASDAQ),
            schedules=(official, partial),
        )


def test_source_artifact_requires_aware_retrieval_and_sha256_digest() -> None:
    artifact = SourceArtifact(
        source_id="nyse-2026-calendar-pdf/v1",
        canonical_url="https://www.nyse.com/example.pdf",
        retrieved_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        http_status=200,
        content_type="application/pdf",
        raw_sha256="70f5577eb43e60a9dbbecaae3cec23d0f02028c05c7f175013bb3e97816d394f",
    )
    assert artifact.retrieved_at.tzinfo is UTC

    with pytest.raises(ValidationError):
        SourceArtifact(
            source_id="bad/v1",
            canonical_url="https://example.com/calendar",
            retrieved_at=datetime(2026, 8, 12, 12),
            http_status=200,
            content_type="text/html",
            raw_sha256="not-a-sha256",
        )


def _private_source_payload() -> dict[str, object]:
    return {
        "source_artifact": {
            "source_id": "local-research-calendar/v1",
            "canonical_url": "https://example.com/private-source",
            "retrieved_at": "2026-08-12T12:00:00Z",
            "http_status": 200,
            "content_type": "application/json",
            "raw_sha256": "f" * 64,
        },
        "schedule_id": "nyse-2026/research-v1",
        "exchange": "NYSE",
        "market": "US",
        "timezone": "America/New_York",
        "year": 2026,
        "coverage_from": "2026-11-27",
        "coverage_through": "2026-11-27",
        "sessions": [
            {
                "session_date": "2026-11-27",
                "segments": [
                    {
                        "opened_at": "2026-11-27T09:30:00-05:00",
                        "closed_at": "2026-11-27T13:00:00-05:00",
                    }
                ],
                "is_half_day": True,
                "provenance_row_id": "local-row-1",
            }
        ],
        "closures": [],
    }


def test_private_generator_rejects_unapproved_profile_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.generate_private_runtime_calendar import (
        PrivateCalendarGenerationError,
        generate_private_calendar,
    )

    source = tmp_path / "source.json"
    source.write_text(json.dumps(_private_source_payload()), encoding="utf-8")
    output = tmp_path / "private-output"
    monkeypatch.setitem(
        generate_private_calendar.__globals__, "_private_calendar_root", lambda: output
    )
    profile = PrivateInstallProfile(
        profile_id="private-calendar-install/v1",
        license_approved=False,
        install_approved=True,
    )

    with pytest.raises(PrivateCalendarGenerationError, match="profile_not_approved"):
        generate_private_calendar(
            source_path=source,
            profile=profile,
        )
    assert not output.exists()


def test_private_generator_normalizes_invalid_schedule_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.generate_private_runtime_calendar import (
        PrivateCalendarGenerationError,
        generate_private_calendar,
    )

    payload = _private_source_payload()
    payload["timezone"] = "Asia/Shanghai"
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "private-output"
    monkeypatch.setitem(
        generate_private_calendar.__globals__, "_private_calendar_root", lambda: output
    )
    profile = PrivateInstallProfile(
        profile_id="private-calendar-install/v1",
        license_approved=True,
        install_approved=True,
    )

    with pytest.raises(PrivateCalendarGenerationError, match="invalid_source_artifact"):
        generate_private_calendar(
            source_path=source,
            profile=profile,
        )
    assert not output.exists()


def test_private_generator_uses_fixed_private_root_with_user_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.generate_private_runtime_calendar import generate_private_calendar

    source = tmp_path / "source.json"
    source.write_text(json.dumps(_private_source_payload()), encoding="utf-8")
    profile = PrivateInstallProfile(
        profile_id="private-calendar-install/v1",
        license_approved=True,
        install_approved=True,
    )

    output = tmp_path / "user-private-calendars"
    monkeypatch.setitem(
        generate_private_calendar.__globals__, "_private_calendar_root", lambda: output
    )
    generated = generate_private_calendar(
        source_path=source,
        profile=profile,
    )
    payload = json.loads(generated.read_text(encoding="utf-8"))
    assert payload["source_artifact"]["raw_sha256"] == "f" * 64
    assert payload["schedule"]["provenance"]["authority"] == "PARTIAL"
    assert payload["schedule"]["provenance"]["evidence_class"] == "RESEARCH_REPORT"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(generated.stat().st_mode) == 0o600


def test_private_generator_rejects_symlink_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.generate_private_runtime_calendar import (
        PrivateCalendarGenerationError,
        generate_private_calendar,
    )

    source = tmp_path / "source.json"
    source.write_text(json.dumps(_private_source_payload()), encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    output = tmp_path / "output-link"
    output.symlink_to(destination, target_is_directory=True)
    monkeypatch.setitem(
        generate_private_calendar.__globals__, "_private_calendar_root", lambda: output
    )
    profile = PrivateInstallProfile(
        profile_id="private-calendar-install/v1",
        license_approved=True,
        install_approved=True,
    )

    with pytest.raises(PrivateCalendarGenerationError, match="unsafe_output_directory"):
        generate_private_calendar(
            source_path=source,
            profile=profile,
        )
    assert tuple(destination.iterdir()) == ()
