from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_agent.backtest import (
    BacktestSession,
    BacktestSpec,
    ChronologicalBacktestRunner,
    OpenFrameSource,
    build_real_data_backtest_spec,
)
from stock_agent.data import PointInTimeStore
from stock_agent.data.providers import BoundedSessionSchedule, SessionScheduleRow
from stock_agent.domain import Bar, Currency, Instrument, Market, StrategyIntent
from stock_agent.market import TradingCalendar
from stock_agent.strategies import StrategyContext

DATES = (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
SYMBOLS = ("AAPL", "MSFT")


def _instant(session_date: date, hour: int) -> datetime:
    return datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        hour,
        tzinfo=UTC,
    )


def _schedule() -> BoundedSessionSchedule:
    return BoundedSessionSchedule(
        market=Market.US,
        start=DATES[0],
        end=DATES[-1],
        sessions=tuple(
            SessionScheduleRow(
                session_date=session_date,
                open_at=_instant(session_date, 14),
                close_at=_instant(session_date, 21),
                timezone="America/New_York",
                provenance="audited-test-calendar",
                generated_on=date(2026, 7, 29),
            )
            for session_date in DATES
        ),
    )


def _instruments() -> tuple[Instrument, ...]:
    return tuple(
        Instrument(
            symbol=symbol,
            market=Market.US,
            currency=Currency.USD,
            sector="Technology",
        )
        for symbol in SYMBOLS
    )


def _populate(
    store: PointInTimeStore,
    *,
    missing: tuple[tuple[str, date], ...] = (),
) -> None:
    for symbol_index, symbol in enumerate(SYMBOLS):
        for date_index, session_date in enumerate(DATES):
            if (symbol, session_date) in missing:
                continue
            open_price = Decimal(100 + symbol_index * 10 + date_index)
            store.append_bar(
                Bar(
                    symbol=symbol,
                    market=Market.US,
                    session_date=session_date,
                    open=open_price,
                    high=open_price + Decimal("2"),
                    low=open_price - Decimal("1"),
                    close=open_price + Decimal("1"),
                    volume=Decimal("1000"),
                    available_at=_instant(session_date, 21),
                ),
                ingested_at=_instant(session_date, 21) + timedelta(seconds=1),
                source="alpha-vantage",
                source_record_id=f"{symbol}-{session_date.isoformat()}-baseline",
            )


def _build(store: PointInTimeStore, *, run_id: str = "real-data-run"):
    return build_real_data_backtest_spec(
        store=store,
        schedule=_schedule(),
        run_id=run_id,
        account_id="account-1",
        instruments=_instruments(),
        initial_cash=Decimal("10000"),
        strategy_config_version="v1",
    )


def test_dense_builder_creates_open_frames_with_exact_persisted_provenance() -> None:
    store = PointInTimeStore()
    _populate(store)

    spec = _build(store)

    assert spec.pit_knowledge_policy == "current-view-baseline/v1"
    assert (
        spec.market_data_price_policy
        == "raw-unadjusted/no-corporate-actions/v1"
    )
    assert tuple(session.session_date for session in spec.sessions) == DATES
    for session in spec.sessions:
        assert tuple(bar.symbol for bar in session.open_bars) == SYMBOLS
        assert tuple(item.symbol for item in session.open_frame_sources) == SYMBOLS
        for bar, source in zip(
            session.open_bars, session.open_frame_sources, strict=True
        ):
            selected = store.latest_bar_revision_as_of(
                market=Market.US,
                symbol=bar.symbol,
                session_date=session.session_date,
                as_of=session.close_at,
            )
            assert selected is not None
            assert bar.open == bar.high == bar.low == bar.close == selected.bar.open
            assert bar.volume == 0
            assert bar.available_at == session.open_at
            assert source.market is Market.US
            assert source.symbol == bar.symbol
            assert source.session_date == session.session_date
            assert source.source == selected.source
            assert source.source_record_id == selected.source_record_id
            assert source.available_at == selected.bar.available_at
            assert source.ingested_at == selected.ingested_at


@pytest.mark.parametrize(
    ("symbol", "session_date"),
    [(symbol, session_date) for symbol in SYMBOLS for session_date in DATES],
)
def test_dense_builder_rejects_every_missing_grid_cell(
    symbol: str, session_date: date
) -> None:
    store = PointInTimeStore()
    _populate(store, missing=((symbol, session_date),))
    with pytest.raises(ValueError, match="missing session data"):
        _build(store)


