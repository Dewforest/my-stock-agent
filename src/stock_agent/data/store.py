from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException, localcontext

import duckdb

from stock_agent.domain import Bar, Market

_DECIMAL_SCALE = Decimal("1E-12")
_DECIMAL_INTEGER_LIMIT = Decimal("1E+26")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_decimal_38_12(value: Decimal, name: str) -> Decimal:
    message = f"{name} must be exactly representable as DECIMAL(38, 12)"
    try:
        with localcontext() as context:
            context.prec = max(50, len(value.as_tuple().digits) + 12)
            if abs(value) >= _DECIMAL_INTEGER_LIMIT:
                raise ValueError(message)
            quantized = value.quantize(_DECIMAL_SCALE)
            if quantized != value:
                raise ValueError(message)
            return quantized
    except DecimalException as error:
        raise ValueError(message) from error


class PointInTimeStore:
    def __init__(self, database: str = ":memory:") -> None:
        self._connection = duckdb.connect(database)
        self._closed = False
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bars (
                market VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                session_date DATE NOT NULL,
                open DECIMAL(38, 12) NOT NULL,
                high DECIMAL(38, 12) NOT NULL,
                low DECIMAL(38, 12) NOT NULL,
                close DECIMAL(38, 12) NOT NULL,
                volume DECIMAL(38, 12) NOT NULL,
                available_at TIMESTAMP NOT NULL,
                ingested_at TIMESTAMP NOT NULL,
                source VARCHAR NOT NULL,
                source_record_id VARCHAR NOT NULL,
                UNIQUE(source, source_record_id)
            )
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("PointInTimeStore is closed")

    def __enter__(self) -> "PointInTimeStore":
        self._ensure_open()
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def append_bar(
        self,
        bar: Bar,
        *,
        ingested_at: datetime,
        source: str,
        source_record_id: str,
    ) -> None:
        self._ensure_open()
        _require_aware(ingested_at, "ingested_at")
        if ingested_at < bar.available_at:
            raise ValueError("ingested_at cannot be before available_at")
        source = source.strip()
        source_record_id = source_record_id.strip()
        if not source:
            raise ValueError("source must not be blank")
        if not source_record_id:
            raise ValueError("source_record_id must not be blank")
        decimals = [
            _require_decimal_38_12(value, name)
            for name, value in (
                ("open", bar.open),
                ("high", bar.high),
                ("low", bar.low),
                ("close", bar.close),
                ("volume", bar.volume),
            )
        ]
        payload = (
            str(bar.market),
            bar.symbol.strip().upper(),
            bar.session_date,
            *decimals,
            bar.available_at.astimezone(UTC).replace(tzinfo=None),
            ingested_at.astimezone(UTC).replace(tzinfo=None),
            source,
            source_record_id,
        )
        existing = self._connection.execute(
            """
            SELECT market, symbol, session_date, open, high, low, close, volume,
                   available_at, ingested_at, source, source_record_id
            FROM bars
            WHERE source = ? AND source_record_id = ?
            """,
            [source, source_record_id],
        ).fetchone()
        if existing is not None:
            if existing == payload:
                return
            raise ValueError(
                "conflicting payload for revision identity "
                f"({source!r}, {source_record_id!r})"
            )
        self._connection.execute(
            """
            INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )

    def latest_bar_as_of(
        self,
        *,
        market: Market,
        symbol: str,
        session_date: date,
        as_of: datetime,
    ) -> Bar | None:
        self._ensure_open()
        _require_aware(as_of, "as_of")
        row = self._connection.execute(
            """
            SELECT symbol, market, session_date, open, high, low, close, volume, available_at
            FROM bars
            WHERE market = ? AND symbol = ? AND session_date = ?
                AND available_at <= ?
            ORDER BY available_at DESC, ingested_at DESC, source DESC, source_record_id DESC
            LIMIT 1
            """,
            [
                str(market),
                symbol.strip().upper(),
                session_date,
                as_of.astimezone(UTC).replace(tzinfo=None),
            ],
        ).fetchone()
        if row is None:
            return None
        return Bar(
            symbol=row[0],
            market=Market(row[1]),
            session_date=row[2],
            open=row[3],
            high=row[4],
            low=row[5],
            close=row[6],
            volume=row[7],
            available_at=row[8].replace(tzinfo=UTC),
        )
