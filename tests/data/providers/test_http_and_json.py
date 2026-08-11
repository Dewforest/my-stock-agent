from __future__ import annotations

import http.client
import socket
import ssl
from contextlib import contextmanager
from decimal import Decimal
from types import TracebackType
from typing import Any

import pytest

from stock_agent.data.providers import MarketDataError, MarketDataErrorCode
from stock_agent.data.providers.http import MAX_RESPONSE_BYTES, HttpsTransport
from stock_agent.data.providers.json import strict_json_loads
from tests.data.providers.exception_graph import (
    assert_exception_graph_excludes,
    capture_market_data_error,
)

HOST = "push2his.eastmoney.com"


class StrSubclass(str):
    pass


@pytest.fixture(scope="module", autouse=True)
def _block_real_sockets() -> Any:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Capability2 tests must never open sockets")

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket, "create_connection", forbidden)
    patcher.setattr(socket, "create_server", forbidden)
    patcher.setattr(socket, "fromfd", forbidden)
    patcher.setattr(socket, "socketpair", forbidden)
    patcher.setattr(socket, "socket", forbidden)
    yield
    patcher.undo()


class FakeSocket:
    def __init__(self, error: BaseException | None = None) -> None:
        self.timeouts: list[float] = []
        self.error = error

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)
        if self.error is not None:
            raise self.error


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._headers = {} if headers is None else headers
        self._read_error = read_error
        self.read_amounts: list[int] = []

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)

    def read(self, amount: int) -> bytes:
        self.read_amounts.append(amount)
        if self._read_error is not None:
            raise self._read_error
        return self._body[:amount]


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse,
        *,
        connect_error: BaseException | None = None,
        request_error: BaseException | None = None,
        response_error: BaseException | None = None,
        socket_timeout_error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.request_error = request_error
        self.response_error = response_error
        self.sock = FakeSocket(socket_timeout_error)
        self.connect_calls = 0
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    def request(
        self,
        method: str,
        target: str,
        body: object = None,
        headers: dict[str, str] | None = None,
        *,
        encode_chunked: bool = False,
    ) -> None:
        assert body is None
        assert encode_chunked is False
        self.requests.append((method, target, {} if headers is None else headers))
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self) -> FakeResponse:
        if self.response_error is not None:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


def _install_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: FakeConnection,
) -> list[tuple[str, float]]:
    constructed: list[tuple[str, float]] = []

    def factory(host: str, *, timeout: float) -> FakeConnection:
        constructed.append((host, timeout))
        return connection

    monkeypatch.setattr(http.client, "HTTPSConnection", factory)
    return constructed


def _transport(timeout: float = 7.25) -> HttpsTransport:
    return HttpsTransport(allowed_hosts=(HOST,), timeout_seconds=timeout)


def test_market_data_error_domain_is_immutable_but_linkage_is_assignable() -> None:
    error = MarketDataError(
        MarketDataErrorCode.SCHEDULE,
        metadata={"session_date": "2026-07-23"},
    )
    original_args = error.args

    for name, value in (
        ("code", MarketDataErrorCode.AUTH),
        ("retryable", True),
        ("metadata", {}),
        ("args", ("auth",)),
    ):
        with pytest.raises(TypeError, match="domain payload is immutable"):
            setattr(error, name, value)
    for name in ("code", "retryable", "metadata", "args"):
        with pytest.raises(TypeError, match="domain payload is immutable"):
            delattr(error, name)
    with pytest.raises(TypeError):
        error.metadata["body"] = "secret"  # type: ignore[index]

    assert error.code is MarketDataErrorCode.SCHEDULE
    assert error.retryable is False
    assert error.metadata == {"session_date": "2026-07-23"}
    assert error.args == original_args

    cause = RuntimeError("safe cause")
    context = ValueError("safe context")
    error.__traceback__ = None
    error.__cause__ = cause
    error.__context__ = context
    error.__suppress_context__ = True
    assert error.__traceback__ is None
    assert error.__cause__ is cause
    assert error.__context__ is context
    assert error.__suppress_context__ is True