def test_builder_excludes_correction_available_after_own_close() -> None:
    store = PointInTimeStore()
    _populate(store)
    correction_available = _instant(DATES[1], 22)
    store.append_bar(
        Bar(
            symbol="AAPL",
            market=Market.US,
            session_date=DATES[1],
            open=Decimal("999"),
            high=Decimal("1000"),
            low=Decimal("998"),
            close=Decimal("999"),
            volume=Decimal("1000"),
            available_at=correction_available,
        ),
        ingested_at=correction_available + timedelta(seconds=1),
        source="alpha-vantage",
        source_record_id="AAPL-late-correction",
    )

    spec = _build(store)
    own_session = spec.sessions[1]

    assert own_session.open_bars[0].open == Decimal("101")
    assert own_session.open_frame_sources[0].source_record_id == (
        f"AAPL-{DATES[1].isoformat()}-baseline"
    )


def test_schedule_is_exact_bounded_immutable_and_chronological() -> None:
    schedule = _schedule()
    with pytest.raises(ValidationError):
        schedule.sessions = ()
    for values in (
        {"market": "US"},
        {"start": DATES[1]},
        {"end": DATES[1]},
        {"sessions": tuple(reversed(schedule.sessions))},
        {
            "sessions": (
                schedule.sessions[0],
                schedule.sessions[0],
                schedule.sessions[2],
            )
        },
    ):
        with pytest.raises(ValidationError):
            BoundedSessionSchedule(**(schedule.model_dump() | values))


class EmptyStrategy:
    strategy_id = "empty-real-data"
    config_version = "v1"

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        return ()


class CorrectingStrategy(EmptyStrategy):
    def __init__(self, store: PointInTimeStore) -> None:
        self._store = store
        self._inserted = False

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        if not self._inserted:
            self._inserted = True
            available_at = _instant(DATES[0], 22)
            self._store.append_bar(
                Bar(
                    symbol="AAPL",
                    market=Market.US,
                    session_date=DATES[0],
                    open=Decimal("999"),
                    high=Decimal("999"),
                    low=Decimal("999"),
                    close=Decimal("999"),
                    volume=Decimal("1000"),
                    available_at=available_at,
                ),
                ingested_at=available_at + timedelta(seconds=1),
                source="alpha-vantage",
                source_record_id="mid-run-correction",
            )
        return ()


def test_runner_freezes_complete_matrix_before_mutable_execution() -> None:
    store = PointInTimeStore()
    _populate(store)
    spec = _build(store, run_id="freeze-run")

    result = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.US, DATES),
        strategy=CorrectingStrategy(store),
    ).run(spec)

    first_symbol_history = tuple(
        next(
            revision
            for revision in session.selected_revisions
            if revision.bar.symbol == "AAPL"
            and revision.bar.session_date == DATES[0]
        )
        for session in result.sessions
    )
    assert {item.source_record_id for item in first_symbol_history} == {
        f"AAPL-{DATES[0].isoformat()}-baseline"
    }


def test_cache_conflicts_when_same_run_and_spec_resolve_different_data() -> None:
    store = PointInTimeStore()
    _populate(store)
    spec = _build(store, run_id="cache-data-conflict")
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.US, DATES),
        strategy=EmptyStrategy(),
    )
    runner.run(spec)
    correction_at = _instant(DATES[1], 20)
    store.append_bar(
        Bar(
            symbol="AAPL",
            market=Market.US,
            session_date=DATES[0],
            open=Decimal("150"),
            high=Decimal("151"),
            low=Decimal("149"),
            close=Decimal("150"),
            volume=Decimal("1000"),
            available_at=correction_at,
        ),
        ingested_at=correction_at + timedelta(seconds=1),
        source="alpha-vantage",
        source_record_id="changed-resolution",
    )

    with pytest.raises(ValueError, match="resolved data"):
        runner.run(spec)


def test_both_manifest_policies_enter_spec_fingerprint() -> None:
    store = PointInTimeStore()
    _populate(store)
    real_spec = _build(store, run_id="real-policy")
    fixture_values = {
        name: getattr(real_spec, name) for name in BacktestSpec.model_fields
    }
    fixture_values.update(
        run_id="fixture-policy",
        pit_knowledge_policy="business-available-at/v1",
        market_data_price_policy="fixture-supplied/v1",
    )
    fixture_spec = BacktestSpec(**fixture_values)
    calendar = TradingCalendar(Market.US, DATES)

    real = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=EmptyStrategy()
    ).run(real_spec)
    fixture = ChronologicalBacktestRunner(
        store=store, calendar=calendar, strategy=EmptyStrategy()
    ).run(fixture_spec)

    assert real.manifest.pit_knowledge_policy == "current-view-baseline/v1"
    assert real.manifest.market_data_price_policy == (
        "raw-unadjusted/no-corporate-actions/v1"
    )
    assert real.spec_fingerprint != fixture.spec_fingerprint


