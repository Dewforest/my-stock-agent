from dataclasses import FrozenInstanceError
from datetime import date, datetime

import pytest

import stock_agent.market as market_module
from stock_agent.domain import Market
from stock_agent.market import NoFutureSession, TradingCalendar


@pytest.fixture
def cn_calendar() -> TradingCalendar:
    return TradingCalendar(
        market=Market.CN,
        sessions=[date(2026, 7, 24), date(2026, 7, 27)],
    )


@pytest.fixture
def us_calendar() -> TradingCalendar:
    return TradingCalendar(
        market=Market.US,
        sessions=[date(2026, 7, 24), date(2026, 7, 27)],
    )


def test_next_session_skips_unlisted_weekend_and_holiday(
    cn_calendar: TradingCalendar,
    us_calendar: TradingCalendar,
) -> None:
    assert cn_calendar.next_session(date(2026, 7, 24)) == date(2026, 7, 27)
    assert us_calendar.next_session(date(2026, 7, 24)) == date(2026, 7, 27)


def test_is_session_checks_only_explicitly_listed_dates(
    us_calendar: TradingCalendar,
) -> None:
    assert us_calendar.is_session(date(2026, 7, 24))
    assert not us_calendar.is_session(date(2026, 7, 25))
    assert not us_calendar.is_session(date(2026, 7, 28))


def test_cn_and_us_calendars_are_independent() -> None:
    cn_calendar = TradingCalendar(Market.CN, [date(2026, 7, 27)])
    us_calendar = TradingCalendar(Market.US, [date(2026, 7, 28)])

    assert cn_calendar.is_session(date(2026, 7, 27))
    assert not us_calendar.is_session(date(2026, 7, 27))


def test_sessions_are_sorted_into_an_immutable_tuple() -> None:
    calendar = TradingCalendar(
        Market.US,
        [date(2026, 7, 28), date(2026, 7, 24), date(2026, 7, 27)],
    )

    assert calendar.sessions == (
        date(2026, 7, 24),
        date(2026, 7, 27),
        date(2026, 7, 28),
    )


def test_sessions_generator_is_materialized_once() -> None:
    yielded = 0

    def generate_sessions():
        nonlocal yielded
        for session in [date(2026, 7, 27), date(2026, 7, 24)]:
            yielded += 1
            yield session

    calendar = TradingCalendar(Market.US, generate_sessions())

    assert yielded == 2
    assert calendar.sessions == (date(2026, 7, 24), date(2026, 7, 27))


def test_mutating_source_list_does_not_change_calendar() -> None:
    sessions = [date(2026, 7, 24)]
    calendar = TradingCalendar(Market.US, sessions)

    sessions.append(date(2026, 7, 27))

    assert calendar.sessions == (date(2026, 7, 24),)


def test_duplicate_sessions_are_rejected() -> None:
    with pytest.raises(ValueError):
        TradingCalendar(
            Market.US,
            [date(2026, 7, 24), date(2026, 7, 24)],
        )


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 24), "2026-07-24"])
def test_sessions_reject_non_plain_dates(invalid: object) -> None:
    with pytest.raises(TypeError):
        TradingCalendar(Market.US, [invalid])  # type: ignore[list-item]


def test_market_rejects_raw_string() -> None:
    with pytest.raises(TypeError):
        TradingCalendar("US", [date(2026, 7, 24)])  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 24), "2026-07-24"])
def test_is_session_rejects_non_plain_dates(invalid: object) -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 24)])

    with pytest.raises(TypeError):
        calendar.is_session(invalid)  # type: ignore[arg-type]


def test_empty_calendar_has_no_future_session() -> None:
    calendar = TradingCalendar(Market.CN, [])

    assert calendar.sessions == ()
    with pytest.raises(NoFutureSession):
        calendar.next_session(date(2026, 7, 24))


def test_last_session_has_no_future_session() -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 24)])

    with pytest.raises(NoFutureSession):
        calendar.next_session(date(2026, 7, 24))


def test_next_session_is_strictly_greater_than_after() -> None:
    calendar = TradingCalendar(
        Market.US,
        [date(2026, 7, 24), date(2026, 7, 27), date(2026, 7, 28)],
    )

    assert calendar.next_session(date(2026, 7, 24)) == date(2026, 7, 27)
    assert calendar.next_session(date(2026, 7, 25)) == date(2026, 7, 27)


def test_no_future_session_message_contains_market_and_after() -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 24)])
    after = date(2026, 7, 24)

    with pytest.raises(NoFutureSession) as error:
        calendar.next_session(after)

    assert Market.US.value in str(error.value)
    assert after.isoformat() in str(error.value)


@pytest.mark.parametrize("invalid", [datetime(2026, 7, 24), "2026-07-24"])
def test_next_session_rejects_non_plain_dates(invalid: object) -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 27)])

    with pytest.raises(TypeError):
        calendar.next_session(invalid)  # type: ignore[arg-type]


def test_calendar_attributes_are_frozen() -> None:
    calendar = TradingCalendar(Market.US, [date(2026, 7, 24)])

    with pytest.raises(FrozenInstanceError):
        calendar.market = Market.CN
    with pytest.raises(FrozenInstanceError):
        calendar.sessions = ()


def test_market_package_exports_only_public_calendar_api() -> None:
    assert market_module.__all__ == ["NoFutureSession", "TradingCalendar"]
    assert market_module.NoFutureSession is NoFutureSession
    assert market_module.TradingCalendar is TradingCalendar
