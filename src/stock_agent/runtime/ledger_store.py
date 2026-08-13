from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from stock_agent.account.ledger import (
    BuyFilled,
    CashAdjusted,
    CashInitialized,
    EventReversed,
    LedgerEvent,
    OpenExecutionBatchBooked,
    PortfolioLedger,
    PortfolioMarked,
    PositionMarked,
    SellFilled,
)
from stock_agent.domain import Market
from stock_agent.runtime.store import RuntimeStore

_EVENT_TYPES = {
    "CashInitialized": CashInitialized,
    "BuyFilled": BuyFilled,
    "SellFilled": SellFilled,
    "CashAdjusted": CashAdjusted,
    "PositionMarked": PositionMarked,
    "OpenExecutionBatchBooked": OpenExecutionBatchBooked,
    "PortfolioMarked": PortfolioMarked,
    "EventReversed": EventReversed,
}


class LedgerStoreError(Exception):
    """Stable, secret-free failure of the durable ledger store."""


class LedgerStore:
    """Append-only durable ledger authority inside the runtime SQLite database."""

    def __init__(self, store: RuntimeStore) -> None:
        if type(store) is not RuntimeStore:
            raise TypeError("store must be exactly RuntimeStore")
        self._store = store

    def append_events(
        self, account_id: str, market: Market, events: tuple[LedgerEvent, ...]
    ) -> None:
        if type(account_id) is not str or not account_id:
            raise LedgerStoreError("account_id must be a nonblank string")
        if type(market) is not Market:
            raise LedgerStoreError("market must be exactly Market")
        if type(events) is not tuple or not events:
            raise LedgerStoreError("events must be a nonempty exact tuple")
        if any(type(event) not in _EVENT_TYPES.values() for event in events):
            raise LedgerStoreError("events must contain exact LedgerEvent values")
        if any(event.account_id != account_id or event.market is not market for event in events):
            raise LedgerStoreError("event account/market must match the target account")

        # Idempotency / conflict pre-check before any semantic replay.
        connection = self._store.connection
        new_events: list[tuple[LedgerEvent, str]] = []
        for event in events:
            payload = _encode(event)
            row = connection.execute(
                "SELECT payload FROM ledger_events WHERE event_id = ?", [event.event_id]
            ).fetchone()
            if row is None:
                new_events.append((event, payload))
            elif row[0] != payload:
                raise LedgerStoreError("ledger event identity conflict")
            # else: identical event already persisted -> idempotent skip.

        if not new_events:
            return

        # Semantic cross-validation by deterministic replay before any write.
        existing = self.load_events(account_id)
        candidate = (*existing, *(event for event, _ in new_events))
        try:
            PortfolioLedger.rebuild(account_id, market, candidate)
        except (TypeError, ValueError) as error:
            raise LedgerStoreError("ledger event batch is invalid") from error

        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT MAX(event_index) FROM ledger_events WHERE account_id = ?",
                [account_id],
            ).fetchone()
            next_index = 0 if row[0] is None else int(row[0]) + 1

            for offset, (event, payload) in enumerate(new_events):
                event_type = type(event).__name__
                event_index = next_index + offset
                connection.execute(
                    "INSERT INTO ledger_events "
                    "(account_id, event_index, event_id, event_type, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [account_id, event_index, event.event_id, event_type, payload],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def load_events(self, account_id: str) -> tuple[LedgerEvent, ...]:
        if type(account_id) is not str or not account_id:
            raise LedgerStoreError("account_id must be a nonblank string")
        try:
            rows = self._store.connection.execute(
                "SELECT event_type, payload FROM ledger_events "
                "WHERE account_id = ? ORDER BY event_index",
                [account_id],
            ).fetchall()
        except Exception:
            raise LedgerStoreError("ledger event read failed") from None
        return tuple(_decode(str(row[0]), str(row[1])) for row in rows)

    def rebuild_ledger(self, account_id: str, market: Market) -> PortfolioLedger:
        events = self.load_events(account_id)
        return PortfolioLedger.rebuild(account_id, market, events)


def _encode(event: LedgerEvent) -> str:
    return json.dumps(
        event.model_dump(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _json_default(value: object) -> object:
    if type(value) is Decimal:
        return {"$decimal": str(value)}
    if type(value) is datetime:
        return {"$datetime": value.isoformat()}
    if type(value) is date:
        return {"$date": value.isoformat()}
    raise TypeError(f"unserializable ledger value {type(value)!r}")


def _decode(event_type: str, payload: str) -> LedgerEvent:
    model = _EVENT_TYPES.get(event_type)
    if model is None:
        raise LedgerStoreError("persisted ledger event type is unknown")
    try:
        raw = json.loads(payload)
        data = _restore(raw)
        return model.model_validate(data)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LedgerStoreError("persisted ledger event payload is invalid") from error


def _restore(value: object) -> object:
    if isinstance(value, dict):
        if len(value) == 1 and "$decimal" in value:
            return Decimal(value["$decimal"])
        if len(value) == 1 and "$datetime" in value:
            return datetime.fromisoformat(value["$datetime"])
        if len(value) == 1 and "$date" in value:
            return date.fromisoformat(value["$date"])
        return {key: _restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore(item) for item in value]
    return value
