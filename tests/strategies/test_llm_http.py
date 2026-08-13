from __future__ import annotations

import os
import socket
import ssl
from collections.abc import Callable

import pytest

from stock_agent.strategies.llm_http import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    BoundedBearerHttpsClient,
    EnvironmentBearerTokenSource,
    LLMHTTPError,
    LLMHTTPErrorCode,
)

HOST = "api.openai.com"
TARGET = "/v1/chat/completions"
TOKEN = "CAP_LLM_TOKEN_f71a93"
BODY = b'{"model":"safe"}'
NATIVE = "CAP_LLM_NATIVE_b861"
RESPONSE_CANARY = "CAP_LLM_RESPONSE_09cd"
TARGET_CANARY = "/v1/chat/completions?canary=TARGET"


class FakeSocket:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)
        if self.error:
            raise self.error


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, object] | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.read_error = read_error
        self.read_amounts: list[int] = []

    def getheader(self, name: str) -> object | None:
        return self.headers.get(name)

    def read(self, amount: int) -> bytes:
        self.read_amounts.append(amount)
        if self.read_error:
            raise self.read_error
        return self.body[:amount]


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse,
        *,
        connect_error: BaseException | None = None,
        request_error: BaseException | None = None,
        response_error: BaseException | None = None,
        timeout_error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.request_error = request_error
        self.response_error = response_error
        self.sock = FakeSocket(timeout_error)
        self.connect_calls = 0
        self.requests: list[tuple[str, str, bytes, dict[str, str], bool]] = []
        self.closed = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error:
            raise self.connect_error

    def request(
        self,
        method: str,
        target: str,
        body: bytes,
        headers: dict[str, str],
        *,
        encode_chunked: bool,
    ) -> None:
        self.requests.append((method, target, body, headers, encode_chunked))
        if self.request_error:
            raise self.request_error

    def getresponse(self) -> FakeResponse:
        if self.response_error:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


class TokenSpy:
    def __init__(self, token: object = TOKEN) -> None:
        self.token = token
        self.calls = 0

    def bearer_token(self) -> str:
        self.calls += 1
        return self.token  # type: ignore[return-value]


def _client(
    connection: FakeConnection,
    token_source: object | None = None,
    *,
    timeout: float = 7.25,
) -> tuple[BoundedBearerHttpsClient, list[tuple[str, float]]]:
    constructed: list[tuple[str, float]] = []

    def factory(host: str, timeout_seconds: float) -> FakeConnection:
        constructed.append((host, timeout_seconds))
        return connection

    return (
        BoundedBearerHttpsClient(
            token_source=token_source or TokenSpy(),  # type: ignore[arg-type]
            allowed_endpoints=((HOST, TARGET),),
            timeout_seconds=timeout,
            connection_factory=factory,
        ),
        constructed,
    )


def _capture(call: Callable[[], object]) -> BaseException:
    try:
        call()
    except BaseException as error:
        return error
    raise AssertionError("expected exception")


def _assert_safe(error: BaseException, canaries: tuple[object, ...]) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    observable = str(error) + repr(error)
    traceback = error.__traceback__
    while traceback:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        if module == "stock_agent.strategies.llm_http":
            observable += repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    for canary in canaries:
        if isinstance(canary, bytes):
            assert canary not in observable.encode(errors="ignore")
        else:
            assert str(canary) not in observable


def test_environment_source_stores_name_and_reads_only_at_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def getenv(name: str) -> str:
        calls.append(name)
        return TOKEN

    monkeypatch.setattr(os, "getenv", getenv)
    source = EnvironmentBearerTokenSource("OPENAI_TEST_KEY")
    assert calls == []
    assert repr(source) == "EnvironmentBearerTokenSource(variable_name='OPENAI_TEST_KEY')"
    assert source.bearer_token() == TOKEN
    assert calls == ["OPENAI_TEST_KEY"]


@pytest.mark.parametrize(
    "token",
    [None, "", " ", "abc def", "abc\n", "é", "x" * 4097, 1, True],
)
def test_invalid_token_fails_before_connection(token: object) -> None:
    source = TokenSpy(token)
    connection = FakeConnection(FakeResponse())
    client, constructed = _client(connection, source)

    error = _capture(lambda: client.post(host=HOST, target=TARGET, body=BODY))

    assert isinstance(error, LLMHTTPError)
    assert error.code is LLMHTTPErrorCode.CREDENTIAL
    assert constructed == []
    sensitive = (token,) if isinstance(token, str) and len(token) > 2 else ()
    _assert_safe(error, (*sensitive, BODY, TARGET))


def test_post_uses_exact_https_request_headers_and_bounds() -> None:
    response = FakeResponse(
        body=b'{"ok":true}',
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": "11",
            "Content-Encoding": "identity",
        },
    )
    connection = FakeConnection(response)
    client, constructed = _client(connection)

    result = client.post(host=HOST, target=TARGET, body=BODY)

    assert result == b'{"ok":true}'
    assert constructed == [(HOST, 7.25)]
    assert connection.connect_calls == 1
    assert connection.sock.timeouts == [7.25]
    assert connection.requests == [
        (
            "POST",
            TARGET,
            BODY,
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            False,
        )
    ]
    assert response.read_amounts == [MAX_RESPONSE_BYTES + 1]
    assert connection.closed is True


