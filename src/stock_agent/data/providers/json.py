from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, NoReturn

from stock_agent.data.providers.errors import MarketDataError, MarketDataErrorCode


class _DuplicateKey(ValueError):
    pass


class _NonFiniteConstant(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _NonFiniteConstant(value)


def _load_result(body: object) -> tuple[object | None, MarketDataErrorCode | None]:
    if type(body) is not bytes or body.startswith(b"\xef\xbb\xbf"):
        return None, MarketDataErrorCode.INVALID_ENCODING
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, MarketDataErrorCode.INVALID_ENCODING

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        return None, MarketDataErrorCode.DUPLICATE
    except (json.JSONDecodeError, _NonFiniteConstant, ValueError):
        return None, MarketDataErrorCode.MALFORMED_JSON
    except Exception:
        return None, MarketDataErrorCode.INTERNAL_CONTRACT
    return parsed, None


def _raise_safe(code: MarketDataErrorCode) -> NoReturn:
    raise MarketDataError(code) from None


def strict_json_loads(body: bytes) -> object:
    result = _load_result(body)
    body = None  # type: ignore[assignment]
    parsed, error_code = result
    result = None  # type: ignore[assignment]
    if error_code is not None:
        _raise_safe(error_code)
    return parsed


__all__ = ["strict_json_loads"]
