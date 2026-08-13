from __future__ import annotations

import http.client
import math
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOKEN_BYTES = 4096


class LLMHTTPErrorCode(StrEnum):
    CREDENTIAL = "credential"
    REQUEST = "request"
    STATUS = "status"
    ENCODING = "encoding"
    CONTENT_TYPE = "content_type"
    SIZE = "size"
    PROTOCOL = "protocol"
    TRANSPORT = "transport"


class LLMHTTPError(Exception):
    """Stable, secret-free failure from the bounded LLM HTTPS client."""

    def __init__(self, code: LLMHTTPErrorCode) -> None:
        self.code = code
        super().__init__(
            "LLM HTTPS request failed"
            if code is LLMHTTPErrorCode.TRANSPORT
            else "LLM HTTPS response rejected"
            if code
            in {
                LLMHTTPErrorCode.STATUS,
                LLMHTTPErrorCode.ENCODING,
                LLMHTTPErrorCode.CONTENT_TYPE,
                LLMHTTPErrorCode.SIZE,
                LLMHTTPErrorCode.PROTOCOL,
            }
            else "LLM bearer credential unavailable"
            if code is LLMHTTPErrorCode.CREDENTIAL
            else "LLM HTTPS request rejected"
        )


class BearerTokenSource(Protocol):
    def bearer_token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class EnvironmentBearerTokenSource:
    variable_name: str

    def __post_init__(self) -> None:
        if (
            type(self.variable_name) is not str
            or not self.variable_name
            or self.variable_name != self.variable_name.strip()
            or not self.variable_name.isascii()
            or not self.variable_name.replace("_", "A").isalnum()
        ):
            raise ValueError("environment variable name is invalid")

    def bearer_token(self) -> str:
        return os.getenv(self.variable_name)  # type: ignore[return-value]


class HttpsConnection(Protocol):
    sock: object

    def connect(self) -> None: ...

    def request(
        self,
        method: str,
        target: str,
        body: bytes,
        headers: dict[str, str],
        *,
        encode_chunked: bool,
    ) -> None: ...

    def getresponse(self) -> object: ...

    def close(self) -> None: ...


class ConnectionFactory(Protocol):
    def __call__(self, host: str, timeout_seconds: float) -> HttpsConnection: ...


_ErrorDescriptor = tuple[LLMHTTPErrorCode, bool]
_PostResult = tuple[bytes | None, _ErrorDescriptor | None]


def _default_connection_factory(host: str, timeout_seconds: float) -> HttpsConnection:
    return http.client.HTTPSConnection(host, timeout=timeout_seconds)


class BoundedBearerHttpsClient:
    __slots__ = (
        "_allowed_endpoints",
        "_connection_factory",
        "_timeout_seconds",
        "_token_source",
    )

    def __init__(
        self,
        *,
        token_source: BearerTokenSource,
        allowed_endpoints: tuple[tuple[str, str], ...],
        timeout_seconds: float = 10.0,
        connection_factory: ConnectionFactory = _default_connection_factory,
    ) -> None:
        if type(allowed_endpoints) is not tuple or not allowed_endpoints:
            raise TypeError("allowed_endpoints must be a nonempty exact tuple")
        normalized: set[tuple[str, str]] = set()
        for endpoint in allowed_endpoints:
            if type(endpoint) is not tuple or len(endpoint) != 2:
                raise TypeError("each endpoint must be an exact host-target pair")
            host, target = endpoint
            if not _valid_host(host) or not _valid_target(target):
                raise ValueError("allowed endpoint is invalid")
            normalized.add(endpoint)
        if len(normalized) != len(allowed_endpoints):
            raise ValueError("allowed endpoints must be unique")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if not callable(getattr(token_source, "bearer_token", None)):
            raise TypeError("token_source must provide bearer_token")
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._token_source = token_source
        self._allowed_endpoints = frozenset(normalized)
        self._timeout_seconds = float(timeout_seconds)
        self._connection_factory = connection_factory

    def post(self, *, host: str, target: str, body: bytes) -> bytes:
        result = _perform_post(
            allowed_endpoints=self._allowed_endpoints,
            token_source=self._token_source,
            timeout_seconds=self._timeout_seconds,
            connection_factory=self._connection_factory,
            host=host,
            target=target,
            body=body,
        )
        self = None  # type: ignore[assignment]
        host = None  # type: ignore[assignment]
        target = None  # type: ignore[assignment]
        body = None  # type: ignore[assignment]
        payload, descriptor = result
        result = None  # type: ignore[assignment]
        if descriptor is not None:
            _raise_safe(descriptor)
        assert payload is not None
        return payload


