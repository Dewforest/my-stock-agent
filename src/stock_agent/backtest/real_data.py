from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from stock_agent.backtest.models import (
    BacktestSession,
    BacktestSpec,
    OpenFrameSource,
)
from stock_agent.data import PointInTimeStore
from stock_agent.data.policies import (
    CURRENT_VIEW_BASELINE_PIT_POLICY,
    RAW_UNADJUSTED_PRICE_POLICY,
)
from stock_agent.data.providers.models import BoundedSessionSchedule
from stock_agent.domain import Bar, Instrument, Market
from stock_agent.execution.cn_rules import CnSessionState


def _model_values(value: object, fields: Mapping[str, object]) -> dict[str, object]:
    try:
        instance_values = value.__dict__
    except AttributeError as error:
        raise ValueError("model has no field storage") from error
    if set(instance_values) != set(fields):
        raise ValueError("model has polluted or missing fields")
    values: dict[str, object] = {}
    for name in fields:
        try:
            values[name] = getattr(value, name)
        except AttributeError as error:
            raise ValueError(f"model is missing field {name!r}") from error
    return values


def build_real_data_backtest_spec(
    *,
    store: PointInTimeStore,
    schedule: BoundedSessionSchedule,
    run_id: str,
    account_id: str,
    instruments: tuple[Instrument, ...],
    initial_cash: Decimal,
    strategy_config_version: str,
    cn_session_states_by_date: Mapping[
        date, tuple[CnSessionState, ...]
    ] | None = None,
) -> BacktestSpec:
    if type(store) is not PointInTimeStore:
        raise TypeError("store must be exactly PointInTimeStore")
    if type(schedule) is not BoundedSessionSchedule:
        raise TypeError("schedule must be exactly BoundedSessionSchedule")
    clean_schedule = BoundedSessionSchedule.model_validate(
        _model_values(schedule, BoundedSessionSchedule.model_fields),
        strict=True,
    )
    if type(instruments) is not tuple or not instruments:
        raise TypeError("instruments must be a nonempty exact tuple")
    clean_instruments: list[Instrument] = []
    for instrument in instruments:
        if type(instrument) is not Instrument:
            raise TypeError("instruments must contain exact Instrument values")
        clean_instruments.append(
            Instrument.model_validate(
                _model_values(instrument, Instrument.model_fields),
                strict=True,
            )
        )
    instrument_tuple = tuple(clean_instruments)
    symbols = tuple(item.symbol for item in instrument_tuple)
    if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
        raise ValueError("instruments must be symbol-sorted and unique")
    if any(item.market is not clean_schedule.market for item in instrument_tuple):
        raise ValueError("instrument markets must match schedule market")
    if clean_schedule.market is Market.CN and cn_session_states_by_date is None:
        raise ValueError("CN real-data builds require explicit session states")
    if clean_schedule.market is Market.US and cn_session_states_by_date is not None:
        raise ValueError("US real-data builds must not contain CN session states")

    sessions: list[BacktestSession] = []
    for schedule_row in clean_schedule.sessions:
        open_bars: list[Bar] = []
        sources: list[OpenFrameSource] = []
        for instrument in instrument_tuple:
            revision = store.latest_bar_revision_as_of(
                market=clean_schedule.market,
                symbol=instrument.symbol,
                session_date=schedule_row.session_date,
                as_of=schedule_row.close_at,
            )
            if revision is None:
                raise ValueError(
                    "missing session data for "
                    f"{instrument.symbol} on {schedule_row.session_date.isoformat()}"
                )
            open_bars.append(
                Bar(
                    symbol=instrument.symbol,
                    market=clean_schedule.market,
                    session_date=schedule_row.session_date,
                    open=revision.bar.open,
                    high=revision.bar.open,
                    low=revision.bar.open,
                    close=revision.bar.open,
                    volume=Decimal(0),
                    available_at=schedule_row.open_at,
                )
            )
            sources.append(
                OpenFrameSource(
                    market=clean_schedule.market,
                    symbol=instrument.symbol,
                    session_date=schedule_row.session_date,
                    source=revision.source,
                    source_record_id=revision.source_record_id,
                    available_at=revision.bar.available_at,
                    ingested_at=revision.ingested_at,
                )
            )
        states: tuple[CnSessionState, ...] = ()
        if cn_session_states_by_date is not None:
            try:
                raw_states = cn_session_states_by_date[schedule_row.session_date]
            except KeyError as error:
                raise ValueError(
                    "missing CN session states for "
                    f"{schedule_row.session_date.isoformat()}"
                ) from error
            if type(raw_states) is not tuple:
                raise TypeError("CN session states must be exact tuples")
            states = tuple(
                CnSessionState.model_validate(
                    _model_values(state, CnSessionState.model_fields),
                    strict=True,
                )
                for state in raw_states
            )
        sessions.append(
            BacktestSession(
                session_date=schedule_row.session_date,
                open_at=schedule_row.open_at,
                close_at=schedule_row.close_at,
                open_bars=tuple(open_bars),
                open_frame_sources=tuple(sources),
                cn_session_states=states,
            )
        )

    return BacktestSpec(
        run_id=run_id,
        account_id=account_id,
        market=clean_schedule.market,
        initial_cash=initial_cash,
        instruments=instrument_tuple,
        sessions=tuple(sessions),
        strategy_config_version=strategy_config_version,
        pit_knowledge_policy=CURRENT_VIEW_BASELINE_PIT_POLICY,
        market_data_price_policy=RAW_UNADJUSTED_PRICE_POLICY,
    )
