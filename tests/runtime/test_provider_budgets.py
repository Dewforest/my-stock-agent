from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import (
    BoundedSessionSchedule,
    FetchedDailyBar,
    MarketDataError,
    MarketDataErrorCode,
    SessionScheduleRow,
)
from stock_agent.domain import Market
from stock_agent.runtime.market_data import (
    BudgetClaimOutcome,
    BudgetKind,
    MarketDataBatchOrchestrator,
    ProviderBudgetProfile,
    SymbolFetchStatus,
)
from stock_agent.runtime.store import RuntimeStore, StoreError

US_SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "XOM", "JNJ", "PG")
NOW = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)
SESSION_DATE = date(2026, 8, 13)


def us_budget(**overrides: object) -> ProviderBudgetProfile:
    values: dict[str, object] = {
        "provider_id": "alpha-vantage-daily/v1",
        "market": Market.US,
        "normal_limit": 10,
        "recovery_limit": 10,
        "reserved": 5,
        "timezone": "America/New_York",
    }
    values.update(overrides)
    return ProviderBudgetProfile(**values)  # type: ignore[arg-type]


def us_schedule() -> BoundedSessionSchedule:
    zone = ZoneInfo("America/New_York")
    session = date(2026, 8, 13)
    return BoundedSessionSchedule(
        market=Market.US,
        start=session,
        end=session,
        sessions=(
            SessionScheduleRow(
                session_date=session,
                open_at=datetime.combine(session, time(9, 30), zone),
                close_at=datetime.combine(session, time(16), zone),
                timezone="America/New_York",
                provenance="fixture-us-schedule",
                generated_on=date(2026, 8, 12),
            ),
        ),
    )


def us_bar(symbol: str) -> FetchedDailyBar:
    return FetchedDailyBar(
        market=Market.US,
        symbol=symbol,
        session_date=SESSION_DATE,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("100"),
        provider_id="alpha-vantage-daily/v1",
        provider_native_symbol=symbol,
        provider_record_id=f"rec-{symbol}",
    )


class FakeProvider:
    def __init__(
        self,
        provider_id: str = "alpha-vantage-daily/v1",
        *,
        error: MarketDataError | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._error = error
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def fetch_daily_bars(self, request: object) -> tuple[FetchedDailyBar, ...]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return (us_bar(request.symbol),)


def build_orchestrator(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    budget: ProviderBudgetProfile,
) -> tuple[MarketDataBatchOrchestrator, list[str]]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    pit_store = PointInTimeStore(":memory:")
    factory_calls: list[str] = []

    def factory(provider_id: str) -> FakeProvider:
        factory_calls.append(provider_id)
        return provider

    orchestrator = MarketDataBatchOrchestrator(
        store=store,
        pit_store=pit_store,
        provider_factory=factory,
        budget=budget,
    )
    return orchestrator, factory_calls


# ── budget primitives ───────────────────────────────────────────────────────


def test_normal_budget_caps_at_limit(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.ensure_provider_budget(
        "alpha-vantage-daily/v1",
        SESSION_DATE,
        normal_limit=10,
        recovery_limit=10,
        reserved=5,
    )
    for _ in range(10):
        assert (
            store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)
            is BudgetClaimOutcome.CLAIMED
        )
    assert (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)
        is BudgetClaimOutcome.EXHAUSTED
    )


def test_recovery_budget_is_durable_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.ensure_provider_budget(
        "alpha-vantage-daily/v1", SESSION_DATE, normal_limit=10, recovery_limit=10, reserved=5
    )
    assert (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.RECOVERY)
        is BudgetClaimOutcome.CLAIMED
    )
    store.close()

    reopened = RuntimeStore(path)
    # The recovery claim already spent must persist; only 9 remain.
    remaining = 0
    while (
        reopened.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.RECOVERY)
        is BudgetClaimOutcome.CLAIMED
    ):
        remaining += 1
    assert remaining == 9


def test_circuit_open_blocks_claims(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.ensure_provider_budget(
        "alpha-vantage-daily/v1", SESSION_DATE, normal_limit=10, recovery_limit=10, reserved=5
    )
    store.open_provider_circuit("alpha-vantage-daily/v1", SESSION_DATE)
    assert (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)
        is BudgetClaimOutcome.CIRCUIT_OPEN
    )


def test_claim_requires_prior_budget_row(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    with pytest.raises(StoreError):
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)


# ── orchestration: budgets, circuit, and retry policy ───────────────────────


def test_us_session_consumes_at_most_ten_normal_requests(tmp_path: Path) -> None:
    provider = FakeProvider()
    orchestrator, factory_calls = build_orchestrator(
        tmp_path, provider=provider, budget=us_budget()
    )
    report = orchestrator.refresh(
        symbols=US_SYMBOLS, schedule=us_schedule(), session_date=SESSION_DATE, now=NOW
    )
    assert len(factory_calls) == 10
    assert all(result.status is SymbolFetchStatus.SUCCESS for result in report.results)
    assert report.provider_calls == 10


