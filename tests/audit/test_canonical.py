import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Context, Decimal, getcontext, setcontext

import stock_agent.audit as audit
from stock_agent.audit import (
    canonical_datetime,
    canonical_decimal,
    canonical_json_bytes,
    canonical_json_string,
    tagged_sha256,
)


def decimal_context_signature() -> tuple[object, ...]:
    context = getcontext()
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        tuple(context.flags.items()),
        tuple(context.traps.items()),
    )


def test_public_exports_are_exact() -> None:
    assert audit.__all__ == [
        "canonical_datetime",
        "canonical_decimal",
        "canonical_json_bytes",
        "canonical_json_string",
        "tagged_sha256",
    ]


def test_canonical_decimal_collapses_zero_and_trailing_zero_equivalence() -> None:
    assert canonical_decimal(Decimal("0")) == "0"
    assert canonical_decimal(Decimal("-0.000")) == "0"
    assert canonical_decimal(Decimal("1.20")) == "12e-1"
    assert canonical_decimal(Decimal("1.2")) == "12e-1"


def test_canonical_decimal_isolated_from_hostile_ambient_context() -> None:
    ambient = getcontext().copy()
    hostile = Context(prec=1, rounding="ROUND_DOWN", Emin=-1, Emax=1)
    value = Decimal("123456789012345678901234567890.120000")

    try:
        setcontext(hostile)
        before = decimal_context_signature()
        assert canonical_decimal(value) == "12345678901234567890123456789012e-2"
        assert decimal_context_signature() == before
    finally:
        setcontext(ambient)


def test_canonical_datetime_uses_equivalent_utc_six_microsecond_form() -> None:
    utc_value = datetime(2026, 7, 29, 8, 0, 0, 123456, tzinfo=UTC)
    offset_value = datetime(
        2026,
        7,
        29,
        16,
        0,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert canonical_datetime(utc_value) == "2026-07-29T08:00:00.123456Z"
    assert canonical_datetime(offset_value) == canonical_datetime(utc_value)
    assert canonical_datetime(datetime(2026, 7, 29, 8, tzinfo=UTC)).endswith(".000000Z")


def test_fixed_array_compact_json_has_utf8_bytes_and_string_forms() -> None:
    fields = ("US", "股票/é", "12e-1", "2026-07-29T08:00:00.123456Z")
    expected = '["US","股票/é","12e-1","2026-07-29T08:00:00.123456Z"]'

    assert canonical_json_string(fields) == expected
    assert canonical_json_bytes(fields) == expected.encode("utf-8")


def test_tagged_sha256_has_tag_and_lowercase_digest() -> None:
    fields = ("US", "股票/é", "12e-1")
    encoded = '["US","股票/é","12e-1"]'.encode()
    expected = f"bar-sha256:{hashlib.sha256(encoded).hexdigest()}"

    result = tagged_sha256("bar", fields)

    assert result == expected
    assert result.removeprefix("bar-sha256:").isalnum()
    assert result == result.lower()
