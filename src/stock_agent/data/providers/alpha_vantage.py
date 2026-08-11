from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, DecimalException
from typing import NoReturn, Protocol
from urllib.parse import urlencode

from pydantic import ValidationError

from stock_agent.audit import tagged_sha256
from stock_agent.data.providers.errors import MarketDataError, MarketDataErrorCode
from stock_agent.data.providers.http import HttpsTransport
from stock_agent.data.providers.json import strict_json_loads
from stock_agent.data.providers.models import DailyBarRequest, FetchedDailyBar
from stock_agent.domain import Market

_HOST = "www.alphavantage.co"
_PATH = "/query"
_PROVIDER_ID = "alpha-vantage"
_FUNCTION = "TIME_SERIES_DAILY"
_OUTPUT_SIZE = "compact"
_DATA_TYPE = "json"
_METADATA_KEY = "Meta Data"
_SERIES_KEY = "Time Series (Daily)"
_SYMBOL_KEY = "2. Symbol"
_ERROR_MESSAGE_KEY = "Error Message"
_NOTE_KEY = "Note"
_INFORMATION_KEY = "Information"
_RECORD_KEYS = (
    "1. open",
    "2. high",
    "3. low",
    "4. close",
    "5. volume",
)
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CREDENTIAL_MARKERS = ("api key", "apikey")
_CREDENTIAL_FAILURE_MARKERS = ("invalid", "missing")
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "call frequency",
    "calls per minute",
    "calls per day",
)

_SafeMetadata = dict[str, str | int | bool]
_ErrorDescriptor = tuple[MarketDataErrorCode, _SafeMetadata]
_FetchResult = tuple[tuple[FetchedDailyBar, ...] | None, _ErrorDescriptor | None]


class _Transport(Protocol):
    def get(self, *, host: str, target: str) -> bytes: ...


def _raise_safe(descriptor: _ErrorDescriptor) -> NoReturn:
    code, metadata = descriptor
    raise MarketDataError(code, metadata=metadata) from None


def _raise_secret_policy() -> NoReturn:
    raise MarketDataError(MarketDataErrorCode.SECRET_POLICY) from None


def _raise_bad_transport() -> NoReturn:
    raise TypeError("transport must provide get") from None


def _raise_credential_immutable() -> NoReturn:
    raise TypeError("opaque credential is immutable") from None


class _OpaqueApiKey:
    __slots__ = ("_sealed", "_value")

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            del self, name, value
            _raise_credential_immutable()
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            del self, name
            _raise_credential_immutable()
        object.__delattr__(self, name)

    def __repr__(self) -> str:
        return "<opaque Alpha Vantage credential>"

    __str__ = __repr__

    def __copy__(self) -> NoReturn:
        del self
        _raise_secret_policy()

    def __deepcopy__(self, memo: object) -> NoReturn:
        del self, memo
        _raise_secret_policy()

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del self, protocol
        _raise_secret_policy()

    def _reveal(self) -> str:
        return self._value


def _constructor_result(
    api_key: object,
    transport: object | None,
) -> tuple[tuple[_OpaqueApiKey, object] | None, str | None]:
    if (
        type(api_key) is not str
        or not api_key
        or api_key != api_key.strip()
    ):
        return None, "secret"
    selected: object
    try:
        selected = (
            HttpsTransport(allowed_hosts=(_HOST,))
            if transport is None
            else transport
        )
        get = selected.get
    except BaseException:
        return None, "transport"
    if not callable(get):
        return None, "transport"
    return (_OpaqueApiKey(api_key), selected), None


def _request_values(request: DailyBarRequest) -> dict[str, object]:
    if type(request) is not DailyBarRequest:
        raise ValueError("request must be exactly DailyBarRequest")
    if set(request.__dict__) != set(DailyBarRequest.model_fields):
        raise ValueError("request has polluted or missing fields")
    return {name: getattr(request, name) for name in DailyBarRequest.model_fields}


def _clean_request(request: DailyBarRequest) -> DailyBarRequest:
    return DailyBarRequest.model_validate(_request_values(request), strict=True)


def _provider_record_id(
    *,
    native_symbol: str,
    session_text: str,
    raw_values: tuple[str, str, str, str, str],
) -> str:
    return tagged_sha256(
        "provider-payload",
        ("alpha-vantage-daily/v1", native_symbol, session_text, *raw_values),
    )