def test_market_data_error_propagates_through_generator_contextmanager() -> None:
    @contextmanager
    def passthrough() -> Any:
        yield

    error = MarketDataError(MarketDataErrorCode.NUMERIC)
    with pytest.raises(MarketDataError) as caught:
        with passthrough():
            raise error

    assert caught.value is error
    traceback = caught.value.__traceback__
    assert isinstance(traceback, TracebackType)
    assert error.code is MarketDataErrorCode.NUMERIC
    assert error.args == ("numeric",)


def test_exception_graph_ignores_caller_frames_but_not_provider_frames() -> None:
    def caller_failure() -> None:
        caller_canary = "CAP2_CALLER_FRAME_ALLOWED_1b6c"
        assert caller_canary
        raise MarketDataError(MarketDataErrorCode.SCHEMA)

    caller_error = capture_market_data_error(caller_failure)
    assert_exception_graph_excludes(caller_error, ("CAP2_CALLER_FRAME_ALLOWED_1b6c",))

    namespace: dict[str, Any] = {
        "MarketDataError": MarketDataError,
        "MarketDataErrorCode": MarketDataErrorCode,
        "__name__": "stock_agent.data.providers.synthetic_test_frame",
    }
    exec(
        "def provider_failure():\n"
        "    provider_canary = 'CAP2_PROVIDER_FRAME_FORBIDDEN_b75d'\n"
        "    raise MarketDataError(MarketDataErrorCode.SCHEMA)\n",
        namespace,
    )
    provider_error = capture_market_data_error(namespace["provider_failure"])
    with pytest.raises(AssertionError, match="canary leaked"):
        assert_exception_graph_excludes(
            provider_error,
            ("CAP2_PROVIDER_FRAME_FORBIDDEN_b75d",),
        )


def test_https_transport_uses_fixed_get_host_target_and_one_connect_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        body=b'{"ok":true}',
        headers={"Content-Length": "11", "Content-Encoding": "identity"},
    )
    connection = FakeConnection(response)
    constructed = _install_connection(monkeypatch, connection)

    body = _transport().get(host=HOST, target="/fixed/path?a=1")

    assert body == b'{"ok":true}'
    assert constructed == [(HOST, 7.25)]
    assert connection.connect_calls == 1
    assert connection.sock.timeouts == [7.25]
    assert connection.requests == [("GET", "/fixed/path?a=1", {})]
    assert response.read_amounts == [MAX_RESPONSE_BYTES + 1]
    assert connection.closed is True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.gaierror("CANARY query"), MarketDataErrorCode.DNS),
        (TimeoutError("CANARY query"), MarketDataErrorCode.CONNECT_TIMEOUT),
        (ConnectionResetError("CANARY query"), MarketDataErrorCode.CONNECTION_RESET),
        (ssl.SSLError("CANARY query"), MarketDataErrorCode.TLS),
    ],
)
def test_connect_failures_are_stage_specific_and_drop_original_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected: MarketDataErrorCode,
) -> None:
    connection = FakeConnection(FakeResponse(), connect_error=error)
    _install_connection(monkeypatch, connection)

    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed?secret=CANARY")

    assert caught.value.code is expected
    assert caught.value.metadata == {"stage": "connect"}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CANARY" not in repr(caught.value)
    assert connection.closed is True


@pytest.mark.parametrize(
    ("phase", "error", "expected"),
    [
        ("request", ConnectionResetError("CANARY"), MarketDataErrorCode.CONNECTION_RESET),
        ("response", TimeoutError("CANARY"), MarketDataErrorCode.READ_TIMEOUT),
        ("read", TimeoutError("CANARY"), MarketDataErrorCode.READ_TIMEOUT),
        ("read", ConnectionResetError("CANARY"), MarketDataErrorCode.CONNECTION_RESET),
        ("read", ssl.SSLError("CANARY"), MarketDataErrorCode.TLS),
    ],
)
def test_request_and_read_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: BaseException,
    expected: MarketDataErrorCode,
) -> None:
    response = FakeResponse(read_error=error if phase == "read" else None)
    connection = FakeConnection(
        response,
        request_error=error if phase == "request" else None,
        response_error=error if phase == "response" else None,
    )
    _install_connection(monkeypatch, connection)

    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed?secret=CANARY")

    assert caught.value.code is expected
    assert caught.value.metadata == {"stage": phase}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CANARY" not in repr(caught.value)


