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

_HOST = "push2his.eastmoney.com"
_PATH = "/api/qt/stock/kline/get"
_PROVIDER_ID = "eastmoney"
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class _Transport(Protocol):
    def get(self, *, host: str, target: str) -> bytes: ...


def _request_values(request: DailyBarRequest) -> dict[str, object]:
    if type(request) is not DailyBarRequest:
        raise ValueError("request must be exactly DailyBarRequest")
    if set(request.__dict__) != set(DailyBarRequest.model_fields):
        raise ValueError("request has polluted or missing fields")
    return {name: getattr(request, name) for name in DailyBarRequest.model_fields}


def _clean_request(request: DailyBarRequest) -> DailyBarRequest:
    admission_error = False
    clean: DailyBarRequest | None = None
    try:
        clean = DailyBarRequest.model_validate(_request_values(request), strict=True)
    except (TypeError, ValueError, ValidationError):
        admission_error = True
    if admission_error:
        raise MarketDataError(MarketDataErrorCode.INVALID_REQUEST) from None
    assert clean is not None
    return clean


def _market_number(symbol: str) -> int:
    if len(symbol) == 6 and symbol.isascii() and symbol.isdigit():
        if symbol.startswith("6"):
            return 1
        if symbol.startswith(("0", "3")):
            return 0
    raise MarketDataError(MarketDataErrorCode.UNSUPPORTED_SYMBOL)


def _provider_record_id(*, native_symbol: str, fields: tuple[str, ...]) -> str:
    return tagged_sha256(
        "provider-payload",
        ("eastmoney-kline/v1", native_symbol, *fields),
    )


_ErrorDescriptor = tuple[MarketDataErrorCode, dict[str, str | int | bool]]


def _raise_safe(descriptor: _ErrorDescriptor) -> NoReturn:
    code, metadata = descriptor
    raise MarketDataError(code, metadata=metadata) from None


