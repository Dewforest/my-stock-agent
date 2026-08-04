from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class MarketDataErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_RANGE = "invalid_range"
    UNSUPPORTED_SYMBOL = "unsupported_symbol"
    DNS = "dns"
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    CONNECTION_RESET = "connection_reset"
    TLS = "tls"
    REDIRECT_DISALLOWED = "redirect_disallowed"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_ENCODING = "invalid_encoding"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    AUTH = "auth"
    THROTTLED = "throttled"
    INFORMATION = "information"
    MALFORMED_JSON = "malformed_json"
    SCHEMA = "schema"
    IDENTITY = "identity"
    NUMERIC = "numeric"
    DUPLICATE = "duplicate"
    OUT_OF_RANGE = "out_of_range"
    EMPTY = "empty"
    COMPACT_COVERAGE = "compact_coverage"
    SCHEDULE = "schedule"
    CLOCK = "clock"
    PERSISTENCE = "persistence"
    PERSISTENCE_CONFLICT = "persistence_conflict"
    MISSING_SESSION_DATA = "missing_session_data"
    LIVE_INCOMPLETE = "live_incomplete"
    EVIDENCE_WRITE = "evidence_write"
    SECRET_POLICY = "secret_policy"
    INTERNAL_CONTRACT = "internal_contract"

    @property
    def retryable(self) -> bool:
        return self in {
            MarketDataErrorCode.DNS,
            MarketDataErrorCode.CONNECT_TIMEOUT,
            MarketDataErrorCode.READ_TIMEOUT,
            MarketDataErrorCode.CONNECTION_RESET,
            MarketDataErrorCode.HTTP_5XX,
            MarketDataErrorCode.THROTTLED,
        }


class MarketDataError(Exception):
    __slots__ = ("_frozen", "code", "metadata", "retryable")

    def __init__(
        self,
        code: MarketDataErrorCode,
        *,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        if type(code) is not MarketDataErrorCode:
            raise TypeError("code must be exactly MarketDataErrorCode")
        safe_metadata = {} if metadata is None else dict(metadata)
        if any(
            type(key) is not str
            or type(value) not in (str, int, bool)
            for key, value in safe_metadata.items()
        ):
            raise TypeError("metadata must contain only safe scalar values")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "retryable", code.retryable)
        object.__setattr__(self, "metadata", MappingProxyType(safe_metadata))
        super().__init__(code.value)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_frozen", False):
            raise TypeError("market data errors are immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"retryable={self.retryable!r}, metadata={dict(self.metadata)!r})"
        )
