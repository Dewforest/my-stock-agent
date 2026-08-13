from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    MarketDataBatchOrchestrator,
    ProviderBudgetProfile,
    SymbolFetchStatus,
)
from stock_agent.runtime.store import RuntimeStore

CN_SYMBOLS = (
    "600519",
    "601318",
    "600036",
    "600276",
    "600900",
    "601088",
    "600030",
    "000333",
    "000858",
    "300750",
)
NOW = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)
CN_SESSION = date(2026, 8, 13)


def cn_budget() -> ProviderBudgetProfile:
    return ProviderBudgetProfile(
        provider_id="eastmoney-daily/v1",
        market=Market.CN,
        normal_limit=10,
        recovery_limit=10,
        reserved=0,
        timezone="Asia/Shanghai",
    )


def cn_schedule() -> BoundedSessionSchedule:
    zone = ZoneInfo("Asia/Shanghai")
    session = date(2026, 8, 13)
    return BoundedSessionSchedule(
        market=Market.CN,
        start=session,
        end=session,
        sessions=(
            SessionScheduleRow(
                session_date=session,
                open_at=datetime.combine(session, time(9, 30), zone),
                close_at=datetime.combine(session, time(15), zone),
                timezone="Asia/Shanghai",
                provenance="fixture-cn-schedule",
                generated_on=date(2026, 8, 12),
            ),
        ),
    )


def cn_bar(symbol: str) -> FetchedDailyBar:
    return FetchedDailyBar(
        market=Market.CN,
        symbol=symbol,
        session_date=CN_SESSION,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=Decimal("1000"),
        provider_id="eastmoney-daily/v1",
        provider_native_symbol=symbol,
        provider_record_id=f"rec-{symbol}",
    )


class ScriptedProvider:
    """Returns bars for successful symbols and a configured error otherwise."""

    provider_id = "eastmoney-daily/v1"

    def __init__(self, failures: dict[str, MarketDataError]) -> None:
        self._failures = failures
        self.calls: dict[str, int] = {}

    def fetch_daily_bars(self, request: object) -> tuple[FetchedDailyBar, ...]:
        symbol = request.symbol
        self.calls[symbol] = self.calls.get(symbol, 0) + 1
        if symbol in self._failures:
            raise self._failures[symbol]
        return (cn_bar(symbol),)


def build(tmp_path: Path, provider: object) -> tuple[MarketDataBatchOrchestrator, list[str]]:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    factory_calls: list[str] = []

    def factory(provider_id: str) -> object:
        factory_calls.append(provider_id)
        return provider

    orchestrator = MarketDataBatchOrchestrator(
        store=store,
        pit_store=PointInTimeStore(":memory:"),
        provider_factory=factory,  # type: ignore[arg-type]
        budget=cn_budget(),
    )
    return orchestrator, factory_calls


# ── 7. CN bounded serial with one retry maximum ─────────────────────────────


def test_cn_retries_once_and_serializes(tmp_path: Path) -> None:
    # One symbol always fails retryably: it must be attempted exactly twice
    # (one normal + one recovery), never a third time.
    provider = ScriptedProvider({"600519": MarketDataError(MarketDataErrorCode.READ_TIMEOUT)})
    orchestrator, factory_calls = build(tmp_path, provider)
    report = orchestrator.refresh(
        symbols=CN_SYMBOLS, schedule=cn_schedule(), session_date=CN_SESSION, now=NOW
    )
    assert len(factory_calls) == 11  # 10 normal + 1 recovery
    assert provider.calls["600519"] == 2
    failing = next(result for result in report.results if result.symbol == "600519")
    assert failing.status is SymbolFetchStatus.RETRYABLE_FAILURE
    successful = [result for result in report.results if result.symbol != "600519"]
    assert all(result.status is SymbolFetchStatus.SUCCESS for result in successful)


# ── 8. one symbol failure preserves other successful ingestions ─────────────


def test_one_failure_preserves_other_successes(tmp_path: Path) -> None:
    provider = ScriptedProvider({"600519": MarketDataError(MarketDataErrorCode.AUTH)})
    orchestrator, _ = build(tmp_path, provider)
    report = orchestrator.refresh(
        symbols=CN_SYMBOLS, schedule=cn_schedule(), session_date=CN_SESSION, now=NOW
    )
    statuses = {result.symbol: result.status for result in report.results}
    assert statuses["600519"] is SymbolFetchStatus.PERMANENT_FAILURE
    assert all(
        status is SymbolFetchStatus.SUCCESS
        for symbol, status in statuses.items()
        if symbol != "600519"
    )


# ── 9. each symbol ingestion is all-or-nothing ──────────────────────────────


def test_each_symbol_ingestion_is_independent_and_complete(tmp_path: Path) -> None:
    provider = ScriptedProvider({"000333": MarketDataError(MarketDataErrorCode.HTTP_5XX)})
    orchestrator, _ = build(tmp_path, provider)
    report = orchestrator.refresh(
        symbols=CN_SYMBOLS, schedule=cn_schedule(), session_date=CN_SESSION, now=NOW
    )
    # The failing symbol got its single recovery retry and remained failed;
    # its failure neither rolled back nor corrupted the other nine.
    assert any(
        result.symbol == "000333" and result.status is SymbolFetchStatus.RETRYABLE_FAILURE
        for result in report.results
    )
    successful = [
        result.symbol for result in report.results if result.status is SymbolFetchStatus.SUCCESS
    ]
    assert len(successful) == 9


# ── 10. safe error codes only ───────────────────────────────────────────────


def test_errors_report_only_safe_codes(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        {
            "600519": MarketDataError(
                MarketDataErrorCode.AUTH,
                metadata={"service": "com.dewforest.secret-service", "account": "amezf"},
            )
        }
    )
    orchestrator, _ = build(tmp_path, provider)
    report = orchestrator.refresh(
        symbols=CN_SYMBOLS, schedule=cn_schedule(), session_date=CN_SESSION, now=NOW
    )
    failing = next(result for result in report.results if result.symbol == "600519")
    assert failing.error_code == "auth"
    # The secret-shaped metadata must never surface in the report.
    for result in report.results:
        assert "secret-service" not in str(result.error_code)
        assert "com.dewforest" not in str(result.error_code)