def _perform_post(
    *,
    allowed_endpoints: frozenset[tuple[str, str]],
    token_source: BearerTokenSource,
    timeout_seconds: float,
    connection_factory: ConnectionFactory,
    host: object,
    target: object,
    body: object,
) -> _PostResult:
    if (
        type(host) is not str
        or type(target) is not str
        or (host, target) not in allowed_endpoints
        or type(body) is not bytes
        or not body
        or len(body) > MAX_REQUEST_BYTES
    ):
        return _failure(LLMHTTPErrorCode.REQUEST)

    token: object = None
    try:
        token = token_source.bearer_token()
    except BaseException:
        return _failure(LLMHTTPErrorCode.CREDENTIAL)
    if not _valid_token(token):
        token = None
        return _failure(LLMHTTPErrorCode.CREDENTIAL)

    connection: HttpsConnection | None = None
    try:
        connection = connection_factory(host, timeout_seconds)
        connection.connect()
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError)
        token = None
        if connection is not None:
            _close_quietly(connection)
        return None, (LLMHTTPErrorCode.TRANSPORT, timed_out)

    try:
        connection.sock.settimeout(timeout_seconds)  # type: ignore[attr-defined]
    except BaseException:
        token = None
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.TRANSPORT)

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        connection.request(
            "POST", target, body, headers, encode_chunked=False
        )
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError)
        token = None
        headers = None  # type: ignore[assignment]
        _close_quietly(connection)
        return None, (LLMHTTPErrorCode.TRANSPORT, timed_out)
    token = None
    headers = None  # type: ignore[assignment]

    try:
        response = connection.getresponse()
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError)
        _close_quietly(connection)
        return None, (LLMHTTPErrorCode.TRANSPORT, timed_out)

    try:
        status = response.status
    except BaseException:
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.PROTOCOL)
    if type(status) is not int or status != 200:
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.STATUS)

    try:
        content_encoding = response.getheader("Content-Encoding")
        content_type = response.getheader("Content-Type")
        content_length = response.getheader("Content-Length")
    except BaseException:
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.PROTOCOL)
    if content_encoding is not None and (
        type(content_encoding) is not str or content_encoding != "identity"
    ):
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.ENCODING)
    if not _valid_json_content_type(content_type):
        _close_quietly(connection)
        return _failure(LLMHTTPErrorCode.CONTENT_TYPE)
    length_error = _content_length_error(content_length)
    content_encoding = content_type = content_length = None
    if length_error is not None:
        _close_quietly(connection)
        return _failure(length_error)

    try:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError)
        _close_quietly(connection)
        return None, (LLMHTTPErrorCode.TRANSPORT, timed_out)
    _close_quietly(connection)
    if type(payload) is not bytes:
        return _failure(LLMHTTPErrorCode.PROTOCOL)
    if len(payload) > MAX_RESPONSE_BYTES:
        payload = None
        return _failure(LLMHTTPErrorCode.SIZE)
    return payload, None


def _valid_token(token: object) -> bool:
    return (
        type(token) is str
        and 1 <= len(token) <= MAX_TOKEN_BYTES
        and token.isascii()
        and not any(character.isspace() or ord(character) < 33 for character in token)
    )


def _valid_host(host: object) -> bool:
    return (
        type(host) is str
        and bool(host)
        and host.isascii()
        and host == host.lower()
        and all(part and part.replace("-", "a").isalnum() for part in host.split("."))
    )


def _valid_target(target: object) -> bool:
    return (
        type(target) is str
        and target.startswith("/")
        and not target.startswith("//")
        and target.isascii()
        and all(character not in target for character in "?#\r\n")
    )


def _valid_json_content_type(value: object) -> bool:
    return type(value) is str and (
        value == "application/json" or value.startswith("application/json;")
    )


def _content_length_error(value: object) -> LLMHTTPErrorCode | None:
    if value is None:
        return None
    if type(value) is not str or not value.isascii() or not value.isdigit():
        return LLMHTTPErrorCode.PROTOCOL
    normalized = value.lstrip("0") or "0"
    maximum = str(MAX_RESPONSE_BYTES)
    if len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    ):
        return LLMHTTPErrorCode.SIZE
    return None


def _failure(code: LLMHTTPErrorCode) -> _PostResult:
    return None, (code, False)


def _raise_safe(descriptor: _ErrorDescriptor) -> NoReturn:
    code, timeout = descriptor
    if timeout:
        raise TimeoutError("LLM HTTPS request timed out") from None
    raise LLMHTTPError(code) from None


def _close_quietly(connection: object) -> None:
    try:
        close = connection.close
    except BaseException:
        return
    try:
        close()
    except BaseException:
        return


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "BearerTokenSource",
    "BoundedBearerHttpsClient",
    "EnvironmentBearerTokenSource",
    "LLMHTTPError",
    "LLMHTTPErrorCode",
]