def test_socket_timeout_configuration_failure_drops_original_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        FakeResponse(),
        socket_timeout_error=RuntimeError("CANARY native failure"),
    )
    _install_connection(monkeypatch, connection)

    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed?secret=CANARY")

    assert caught.value.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert caught.value.metadata == {"stage": "read"}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CANARY" not in repr(caught.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (301, MarketDataErrorCode.REDIRECT_DISALLOWED),
        (401, MarketDataErrorCode.AUTH),
        (403, MarketDataErrorCode.AUTH),
        (404, MarketDataErrorCode.HTTP_4XX),
        (429, MarketDataErrorCode.THROTTLED),
        (500, MarketDataErrorCode.HTTP_5XX),
    ],
)
def test_non_200_status_mapping_never_reads_response_body(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: MarketDataErrorCode,
) -> None:
    response = FakeResponse(status=status, body=b"must not be read")
    _install_connection(monkeypatch, FakeConnection(response))

    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed")

    assert caught.value.code is expected
    assert caught.value.metadata == {"status": status}
    assert response.read_amounts == []


def test_transport_rejects_redirect_like_nonstandard_success_and_disallowed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(status=204)
    _install_connection(monkeypatch, FakeConnection(response))
    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed")
    assert caught.value.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert response.read_amounts == []

    with pytest.raises(MarketDataError) as disallowed:
        _transport().get(host="evil.example", target="/fixed")
    assert disallowed.value.code is MarketDataErrorCode.INVALID_REQUEST


def test_declared_oversize_and_unsupported_encoding_fail_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversize = FakeResponse(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
    _install_connection(monkeypatch, FakeConnection(oversize))
    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed")
    assert caught.value.code is MarketDataErrorCode.RESPONSE_TOO_LARGE
    assert oversize.read_amounts == []

    compressed = FakeResponse(headers={"Content-Encoding": "gzip"})
    _install_connection(monkeypatch, FakeConnection(compressed))
    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed")
    assert caught.value.code is MarketDataErrorCode.UNSUPPORTED_ENCODING
    assert compressed.read_amounts == []


def test_hostile_digit_only_content_length_is_safely_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "3141592653589793238462643383279"
    content_length = "9" * 2500 + canary + "9" * 2500
    response = FakeResponse(headers={"Content-Length": content_length})
    _install_connection(monkeypatch, FakeConnection(response))

    caught = capture_market_data_error(
        lambda: _transport().get(host=HOST, target="/fixed")
    )

    assert caught.code is MarketDataErrorCode.RESPONSE_TOO_LARGE
    assert response.read_amounts == []
    assert_exception_graph_excludes(caught, (canary, content_length))


@pytest.mark.parametrize(
    "content_encoding",
    ["", " ", "IDENTITY", "Identity", b"identity", StrSubclass("identity")],
)
def test_content_encoding_accepts_only_absent_or_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    content_encoding: object,
) -> None:
    response = FakeResponse()
    response._headers["Content-Encoding"] = content_encoding  # type: ignore[assignment]
    _install_connection(monkeypatch, FakeConnection(response))

    caught = capture_market_data_error(
        lambda: _transport().get(host=HOST, target="/fixed")
    )

    assert caught.code is MarketDataErrorCode.UNSUPPORTED_ENCODING
    assert response.read_amounts == []