def _spec_values(spec: BacktestSpec) -> dict[str, object]:
    return {name: getattr(spec, name) for name in BacktestSpec.model_fields}


def _session_values(session: BacktestSession) -> dict[str, object]:
    return {name: getattr(session, name) for name in BacktestSession.model_fields}


def _source_values(source: OpenFrameSource) -> dict[str, object]:
    return {name: getattr(source, name) for name in OpenFrameSource.model_fields}


def test_real_policy_pair_requires_nonempty_exact_universe_provenance() -> None:
    store = PointInTimeStore()
    _populate(store)
    real_spec = _build(store)
    empty_sessions = tuple(
        BacktestSession(**(_session_values(session) | {"open_frame_sources": ()}))
        for session in real_spec.sessions
    )

    with pytest.raises(ValidationError, match=r"real-data.*provenance"):
        BacktestSpec(**(_spec_values(real_spec) | {"sessions": empty_sessions}))

    first = real_spec.sessions[0]
    reversed_sources = tuple(reversed(first.open_frame_sources))
    with pytest.raises(ValidationError, match="exactly match open bars"):
        BacktestSession(
            **(_session_values(first) | {"open_frame_sources": reversed_sources})
        )

    wrong_universe_source = OpenFrameSource(
        **(_source_values(first.open_frame_sources[0]) | {"symbol": "NVDA"})
    )
    with pytest.raises(ValidationError, match="exactly match open bars"):
        BacktestSession(
            **(
                _session_values(first)
                | {
                    "open_frame_sources": (
                        wrong_universe_source,
                        first.open_frame_sources[1],
                    )
                }
            )
        )


@pytest.mark.parametrize(
    ("pit_policy", "price_policy"),
    [
        ("current-view-baseline/v1", "fixture-supplied/v1"),
        (
            "business-available-at/v1",
            "raw-unadjusted/no-corporate-actions/v1",
        ),
    ],
)
def test_real_policies_are_an_atomic_pair(
    pit_policy: str, price_policy: str
) -> None:
    store = PointInTimeStore()
    _populate(store)
    real_spec = _build(store)

    with pytest.raises(ValidationError, match="policy pair"):
        BacktestSpec(
            **(
                _spec_values(real_spec)
                | {
                    "pit_knowledge_policy": pit_policy,
                    "market_data_price_policy": price_policy,
                }
            )
        )


@pytest.mark.parametrize("mismatch", ["source", "open"])
def test_real_provenance_mismatch_is_rejected_before_mutable_execution(
    mismatch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PointInTimeStore()
    _populate(store)
    spec = _build(store, run_id=f"bad-{mismatch}")
    first = spec.sessions[0]
    if mismatch == "source":
        changed_source = OpenFrameSource(
            **(
                _source_values(first.open_frame_sources[0])
                | {"source_record_id": "wrong-record"}
            )
        )
        changed_session = BacktestSession(
            **(
                _session_values(first)
                | {
                    "open_frame_sources": (
                        changed_source,
                        first.open_frame_sources[1],
                    )
                }
            )
        )
    else:
        changed_bar = Bar(
            **(
                first.open_bars[0].model_dump()
                | {
                    "open": first.open_bars[0].open + Decimal("1"),
                    "high": first.open_bars[0].open + Decimal("1"),
                    "low": first.open_bars[0].open + Decimal("1"),
                    "close": first.open_bars[0].open + Decimal("1"),
                }
            )
        )
        changed_session = BacktestSession(
            **(
                _session_values(first)
                | {"open_bars": (changed_bar, first.open_bars[1])}
            )
        )
    changed_spec = BacktestSpec(
        **(_spec_values(spec) | {"sessions": (changed_session, *spec.sessions[1:])})
    )
    strategy = EmptyStrategy()

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("mutable execution must not begin")

    monkeypatch.setattr(strategy, "evaluate", unexpected)
    runner = ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(Market.US, DATES),
        strategy=strategy,
    )
    with pytest.raises(ValueError, match="open provenance"):
        runner.run(changed_spec)


def test_independent_runners_over_same_frozen_inputs_are_byte_equivalent() -> None:
    stores = (PointInTimeStore(), PointInTimeStore())
    for store in stores:
        _populate(store)
    specs = tuple(_build(store, run_id="byte-equivalent") for store in stores)
    results = tuple(
        ChronologicalBacktestRunner(
            store=store,
            calendar=TradingCalendar(Market.US, DATES),
            strategy=EmptyStrategy(),
        ).run(spec)
        for store, spec in zip(stores, specs, strict=True)
    )
    assert results[0].model_dump_json().encode() == results[1].model_dump_json().encode()
