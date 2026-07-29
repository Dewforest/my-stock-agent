from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from stock_agent.backtest import BacktestSession, BacktestSpec, ChronologicalBacktestRunner
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Currency, Instrument, Market, StrategyIntent
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext

DATES = tuple(date(2026, 7, day) for day in (20, 21, 22, 23, 24))


@dataclass(frozen=True)
class EmptyStrategy:
    strategy_id: str = "empty-pit"
    config_version: str = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        return ()


def _instant(session_date: date, hour: int) -> datetime:
    return datetime(session_date.year, session_date.month, session_date.day, hour, tzinfo=UTC)


def _bar(session_date: date, close: Decimal, available_at: datetime) -> Bar:
    return Bar(
        symbol="AAPL",
        market=Market.US,
        session_date=session_date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1000"),
        available_at=available_at,
    )


def _spec(run_id: str) -> BacktestSpec:
    return BacktestSpec(
        run_id=run_id,
        account_id="pit-account",
        market=Market.US,
        initial_cash=Decimal("1000"),
        instruments=(
            Instrument(
                symbol="AAPL",
                market=Market.US,
                currency=Currency.USD,
                sector="Technology",
            ),
        ),
        sessions=tuple(
            BacktestSession(
                session_date=session_date,
                open_at=_instant(session_date, 14),
                close_at=_instant(session_date, 21),
                open_bars=(
                    Bar(
                        symbol="AAPL",
                        market=Market.US,
                        session_date=session_date,
                        open=Decimal("100"),
                        high=Decimal("100"),
                        low=Decimal("100"),
                        close=Decimal("100"),
                        volume=Decimal(0),
                        available_at=_instant(session_date, 14),
                    ),
                ),
            )
            for session_date in DATES
        ),
        strategy_config_version="v1",
    )


def _store(*, with_correction: bool) -> PointInTimeStore:
    store = PointInTimeStore()
    for session_date in DATES:
        close_at = _instant(session_date, 21)
        store.append_bar(
            _bar(session_date, Decimal("100"), close_at),
            ingested_at=close_at + timedelta(seconds=1),
            source="official-original",
            source_record_id=f"original-{session_date.isoformat()}",
        )
    if with_correction:
        correction_available = _instant(DATES[3], 20)
        store.append_bar(
            _bar(DATES[0], Decimal("111"), correction_available),
            ingested_at=correction_available + timedelta(seconds=7),
            source="official-correction",
            source_record_id="correction-d1-v2",
        )
    return store


def test_late_pit_correction_changes_only_first_lawful_cumulative_snapshot_and_audit() -> None:
    corrected_store = _store(with_correction=True)
    baseline_store = _store(with_correction=False)
    calendar = TradingCalendar(Market.US, DATES)

    corrected = ChronologicalBacktestRunner(
        store=corrected_store, calendar=calendar, strategy=EmptyStrategy()
    ).run(_spec("corrected-run"))
    baseline = ChronologicalBacktestRunner(
        store=baseline_store, calendar=calendar, strategy=EmptyStrategy()
    ).run(_spec("baseline-run"))

    d1_selected = tuple(
        session.selected_revisions[0] for session in corrected.sessions
    )
    assert tuple(item.bar.close for item in d1_selected) == (
        Decimal("100.000000000000"),
        Decimal("100.000000000000"),
        Decimal("100.000000000000"),
        Decimal("111.000000000000"),
        Decimal("111.000000000000"),
    )
    assert tuple(item.source for item in d1_selected) == (
        "official-original",
        "official-original",
        "official-original",
        "official-correction",
        "official-correction",
    )
    assert tuple(item.source_record_id for item in d1_selected) == (
        "original-2026-07-20",
        "original-2026-07-20",
        "original-2026-07-20",
        "correction-d1-v2",
        "correction-d1-v2",
    )
    assert d1_selected[0].bar.available_at == _instant(DATES[0], 21)
    assert d1_selected[0].ingested_at == _instant(DATES[0], 21) + timedelta(seconds=1)
    assert d1_selected[3].bar.available_at == _instant(DATES[3], 20)
    assert d1_selected[3].ingested_at == _instant(DATES[3], 20) + timedelta(seconds=7)
    assert corrected.sessions[2].selected_revisions[0] == d1_selected[0]
    assert corrected.resolved_data_fingerprint != baseline.resolved_data_fingerprint

    corrected_store.close()
    baseline_store.close()