def _semantic_text(payload: dict[str, object], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if type(value) is not str or not value or value != value.strip():
        raise MarketDataError(MarketDataErrorCode.SCHEMA)
    return value


def _is_credential_error(message: str) -> bool:
    lowered = message.casefold()
    return any(marker in lowered for marker in _CREDENTIAL_MARKERS) and any(
        marker in lowered for marker in _CREDENTIAL_FAILURE_MARKERS
    )


def _is_rate_limit(message: str) -> bool:
    lowered = message.casefold()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _response_category(payload: dict[str, object]) -> MarketDataErrorCode | None:
    error_message = _semantic_text(payload, _ERROR_MESSAGE_KEY)
    if error_message is not None:
        return (
            MarketDataErrorCode.AUTH
            if _is_credential_error(error_message)
            else MarketDataErrorCode.INVALID_REQUEST
        )
    note = _semantic_text(payload, _NOTE_KEY)
    if note is not None:
        return (
            MarketDataErrorCode.THROTTLED
            if _is_rate_limit(note)
            else MarketDataErrorCode.SCHEMA
        )
    information = _semantic_text(payload, _INFORMATION_KEY)
    if information is not None:
        return (
            MarketDataErrorCode.THROTTLED
            if _is_rate_limit(information)
            else MarketDataErrorCode.INFORMATION
        )
    return None


def _parse_session_date(value: object) -> date:
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        raise MarketDataError(MarketDataErrorCode.SCHEMA)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MarketDataError(MarketDataErrorCode.SCHEMA) from None


def _parse_payload(
    *,
    payload: object,
    request: DailyBarRequest,
) -> tuple[FetchedDailyBar, ...]:
    if type(payload) is not dict:
        raise MarketDataError(MarketDataErrorCode.SCHEMA)
    category = _response_category(payload)
    if category is not None:
        raise MarketDataError(category)

    metadata = payload.get(_METADATA_KEY)
    series = payload.get(_SERIES_KEY)
    if type(metadata) is not dict or type(series) is not dict:
        raise MarketDataError(MarketDataErrorCode.SCHEMA)
    native_symbol = metadata.get(_SYMBOL_KEY)
    if type(native_symbol) is not str:
        raise MarketDataError(MarketDataErrorCode.SCHEMA)
    if native_symbol != request.symbol:
        raise MarketDataError(MarketDataErrorCode.IDENTITY)
    if not series:
        raise MarketDataError(MarketDataErrorCode.EMPTY)

    admitted: list[FetchedDailyBar] = []
    for session_text, raw_record in series.items():
        session_date = _parse_session_date(session_text)
        if type(raw_record) is not dict or set(raw_record) != set(_RECORD_KEYS):
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        raw_values: list[str] = []
        for key in _RECORD_KEYS:
            value = raw_record[key]
            if type(value) is not str or not value or value != value.strip():
                raise MarketDataError(MarketDataErrorCode.SCHEMA)
            raw_values.append(value)
        exact_values = tuple(raw_values)
        numeric_error = False
        bar: FetchedDailyBar | None = None
        try:
            bar = FetchedDailyBar(
                market=Market.US,
                symbol=request.symbol,
                session_date=session_date,
                open=Decimal(exact_values[0]),
                high=Decimal(exact_values[1]),
                low=Decimal(exact_values[2]),
                close=Decimal(exact_values[3]),
                volume=Decimal(exact_values[4]),
                provider_id=_PROVIDER_ID,
                provider_native_symbol=native_symbol,
                provider_record_id=_provider_record_id(
                    native_symbol=native_symbol,
                    session_text=session_text,
                    raw_values=exact_values,
                ),
            )
        except (DecimalException, ValueError, ValidationError):
            numeric_error = True
        if numeric_error:
            raise MarketDataError(MarketDataErrorCode.NUMERIC) from None
        assert bar is not None
        admitted.append(bar)

    admitted.sort(key=lambda item: item.session_date)
    earliest_returned = admitted[0].session_date
    if request.start < earliest_returned:
        raise MarketDataError(MarketDataErrorCode.COMPACT_COVERAGE)
    filtered = tuple(
        item
        for item in admitted
        if request.start <= item.session_date <= request.end
    )
    if not filtered:
        raise MarketDataError(MarketDataErrorCode.EMPTY)
    return filtered


def _fetch_result(
    *,
    transport: object,
    credential: _OpaqueApiKey,
    request: DailyBarRequest,
) -> _FetchResult:
    try:
        clean_request = _clean_request(request)
        if clean_request.market is not Market.US:
            raise MarketDataError(MarketDataErrorCode.UNSUPPORTED_SYMBOL)
        parameters = (
            ("function", _FUNCTION),
            ("symbol", clean_request.symbol),
            ("outputsize", _OUTPUT_SIZE),
            ("datatype", _DATA_TYPE),
            ("apikey", credential._reveal()),
        )
        target = f"{_PATH}?{urlencode(parameters)}"
        body = transport.get(host=_HOST, target=target)  # type: ignore[attr-defined]
        if type(body) is not bytes:
            raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
        payload = strict_json_loads(body)
        bars = _parse_payload(payload=payload, request=clean_request)
    except MarketDataError as error:
        return None, (error.code, dict(error.metadata))
    except BaseException:
        return None, (MarketDataErrorCode.INTERNAL_CONTRACT, {})
    return bars, None


class AlphaVantageDailyBarProvider:
    __slots__ = ("_credential", "_transport")

    def __init__(
        self,
        *,
        api_key: str,
        transport: _Transport | None = None,
    ) -> None:
        state, error_kind = _constructor_result(api_key, transport)
        api_key = None  # type: ignore[assignment]
        transport = None
        if error_kind is not None:
            state = None
            self = None  # type: ignore[assignment]
            if error_kind == "secret":
                _raise_secret_policy()
            _raise_bad_transport()
        assert state is not None
        credential, selected = state
        self._credential = credential
        self._transport = selected

    @property
    def provider_id(self) -> str:
        return _PROVIDER_ID

    def __repr__(self) -> str:
        return (
            "AlphaVantageDailyBarProvider("
            f"provider_id={_PROVIDER_ID!r}, credential=<opaque>)"
        )

    __str__ = __repr__

    def __copy__(self) -> NoReturn:
        del self
        _raise_secret_policy()

    def __deepcopy__(self, memo: object) -> NoReturn:
        del self, memo
        _raise_secret_policy()

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        del self, protocol
        _raise_secret_policy()

    def fetch_daily_bars(
        self,
        request: DailyBarRequest,
    ) -> tuple[FetchedDailyBar, ...]:
        try:
            outcome = _fetch_result(
                transport=self._transport,
                credential=self._credential,
                request=request,
            )
        except BaseException:
            outcome = (None, (MarketDataErrorCode.INTERNAL_CONTRACT, {}))
        bars, descriptor = outcome
        outcome = None  # type: ignore[assignment]
        self = None  # type: ignore[assignment]
        request = None  # type: ignore[assignment]
        if descriptor is not None:
            _raise_safe(descriptor)
        assert bars is not None
        return bars


__all__ = ["AlphaVantageDailyBarProvider"]
