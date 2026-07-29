import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal


def canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("canonical Decimal must be finite")
    if value.is_zero():
        return "0"

    decimal_tuple = value.as_tuple()
    digits = list(decimal_tuple.digits)
    exponent = int(decimal_tuple.exponent)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    sign = "-" if decimal_tuple.sign else ""
    return f"{sign}{coefficient}e{exponent}"


def canonical_datetime(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("canonical datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json_string(fields: tuple[object, ...]) -> str:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def canonical_json_bytes(fields: tuple[object, ...]) -> bytes:
    return canonical_json_string(fields).encode("utf-8")


def tagged_sha256(tag: str, fields: tuple[object, ...]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
    return f"{tag}-sha256:{digest}"
