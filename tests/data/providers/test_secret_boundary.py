from __future__ import annotations

import copy
import json
import logging
import os
import pickle
import socket
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    BoundedSessionSchedule,
    DailyBarRequest,
    IncrementalBarIngestor,
    MarketDataError,
    MarketDataErrorCode,
    SessionScheduleRow,
)
from stock_agent.data.providers.alpha_vantage import AlphaVantageDailyBarProvider
from stock_agent.domain import Market
from tests.data.providers.exception_graph import (
    assert_exception_graph_excludes,
    capture_market_data_error,
)

HOST = "www.alphavantage.co"
SECRET_KEY = "".join(("CAP3_SECRET_", "CANARY_", "f71a93"))
BODY_CANARY = "".join(("CAP3_BODY_", "CANARY_", "09cd"))
PAYLOAD_CANARY = "".join(("CAP3_NATIVE_", "PAYLOAD_", "b861"))


@pytest.fixture(scope="module", autouse=True)
def _block_real_sockets() -> Any:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Capability3 secret tests must never open sockets")

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket, "create_connection", forbidden)
    patcher.setattr(socket, "create_server", forbidden)
    patcher.setattr(socket, "fromfd", forbidden)
    patcher.setattr(socket, "socketpair", forbidden)
    patcher.setattr(socket, "socket", forbidden)
    yield
    patcher.undo()


def _request() -> DailyBarRequest:
    return DailyBarRequest(
        market=Market.US,
        symbol="IBM",
        start=date(2025, 7, 1),
        end=date(2025, 7, 1),
        price_mode="RAW",
    )


def _schedule() -> BoundedSessionSchedule:
    zone = ZoneInfo("America/New_York")
    session_date = date(2025, 7, 1)
    return BoundedSessionSchedule(
        market=Market.US,
        start=session_date,
        end=session_date,
        sessions=(
            SessionScheduleRow(
                session_date=session_date,
                open_at=datetime.combine(session_date, time(9, 30), zone),
                close_at=datetime.combine(session_date, time(16), zone),
                timezone="America/New_York",
                provenance="secret-boundary-schedule",
                generated_on=date(2026, 8, 4),
            ),
        ),
    )


def _success_body() -> bytes:
    return json.dumps(
        {
            "Meta Data": {"2. Symbol": "IBM"},
            "Time Series (Daily)": {
                "2025-07-01": {
                    "1. open": "295.00",
                    "2. high": "297.00",
                    "3. low": "294.00",
                    "4. close": "296.00",
                    "5. volume": "3000000",
                }
            },
        },
        separators=(",", ":"),
    ).encode()


class FakeTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, str]] = []

    def get(self, *, host: str, target: str) -> bytes:
        self.calls.append((host, target))
        return self.body


class ExplodingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, *, host: str, target: str) -> bytes:
        self.calls.append((host, target))
        raise RuntimeError(f"{PAYLOAD_CANARY}:{target}:{BODY_CANARY}")


def _capture_any(call: Callable[[], object]) -> BaseException:
    caught: BaseException | None = None
    try:
        call()
    except BaseException as error:
        caught = error
    finally:
        call = None  # type: ignore[assignment]
    if caught is None:
        raise AssertionError("expected an exception")
    return caught


def _assert_renderings_exclude(value: object, canaries: tuple[str, ...]) -> None:
    rendered = (str(value), repr(value))
    for canary in canaries:
        assert all(canary not in item for item in rendered)


def test_provider_and_transport_representations_are_opaque() -> None:
    transport = FakeTransport(_success_body())
    provider = AlphaVantageDailyBarProvider(api_key=SECRET_KEY, transport=transport)

    bars = provider.fetch_daily_bars(_request())

    full_target = transport.calls[0][1]
    assert dict(parse_qsl(urlsplit(full_target).query))["apikey"] == SECRET_KEY
    for value in (provider, transport):
        _assert_renderings_exclude(value, (SECRET_KEY, full_target))
    assert not hasattr(provider, "__dict__")
    assert not hasattr(provider, "model_dump")
    assert SECRET_KEY not in bars[0].model_dump_json()
    assert SECRET_KEY not in bars[0].provider_record_id