@pytest.mark.parametrize(
    ("host", "target", "body"),
    [
        ("evil.example", TARGET, BODY),
        (HOST, "/other", BODY),
        (HOST, TARGET_CANARY, BODY),
        (HOST, TARGET, "not-bytes"),
        (HOST, TARGET, b""),
        (HOST, TARGET, b"x" * (MAX_REQUEST_BYTES + 1)),
    ],
)
def test_request_admission_precedes_token_and_connection(
    host: object, target: object, body: object
) -> None:
    source = TokenSpy()
    client, constructed = _client(FakeConnection(FakeResponse()), source)

    error = _capture(
        lambda: client.post(host=host, target=target, body=body)  # type: ignore[arg-type]
    )

    assert isinstance(error, LLMHTTPError)
    assert error.code is LLMHTTPErrorCode.REQUEST
    assert source.calls == 0
    assert constructed == []


@pytest.mark.parametrize("status", [201, 204, 301, 307, 400, 401, 403, 429, 500, 599])
def test_only_status_200_is_accepted_and_error_body_is_not_read(status: int) -> None:
    response = FakeResponse(status=status, body=RESPONSE_CANARY.encode())
    connection = FakeConnection(response)
    client, _ = _client(connection)

    error = _capture(lambda: client.post(host=HOST, target=TARGET, body=BODY))

    assert isinstance(error, LLMHTTPError)
    assert error.code is LLMHTTPErrorCode.STATUS
    assert response.read_amounts == []
    assert connection.closed is True
    _assert_safe(error, (TOKEN, BODY, TARGET, RESPONSE_CANARY))


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Content-Type": "text/plain"}, LLMHTTPErrorCode.CONTENT_TYPE),
        ({"Content-Type": "Application/JSON"}, LLMHTTPErrorCode.CONTENT_TYPE),
        ({"Content-Type": b"application/json"}, LLMHTTPErrorCode.CONTENT_TYPE),
        (
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            LLMHTTPErrorCode.ENCODING,
        ),
        (
            {"Content-Type": "application/json", "Content-Encoding": "Identity"},
            LLMHTTPErrorCode.ENCODING,
        ),
        (
            {"Content-Type": "application/json", "Content-Length": str(MAX_RESPONSE_BYTES + 1)},
            LLMHTTPErrorCode.SIZE,
        ),
        (
            {"Content-Type": "application/json", "Content-Length": "invalid"},
            LLMHTTPErrorCode.PROTOCOL,
        ),
    ],
)
def test_response_headers_are_exact_and_checked_before_read(
    headers: dict[str, object], code: LLMHTTPErrorCode
) -> None:
    response = FakeResponse(headers=headers)
    client, _ = _client(FakeConnection(response))
    error = _capture(lambda: client.post(host=HOST, target=TARGET, body=BODY))
    assert isinstance(error, LLMHTTPError)
    assert error.code is code
    assert response.read_amounts == []


def test_actual_response_size_is_bounded_and_connection_closed() -> None:
    response = FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1))
    connection = FakeConnection(response)
    client, _ = _client(connection)
    error = _capture(lambda: client.post(host=HOST, target=TARGET, body=BODY))
    assert isinstance(error, LLMHTTPError)
    assert error.code is LLMHTTPErrorCode.SIZE
    assert connection.closed is True


@pytest.mark.parametrize(
    ("phase", "native", "timeout"),
    [
        ("connect", RuntimeError(NATIVE), False),
        ("connect", TimeoutError(NATIVE), True),
        ("request", ssl.SSLError(NATIVE), False),
        ("response", socket.gaierror(NATIVE), False),
        ("read", ConnectionResetError(NATIVE), False),
        ("read", TimeoutError(NATIVE), True),
        ("settimeout", RuntimeError(NATIVE), False),
    ],
)
def test_native_failures_are_sanitized_closed_and_timeout_preserved(
    phase: str, native: BaseException, timeout: bool
) -> None:
    response = FakeResponse(
        body=RESPONSE_CANARY.encode(), read_error=native if phase == "read" else None
    )
    connection = FakeConnection(
        response,
        connect_error=native if phase == "connect" else None,
        request_error=native if phase == "request" else None,
        response_error=native if phase == "response" else None,
        timeout_error=native if phase == "settimeout" else None,
    )
    client, _ = _client(connection)

    error = _capture(lambda: client.post(host=HOST, target=TARGET, body=BODY))

    assert isinstance(error, TimeoutError if timeout else LLMHTTPError)
    assert str(error) == ("LLM HTTPS request timed out" if timeout else "LLM HTTPS request failed")
    assert connection.closed is True
    _assert_safe(error, (TOKEN, BODY, TARGET, NATIVE, RESPONSE_CANARY))