class EastmoneyDailyBarProvider:
    __slots__ = ("_transport",)

    def __init__(self, *, transport: _Transport | None = None) -> None:
        selected: object = (
            HttpsTransport(allowed_hosts=(_HOST,)) if transport is None else transport
        )
        try:
            get = selected.get
        except AttributeError as error:
            raise TypeError("transport must provide get") from error
        if not callable(get):
            raise TypeError("transport must provide get")
        self._transport = selected

    @property
    def provider_id(self) -> str:
        return _PROVIDER_ID

    def fetch_daily_bars(
        self,
        request: DailyBarRequest,
    ) -> tuple[FetchedDailyBar, ...]:
        result: tuple[FetchedDailyBar, ...] | None = None
        descriptor: _ErrorDescriptor | None = None
        try:
            result = self._fetch_daily_bars(request)
        except MarketDataError as error:
            descriptor = error.code, dict(error.metadata)
        except BaseException:
            descriptor = MarketDataErrorCode.INTERNAL_CONTRACT, {}
        self = None  # type: ignore[assignment]
        request = None  # type: ignore[assignment]
        if descriptor is not None:
            _raise_safe(descriptor)
        assert result is not None
        return result

    def _fetch_daily_bars(
        self,
        request: DailyBarRequest,
    ) -> tuple[FetchedDailyBar, ...]:
        clean_request = _clean_request(request)
        if clean_request.market is not Market.CN:
            raise MarketDataError(MarketDataErrorCode.UNSUPPORTED_SYMBOL)
        market_number = _market_number(clean_request.symbol)
        native_symbol = f"{market_number}.{clean_request.symbol}"
        parameters = (
            ("secid", native_symbol),
            ("beg", clean_request.start.strftime("%Y%m%d")),
            ("end", clean_request.end.strftime("%Y%m%d")),
            ("klt", "101"),
            ("fqt", "0"),
            ("fields1", "f1,f2,f3,f4,f5,f6"),
            ("fields2", "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"),
        )
        target = f"{_PATH}?{urlencode(parameters)}"

        transport_error: MarketDataError | None = None
        body: object = None
        try:
            body = self._transport.get(host=_HOST, target=target)  # type: ignore[union-attr]
        except MarketDataError as error:
            transport_error = MarketDataError(error.code, metadata=error.metadata)
        except BaseException:
            transport_error = MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)
        if transport_error is not None:
            raise transport_error from None
        if type(body) is not bytes:
            raise MarketDataError(MarketDataErrorCode.INTERNAL_CONTRACT)

        json_error: MarketDataError | None = None
        payload: object = None
        try:
            payload = strict_json_loads(body)
        except MarketDataError as error:
            json_error = MarketDataError(error.code, metadata=error.metadata)
        if json_error is not None:
            raise json_error from None
        return self._parse_payload(
            payload=payload,
            request=clean_request,
            market_number=market_number,
            native_symbol=native_symbol,
        )

    @staticmethod
    def _parse_payload(
        *,
        payload: object,
        request: DailyBarRequest,
        market_number: int,
        native_symbol: str,
    ) -> tuple[FetchedDailyBar, ...]:
        if type(payload) is not dict:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        rc = payload.get("rc")
        if type(rc) is not int:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        if rc != 0:
            raise MarketDataError(MarketDataErrorCode.INFORMATION)
        data = payload.get("data")
        if type(data) is not dict:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        code = data.get("code")
        native_market = data.get("market")
        rows = data.get("klines")
        if type(code) is not str or len(code) != 6:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        if code != request.symbol:
            raise MarketDataError(MarketDataErrorCode.IDENTITY)
        if type(native_market) is not int:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        if native_market != market_number:
            raise MarketDataError(MarketDataErrorCode.IDENTITY)
        if type(rows) is not list:
            raise MarketDataError(MarketDataErrorCode.SCHEMA)
        if not rows:
            raise MarketDataError(MarketDataErrorCode.EMPTY)

        bars: list[FetchedDailyBar] = []
        dates: set[date] = set()
        for row in rows:
            if type(row) is not str:
                raise MarketDataError(MarketDataErrorCode.SCHEMA)
            raw_fields = row.split(",")
            if len(raw_fields) != 11 or any(not field or not field.strip() for field in raw_fields):
                raise MarketDataError(MarketDataErrorCode.SCHEMA)
            if any(field != field.strip() for field in raw_fields):
                raise MarketDataError(MarketDataErrorCode.SCHEMA)
            fields = tuple(raw_fields)
            if _DATE_PATTERN.fullmatch(fields[0]) is None:
                raise MarketDataError(MarketDataErrorCode.SCHEMA)
            date_error = False
            session_date: date | None = None
            try:
                session_date = date.fromisoformat(fields[0])
            except ValueError:
                date_error = True
            if date_error:
                raise MarketDataError(MarketDataErrorCode.SCHEMA) from None
            assert session_date is not None
            if session_date in dates:
                raise MarketDataError(MarketDataErrorCode.DUPLICATE)
            dates.add(session_date)
            if not request.start <= session_date <= request.end:
                raise MarketDataError(MarketDataErrorCode.OUT_OF_RANGE)

            numeric_error = False
            bar: FetchedDailyBar | None = None
            try:
                bar = FetchedDailyBar(
                    market=Market.CN,
                    symbol=request.symbol,
                    session_date=session_date,
                    open=Decimal(fields[1]),
                    close=Decimal(fields[2]),
                    high=Decimal(fields[3]),
                    low=Decimal(fields[4]),
                    volume=Decimal(fields[5]),
                    provider_id=_PROVIDER_ID,
                    provider_native_symbol=native_symbol,
                    provider_record_id=_provider_record_id(
                        native_symbol=native_symbol,
                        fields=fields,
                    ),
                )
            except (DecimalException, ValueError, ValidationError):
                numeric_error = True
            if numeric_error:
                raise MarketDataError(MarketDataErrorCode.NUMERIC) from None
            assert bar is not None
            bars.append(bar)
        bars.sort(key=lambda item: item.session_date)
        return tuple(bars)


__all__ = ["EastmoneyDailyBarProvider"]