def test_private_credential_wrapper_is_immutable_opaque_and_failure_safe() -> None:
    provider = AlphaVantageDailyBarProvider(
        api_key=SECRET_KEY,
        transport=FakeTransport(_success_body()),
    )
    credential = provider._credential  # type: ignore[attr-defined]

    _assert_renderings_exclude(credential, (SECRET_KEY,))
    caught = _capture_any(
        lambda: setattr(credential, "_value", "replacement")
    )

    assert isinstance(caught, TypeError)
    assert_exception_graph_excludes(caught, (SECRET_KEY,))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "operation",
    [
        lambda provider: copy.copy(provider),
        lambda provider: copy.deepcopy(provider),
        lambda provider: pickle.dumps(provider),
    ],
)
def test_copy_deepcopy_and_pickle_are_rejected_without_secret_graph(
    operation: Callable[[AlphaVantageDailyBarProvider], object],
) -> None:
    provider = AlphaVantageDailyBarProvider(
        api_key=SECRET_KEY,
        transport=FakeTransport(_success_body()),
    )

    caught = _capture_any(lambda: operation(provider))

    assert isinstance(caught, MarketDataError)
    assert caught.code is MarketDataErrorCode.SECRET_POLICY
    assert caught.__cause__ is None
    assert caught.__context__ is None
    assert_exception_graph_excludes(caught, (SECRET_KEY,))


def test_transport_failure_drops_key_full_target_body_native_exception_and_provider_state() -> None:
    transport = ExplodingTransport()
    provider = AlphaVantageDailyBarProvider(api_key=SECRET_KEY, transport=transport)

    caught = capture_market_data_error(lambda: provider.fetch_daily_bars(_request()))

    assert caught.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert caught.metadata == {}
    assert caught.args == ("internal_contract",)
    assert caught.__cause__ is None
    assert caught.__context__ is None
    full_target = transport.calls[0][1]
    assert_exception_graph_excludes(
        caught,
        (SECRET_KEY, full_target, BODY_CANARY, PAYLOAD_CANARY),
    )
    formatted = "".join(traceback.format_exception(caught))
    assert all(
        canary not in formatted
        for canary in (SECRET_KEY, full_target, BODY_CANARY, PAYLOAD_CANARY)
    )


def test_shared_ingestor_failure_drops_provider_credential_from_its_traceback_frames() -> None:
    transport = ExplodingTransport()
    provider = AlphaVantageDailyBarProvider(api_key=SECRET_KEY, transport=transport)
    store = PointInTimeStore()
    try:
        ingestor = IncrementalBarIngestor(provider=provider, store=store)
        caught = capture_market_data_error(
            lambda: ingestor.ingest(
                request=_request(),
                schedule=_schedule(),
                ingested_at=datetime(2025, 7, 2, 12, tzinfo=UTC),
            )
        )
    finally:
        store.close()

    full_target = transport.calls[0][1]
    assert caught.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert_exception_graph_excludes(
        caught,
        (SECRET_KEY, full_target, BODY_CANARY, PAYLOAD_CANARY),
    )


def test_parser_failure_drops_body_and_native_payload_from_every_provider_frame() -> None:
    body = json.dumps(
        {
            "Meta Data": {"2. Symbol": "IBM"},
            "Time Series (Daily)": {
                "2025-07-01": {
                    "1. open": PAYLOAD_CANARY,
                    "2. high": "297.00",
                    "3. low": "294.00",
                    "4. close": "296.00",
                    "5. volume": "3000000",
                    "secret-shaped-provider-field": SECRET_KEY,
                }
            },
            "body_canary": BODY_CANARY,
        },
        separators=(",", ":"),
    ).encode()
    provider = AlphaVantageDailyBarProvider(
        api_key=SECRET_KEY,
        transport=FakeTransport(body),
    )

    caught = capture_market_data_error(lambda: provider.fetch_daily_bars(_request()))

    assert caught.code is MarketDataErrorCode.SCHEMA
    assert_exception_graph_excludes(
        caught,
        (SECRET_KEY, body, body.decode(), BODY_CANARY, PAYLOAD_CANARY),
    )


