from __future__ import annotations

import http.client
import math
import socket
import ssl
from typing import NoReturn
from urllib.parse import urlsplit

from stock_agent.data.providers.errors import MarketDataError, MarketDataErrorCode

MAX_RESPONSE_BYTES = 1024 * 1024
_ErrorDescriptor = tuple[MarketDataErrorCode, dict[str, str | int | bool]]
_GetResult = tuple[bytes | None, _ErrorDescriptor | None]


def _transport_error_descriptor(*, error: BaseException, stage: str) -> _ErrorDescriptor:
    if isinstance(error, socket.gaierror):
        code = MarketDataErrorCode.DNS
    elif isinstance(error, ssl.SSLError):
        code = MarketDataErrorCode.TLS
    elif isinstance(error, TimeoutError):
        code = (
            MarketDataErrorCode.CONNECT_TIMEOUT
            if stage == "connect"
            else MarketDataErrorCode.READ_TIMEOUT
        )
    elif isinstance(
        error,
        (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError, BrokenPipeError),
    ):
        code = MarketDataErrorCode.CONNECTION_RESET
    elif isinstance(error, OSError):
        code = MarketDataErrorCode.CONNECTION_RESET
    else:
        code = MarketDataErrorCode.INTERNAL_CONTRACT
    return code, {"stage": stage}


def _error(
    code: MarketDataErrorCode,
    metadata: dict[str, str | int | bool] | None = None,
) -> _GetResult:
    return None, (code, {} if metadata is None else metadata)


def _raise_safe(descriptor: _ErrorDescriptor) -> NoReturn:
    code, metadata = descriptor
    raise MarketDataError(code, metadata=metadata) from None


def _close_quietly(connection: object) -> None:
    try:
        close = connection.close
    except BaseException:
        return
    try:
        close()
    except BaseException:
        return


def _status_descriptor(status: object) -> _ErrorDescriptor | None:
    if type(status) is not int:
        return MarketDataErrorCode.INTERNAL_CONTRACT, {}
    if status == 200:
        return None
    metadata: dict[str, str | int | bool] = {"status": status}
    if 300 <= status <= 399:
        return MarketDataErrorCode.REDIRECT_DISALLOWED, metadata
    if status in (401, 403):
        return MarketDataErrorCode.AUTH, metadata
    if status == 429:
        return MarketDataErrorCode.THROTTLED, metadata
    if 400 <= status <= 499:
        return MarketDataErrorCode.HTTP_4XX, metadata
    if 500 <= status <= 599:
        return MarketDataErrorCode.HTTP_5XX, metadata
    return MarketDataErrorCode.INTERNAL_CONTRACT, metadata


def _perform_get(
    *,
    allowed_hosts: frozenset[str],
    timeout_seconds: float,
    host: object,
    target: object,
) -> _GetResult:
    if type(host) is not str or host not in allowed_hosts:
        return _error(MarketDataErrorCode.INVALID_REQUEST)
    if (
        type(target) is not str
        or not target.startswith("/")
        or target.startswith("//")
        or "#" in target
        or "\r" in target
        or "\n" in target
    ):
        return _error(MarketDataErrorCode.INVALID_REQUEST)

    connection: object | None = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=timeout_seconds)
        connection.connect()  # type: ignore[attr-defined]
    except BaseException as error:
        descriptor = _transport_error_descriptor(error=error, stage="connect")
        if connection is not None:
            _close_quietly(connection)
        return None, descriptor
    assert connection is not None

    try:
        socket_object = connection.sock  # type: ignore[attr-defined]
    except BaseException:
        _close_quietly(connection)
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT, {"stage": "read"})
    if socket_object is None:
        _close_quietly(connection)
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
    try:
        socket_object.settimeout(timeout_seconds)
    except BaseException:
        _close_quietly(connection)
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT, {"stage": "read"})

    try:
        connection.request("GET", target)  # type: ignore[attr-defined]
    except BaseException as error:
        descriptor = _transport_error_descriptor(error=error, stage="request")
        _close_quietly(connection)
        return None, descriptor

    try:
        response = connection.getresponse()  # type: ignore[attr-defined]
    except BaseException as error:
        descriptor = _transport_error_descriptor(error=error, stage="response")
        _close_quietly(connection)
        return None, descriptor

    try:
        status_value = response.status
    except BaseException:
        _close_quietly(connection)
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
    status_descriptor = _status_descriptor(status_value)
    if status_descriptor is not None:
        _close_quietly(connection)
        return None, status_descriptor

    try:
        content_encoding = response.getheader("Content-Encoding")
        content_length = response.getheader("Content-Length")
    except BaseException:
        _close_quietly(connection)
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
    if content_encoding is not None and (
        type(content_encoding) is not str or content_encoding != "identity"
    ):
        _close_quietly(connection)
        return _error(MarketDataErrorCode.UNSUPPORTED_ENCODING)
    if content_length is not None:
        if type(content_length) is not str or not content_length.isascii():
            _close_quietly(connection)
            return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
        stripped_length = content_length.strip()
        if not stripped_length.isdigit():
            _close_quietly(connection)
            return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
        normalized_length = stripped_length.lstrip("0") or "0"
        maximum_length = str(MAX_RESPONSE_BYTES)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length)
            and normalized_length > maximum_length
        ):
            _close_quietly(connection)
            return _error(
                MarketDataErrorCode.RESPONSE_TOO_LARGE,
                {"limit_bytes": MAX_RESPONSE_BYTES},
            )

    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except BaseException as error:
        descriptor = _transport_error_descriptor(error=error, stage="read")
        _close_quietly(connection)
        return None, descriptor
    _close_quietly(connection)
    if type(body) is not bytes:
        return _error(MarketDataErrorCode.INTERNAL_CONTRACT)
    if len(body) > MAX_RESPONSE_BYTES:
        return _error(
            MarketDataErrorCode.RESPONSE_TOO_LARGE,
            {"limit_bytes": MAX_RESPONSE_BYTES},
        )
    return body, None


class HttpsTransport:
    __slots__ = ("_allowed_hosts", "_timeout_seconds")

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float = 10.0,
    ) -> None:
        if type(allowed_hosts) is not tuple or not allowed_hosts:
            raise TypeError("allowed_hosts must be a nonempty exact tuple")
        if any(
            type(host) is not str
            or not host
            or host != host.strip()
            or not host.isascii()
            or urlsplit(f"https://{host}").hostname != host
            for host in allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain exact HTTPS host names")
        if len(allowed_hosts) != len(set(allowed_hosts)):
            raise ValueError("allowed_hosts must be unique")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._allowed_hosts = frozenset(allowed_hosts)
        self._timeout_seconds = float(timeout_seconds)

    def get(self, *, host: str, target: str) -> bytes:
        result = _perform_get(
            allowed_hosts=self._allowed_hosts,
            timeout_seconds=self._timeout_seconds,
            host=host,
            target=target,
        )
        self = None  # type: ignore[assignment]
        host = None  # type: ignore[assignment]
        target = None  # type: ignore[assignment]
        body, descriptor = result
        result = None  # type: ignore[assignment]
        if descriptor is not None:
            _raise_safe(descriptor)
        assert body is not None
        return body


__all__ = ["MAX_RESPONSE_BYTES", "HttpsTransport"]