def test_unknown_or_chunked_length_is_bounded_to_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1))
    _install_connection(monkeypatch, FakeConnection(response))

    with pytest.raises(MarketDataError) as caught:
        _transport().get(host=HOST, target="/fixed")

    assert caught.value.code is MarketDataErrorCode.RESPONSE_TOO_LARGE
    assert response.read_amounts == [MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_hosts": []},
        {"allowed_hosts": ()},
        {"allowed_hosts": (HOST, HOST)},
        {"allowed_hosts": (" push2his.eastmoney.com",)},
        {"allowed_hosts": (HOST,), "timeout_seconds": True},
        {"allowed_hosts": (HOST,), "timeout_seconds": 0},
        {"allowed_hosts": (HOST,), "timeout_seconds": float("inf")},
    ],
)
def test_transport_configuration_is_exact_and_bounded(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        HttpsTransport(**kwargs)


def test_strict_json_loader_preserves_decimal_without_binary_float() -> None:
    parsed = strict_json_loads(b'{"integer":1,"decimal":0.1000000000000000001}')
    assert type(parsed) is dict
    assert type(parsed["integer"]) is int
    assert type(parsed["decimal"]) is Decimal
    assert parsed["decimal"] == Decimal("0.1000000000000000001")


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":1,"a":2}',
        b'{"outer":{"a":1,"a":2}}',
        b'[{"a":1,"a":2}]',
    ],
)
def test_strict_json_loader_rejects_duplicate_keys_at_every_level(body: bytes) -> None:
    with pytest.raises(MarketDataError) as caught:
        strict_json_loads(body)
    assert caught.value.code is MarketDataErrorCode.DUPLICATE


@pytest.mark.parametrize(
    "body",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{} trailing',
        b'\xff',
        b'\xef\xbb\xbf{}',
    ],
)
def test_strict_json_loader_rejects_constants_garbage_utf8_and_bom(body: bytes) -> None:
    with pytest.raises(MarketDataError) as caught:
        strict_json_loads(body)
    expected = (
        MarketDataErrorCode.INVALID_ENCODING
        if body in (b"\xff", b"\xef\xbb\xbf{}")
        else MarketDataErrorCode.MALFORMED_JSON
    )
    assert caught.value.code is expected


def test_transport_failure_exception_graph_cannot_reach_request_response_or_native_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "/fixed?secret=CAP2_TARGET_QUERY_9f41"
    body = b'CAP2_BODY_BYTES_273c {"payload":"CAP2_PAYLOAD_TEXT_a84e"}'
    native_message = "CAP2_NATIVE_EXCEPTION_6d12"
    connection = FakeConnection(
        FakeResponse(body=body),
        request_error=ConnectionResetError(native_message),
    )
    _install_connection(monkeypatch, connection)

    caught = capture_market_data_error(
        lambda: _transport().get(host=HOST, target=target)
    )

    assert caught.code is MarketDataErrorCode.CONNECTION_RESET
    assert_exception_graph_excludes(
        caught,
        (target, body, body.decode(), native_message),
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"secret":"CAP2_JSON_TEXT_d91b",}', MarketDataErrorCode.MALFORMED_JSON),
        (b'CAP2_JSON_BYTES_0a77\xff', MarketDataErrorCode.INVALID_ENCODING),
    ],
)
def test_strict_json_failure_exception_graph_cannot_reach_body_or_decoded_text(
    body: bytes,
    expected: MarketDataErrorCode,
) -> None:
    caught = capture_market_data_error(lambda: strict_json_loads(body))

    assert caught.code is expected
    assert_exception_graph_excludes(caught, (body, body.decode(errors="replace")))


def test_deeply_nested_json_native_failure_is_safely_wrapped() -> None:
    canary = b"CAP2_DEEP_JSON_7f32"
    body = b"[" * 2000 + b'"' + canary + b'"' + b"]" * 2000

    caught = capture_market_data_error(lambda: strict_json_loads(body))

    assert caught.code is MarketDataErrorCode.INTERNAL_CONTRACT
    assert_exception_graph_excludes(caught, (body, canary, canary.decode()))


def test_standard_fake_transport_path_does_not_attempt_a_socket() -> None:
    assert strict_json_loads(b'{"network":"not-used"}') == {"network": "not-used"}


def test_module_socket_blocker_rejects_socket_construction_and_connection() -> None:
    with pytest.raises(AssertionError, match="must never open sockets"):
        socket.socket()
    with pytest.raises(AssertionError, match="must never open sockets"):
        socket.create_connection((HOST, 443))
