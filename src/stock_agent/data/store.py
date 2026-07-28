from datetime import UTC, date, datetime

import duckdb

from stock_agent.domain import Bar, Market


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


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
                source_record_id VARCHAR NOT NULL
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
        self._connection.execute(
            """
            INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(bar.market),
                bar.symbol.strip().upper(),
                bar.session_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.available_at.astimezone(UTC).replace(tzinfo=None),
                ingested_at.astimezone(UTC).replace(tzinfo=None),
                source,
                source_record_id,
            ],
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
            ORDER BY available_at DESC, ingested_at DESC
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