def test_completed_symbols_are_not_refetched_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    budget = us_budget()

    provider = FakeProvider()
    factory_calls: list[str] = []

    def factory(provider_id: str) -> FakeProvider:
        factory_calls.append(provider_id)
        return provider

    store = RuntimeStore(path)
    orchestrator = MarketDataBatchOrchestrator(
        store=store,
        pit_store=PointInTimeStore(":memory:"),
        provider_factory=factory,
        budget=budget,
    )
    orchestrator.refresh(
        symbols=US_SYMBOLS, schedule=us_schedule(), session_date=SESSION_DATE, now=NOW
    )
    assert len(factory_calls) == 10
    store.close()

    # Restart with fresh connections; a second refresh must not refetch completed symbols.
    factory_calls.clear()
    reopened_store = RuntimeStore(path)
    reopened = MarketDataBatchOrchestrator(
        store=reopened_store,
        pit_store=PointInTimeStore(":memory:"),
        provider_factory=factory,
        budget=budget,
    )
    report = reopened.refresh(
        symbols=US_SYMBOLS,
        schedule=us_schedule(),
        session_date=SESSION_DATE,
        now=NOW + timedelta(minutes=1),
    )
    assert len(factory_calls) == 0
    assert all(result.status is SymbolFetchStatus.ALREADY_COMPLETED for result in report.results)


def test_one_allowed_retry_consumes_durable_recovery_budget(tmp_path: Path) -> None:
    # First call retryable, retry succeeds. The recovery spend is durable.
    provider = FakeProvider()
    calls: list[int] = []

    class RetryThenSuccess:
        provider_id = "alpha-vantage-daily/v1"

        def __init__(self, inner: FakeProvider) -> None:
            self._inner = inner

        def fetch_daily_bars(self, request: object) -> tuple[FetchedDailyBar, ...]:
            calls.append(1)
            if len(calls) == 1:
                raise MarketDataError(MarketDataErrorCode.READ_TIMEOUT)
            return self._inner.fetch_daily_bars(request)

    wrapped = RetryThenSuccess(provider)
    orchestrator, _ = build_orchestrator(
        tmp_path,
        provider=wrapped,
        budget=us_budget(),  # type: ignore[arg-type]
    )
    report = orchestrator.refresh(
        symbols=("AAPL",), schedule=us_schedule(), session_date=SESSION_DATE, now=NOW
    )
    assert len(calls) == 2  # one normal + one recovery
    assert report.results[0].status is SymbolFetchStatus.SUCCESS

    # The recovery budget has one durable spend.
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    remaining = 0
    while (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.RECOVERY)
        is BudgetClaimOutcome.CLAIMED
    ):
        remaining += 1
    assert remaining == 9


def test_five_reserved_requests_remain_unavailable(tmp_path: Path) -> None:
    # After 10 normal + 10 recovery spends, a third batch cannot claim anything.
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.ensure_provider_budget(
        "alpha-vantage-daily/v1", SESSION_DATE, normal_limit=10, recovery_limit=10, reserved=5
    )
    for _ in range(10):
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)
    for _ in range(10):
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.RECOVERY)
    assert (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.NORMAL)
        is BudgetClaimOutcome.EXHAUSTED
    )
    assert (
        store.claim_provider_budget("alpha-vantage-daily/v1", SESSION_DATE, BudgetKind.RECOVERY)
        is BudgetClaimOutcome.EXHAUSTED
    )


def test_throttled_opens_circuit_and_stops_remaining(tmp_path: Path) -> None:
    provider = FakeProvider(error=MarketDataError(MarketDataErrorCode.THROTTLED))
    orchestrator, factory_calls = build_orchestrator(
        tmp_path, provider=provider, budget=us_budget()
    )
    report = orchestrator.refresh(
        symbols=US_SYMBOLS, schedule=us_schedule(), session_date=SESSION_DATE, now=NOW
    )
    # Only the first symbol is attempted; the rest see the open circuit.
    assert len(factory_calls) == 1
    assert report.results[0].status is SymbolFetchStatus.THROTTLED
    assert all(result.status is SymbolFetchStatus.CIRCUIT_OPEN for result in report.results[1:])


def test_permanent_errors_never_retry(tmp_path: Path) -> None:
    provider = FakeProvider(error=MarketDataError(MarketDataErrorCode.AUTH))
    orchestrator, factory_calls = build_orchestrator(
        tmp_path, provider=provider, budget=us_budget()
    )
    report = orchestrator.refresh(
        symbols=("AAPL",), schedule=us_schedule(), session_date=SESSION_DATE, now=NOW
    )
    assert len(factory_calls) == 1  # no recovery retry
    assert report.results[0].status is SymbolFetchStatus.PERMANENT_FAILURE
    assert report.results[0].error_code == "auth"