def test_invalid_key_and_bad_transport_constructor_failures_clear_provider_frames() -> None:
    invalid_key = "".join((" ", SECRET_KEY, " "))
    invalid = _capture_any(
        lambda: AlphaVantageDailyBarProvider(
            api_key=invalid_key,
            transport=FakeTransport(_success_body()),
        )
    )
    assert isinstance(invalid, MarketDataError)
    assert invalid.code is MarketDataErrorCode.SECRET_POLICY
    assert_exception_graph_excludes(invalid, (SECRET_KEY, invalid_key))

    bad_transport = _capture_any(
        lambda: AlphaVantageDailyBarProvider(api_key=SECRET_KEY, transport=object())
    )
    assert isinstance(bad_transport, TypeError)
    assert_exception_graph_excludes(  # type: ignore[arg-type]
        bad_transport,
        (SECRET_KEY,),
    )


def test_shared_ingestor_constructor_failure_drops_provider_credential() -> None:
    provider = AlphaVantageDailyBarProvider(
        api_key=SECRET_KEY,
        transport=FakeTransport(_success_body()),
    )

    caught = _capture_any(
        lambda: IncrementalBarIngestor(provider=provider, store=object())  # type: ignore[arg-type]
    )

    assert isinstance(caught, TypeError)
    assert_exception_graph_excludes(caught, (SECRET_KEY,))  # type: ignore[arg-type]


def test_explicit_key_injection_never_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_environment = os.environ

    class ForbiddenEnvironment:
        def __getitem__(self, key: object) -> object:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not read environment credentials")
            return original_environment[key]  # type: ignore[index]

        def get(self, key: object, default: object = None) -> object:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not read environment credentials")
            return original_environment.get(key, default)  # type: ignore[arg-type]

        def __contains__(self, key: object) -> bool:
            if key == "ALPHA_VANTAGE_API_KEY":
                raise AssertionError("standard tests must not read environment credentials")
            return key in original_environment

        def __setitem__(self, key: str, value: str) -> None:
            original_environment[key] = value

        def __delitem__(self, key: str) -> None:
            del original_environment[key]

    def guarded_getenv(key: str, default: str | None = None) -> str | None:
        if key == "ALPHA_VANTAGE_API_KEY":
            raise AssertionError("standard tests must not read environment credentials")
        return original_environment.get(key, default)

    monkeypatch.setattr(os, "environ", ForbiddenEnvironment())
    monkeypatch.setattr(os, "getenv", guarded_getenv)
    provider = AlphaVantageDailyBarProvider(
        api_key=SECRET_KEY,
        transport=FakeTransport(_success_body()),
    )

    assert provider.fetch_daily_bars(_request())


def test_logs_outputs_errors_and_model_dumps_never_expose_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(_success_body())
    provider = AlphaVantageDailyBarProvider(api_key=SECRET_KEY, transport=transport)
    bars = provider.fetch_daily_bars(_request())
    error = capture_market_data_error(
        lambda: AlphaVantageDailyBarProvider(
            api_key=SECRET_KEY,
            transport=FakeTransport(b'{"Information":"premium endpoint"}'),
        ).fetch_daily_bars(_request())
    )

    with caplog.at_level(logging.INFO):
        logging.getLogger("capability3.secret").info(
            "provider=%s bars=%s error=%s",
            provider,
            bars,
            error,
        )

    observable = "\n".join(
        (
            caplog.text,
            str(provider),
            repr(provider),
            repr(bars),
            bars[0].model_dump_json(),
            str(error),
            repr(error),
            "".join(traceback.format_exception(error)),
        )
    )
    assert SECRET_KEY not in observable
    assert transport.calls[0][1] not in observable
