from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import StringConstraints

from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    BoundedSessionSchedule,
    DailyBarRequest,
    IncrementalBarIngestor,
    MarketDataError,
    MarketDataErrorCode,
)
from stock_agent.data.providers.protocol import HistoricalDailyBarProvider
from stock_agent.domain import Market
from stock_agent.runtime.models import RuntimeModel
from stock_agent.runtime.store import (
    BudgetClaimOutcome,
    BudgetKind,
    RuntimeStore,
    StoreError,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SymbolFetchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    THROTTLED = "THROTTLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"


class ProviderBudgetProfile(RuntimeModel):
    provider_id: NonEmptyStr
    market: Market
    normal_limit: int
    recovery_limit: int
    reserved: int
    timezone: NonEmptyStr

    def validate_profile(self) -> ProviderBudgetProfile:
        if self.normal_limit <= 0:
            raise ValueError("normal_limit must be positive")
        if self.recovery_limit < 0 or self.reserved < 0:
            raise ValueError("recovery_limit and reserved must be nonnegative")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA zone name") from error
        return self


class SymbolFetchResult(RuntimeModel):
    symbol: NonEmptyStr
    status: SymbolFetchStatus
    error_code: str | None = None
    attempt_number: int = 0


class BatchReport(RuntimeModel):
    results: tuple[SymbolFetchResult, ...]
    provider_calls: int


class MarketDataBatchOrchestrator:
    """Refreshes a bounded symbol batch under a durable provider budget.

    The provider factory is invoked only after a budget claim succeeds, and
    ``IncrementalBarIngestor`` remains the single-symbol all-or-nothing
    transaction boundary.
    """

    def __init__(
        self,
        *,
        store: RuntimeStore,
        pit_store: PointInTimeStore,
        provider_factory: Callable[[str], HistoricalDailyBarProvider],
        budget: ProviderBudgetProfile,
    ) -> None:
        if type(store) is not RuntimeStore:
            raise TypeError("store must be exactly RuntimeStore")
        if type(pit_store) is not PointInTimeStore:
            raise TypeError("pit_store must be exactly PointInTimeStore")
        if not callable(provider_factory):
            raise TypeError("provider_factory must be callable")
        if type(budget) is not ProviderBudgetProfile:
            raise TypeError("budget must be exactly ProviderBudgetProfile")
        budget.validate_profile()
        self._store = store
        self._pit_store = pit_store
        self._provider_factory = provider_factory
        self._budget = budget

    def refresh(
        self,
        *,
        symbols: tuple[str, ...],
        schedule: BoundedSessionSchedule,
        session_date: date,
        now: datetime,
    ) -> BatchReport:
        self._validate_refresh(symbols, schedule, session_date, now)
        provider_id = self._budget.provider_id
        market = self._budget.market
        day = now.astimezone(ZoneInfo(self._budget.timezone)).date()

        self._store.ensure_provider_budget(
            provider_id,
            day,
            normal_limit=self._budget.normal_limit,
            recovery_limit=self._budget.recovery_limit,
            reserved=self._budget.reserved,
        )

        results: dict[str, SymbolFetchResult] = {}
        retryable: list[str] = []
        provider_calls = 0

        # Phase 1: normal attempts, serialized and symbol-sorted.
        for symbol in sorted(symbols):
            if self._store.is_circuit_open(provider_id, day):
                results[symbol] = self._skipped(symbol, SymbolFetchStatus.CIRCUIT_OPEN)
                continue
            if self._store.has_completed_symbol(provider_id, market, symbol, session_date):
                results[symbol] = self._skipped(symbol, SymbolFetchStatus.ALREADY_COMPLETED)
                continue
            result, calls = self._attempt(
                symbol, schedule, session_date, now, day, BudgetKind.NORMAL
            )
            provider_calls += calls
            results[symbol] = result
            if result.status is SymbolFetchStatus.RETRYABLE_FAILURE:
                retryable.append(symbol)
            # THROTTLED opened the circuit inside _attempt; remaining symbols
            # observe it on their next iteration.

        # Phase 2: bounded recovery retries for retryable failures only.
        for symbol in retryable:
            if self._store.is_circuit_open(provider_id, day):
                results[symbol] = self._skipped(symbol, SymbolFetchStatus.CIRCUIT_OPEN)
                continue
            result, calls = self._attempt(
                symbol, schedule, session_date, now, day, BudgetKind.RECOVERY
            )
            provider_calls += calls
            results[symbol] = result

        ordered = tuple(results[symbol] for symbol in sorted(symbols))
        return BatchReport(results=ordered, provider_calls=provider_calls)

    def _attempt(
        self,
        symbol: str,
        schedule: BoundedSessionSchedule,
        session_date: date,
        now: datetime,
        day: date,
        kind: BudgetKind,
    ) -> tuple[SymbolFetchResult, int]:
        provider_id = self._budget.provider_id
        attempt_number = 1 if kind is BudgetKind.NORMAL else 2

        claim = self._store.claim_provider_budget(provider_id, day, kind)
        if claim is BudgetClaimOutcome.EXHAUSTED:
            return (
                SymbolFetchResult(
                    symbol=symbol,
                    status=SymbolFetchStatus.BUDGET_EXHAUSTED,
                    attempt_number=attempt_number,
                ),
                0,
            )
        if claim is BudgetClaimOutcome.CIRCUIT_OPEN:
            return self._skipped(symbol, SymbolFetchStatus.CIRCUIT_OPEN), 0

        # Budget claimed: only now may the provider be constructed.
        provider = self._provider_factory(provider_id)
        ingestor = IncrementalBarIngestor(provider=provider, store=self._pit_store)
        request = DailyBarRequest(
            market=self._budget.market,
            symbol=symbol,
            start=schedule.start,
            end=schedule.end,
        )

        status: SymbolFetchStatus
        error_code: str | None = None
        try:
            ingestor.ingest(request=request, schedule=schedule, ingested_at=now)
            status = SymbolFetchStatus.SUCCESS
        except MarketDataError as error:
            if error.code is MarketDataErrorCode.THROTTLED:
                self._store.open_provider_circuit(provider_id, day)
                status = SymbolFetchStatus.THROTTLED
            elif error.retryable:
                status = SymbolFetchStatus.RETRYABLE_FAILURE
            else:
                status = SymbolFetchStatus.PERMANENT_FAILURE
            error_code = error.code.value

        self._store.record_symbol_attempt(
            provider_id=provider_id,
            market=self._budget.market,
            symbol=symbol,
            session_date=session_date,
            attempt_number=attempt_number,
            status=status.value,
            error_code=error_code,
            started_at=now,
            ended_at=now,
        )
        return (
            SymbolFetchResult(
                symbol=symbol,
                status=status,
                error_code=error_code,
                attempt_number=attempt_number,
            ),
            1,
        )

    @staticmethod
    def _skipped(symbol: str, status: SymbolFetchStatus) -> SymbolFetchResult:
        return SymbolFetchResult(symbol=symbol, status=status, attempt_number=0)

    def _validate_refresh(
        self,
        symbols: tuple[str, ...],
        schedule: BoundedSessionSchedule,
        session_date: date,
        now: datetime,
    ) -> None:
        if type(symbols) is not tuple or not symbols:
            raise StoreError("refresh requires a nonempty exact tuple of symbols")
        if any(type(symbol) is not str or not symbol for symbol in symbols):
            raise StoreError("refresh symbols must be nonblank strings")
        if type(schedule) is not BoundedSessionSchedule:
            raise StoreError("refresh requires an exact BoundedSessionSchedule")
        if schedule.market is not self._budget.market:
            raise StoreError("schedule market must match the budget market")
        if type(session_date) is not date:
            raise StoreError("refresh requires a plain session date")
        if type(now) is not datetime or now.tzinfo is None:
            raise StoreError("refresh requires an aware UTC datetime")
