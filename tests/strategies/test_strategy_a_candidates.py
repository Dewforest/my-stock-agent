from datetime import UTC, date, datetime
from decimal import (
    Decimal,
    Inexact,
    Rounded,
    localcontext,
)

import pytest

from stock_agent.domain import Bar, Market, PortfolioSnapshot, Position, Side
from stock_agent.strategies.llm_contract import (
    StrategyAConfig,
    StrategyADataQuality,
    StrategyARegime,
)
from stock_agent.strategies.protocol import MarketSnapshot, StrategyContext
from stock_agent.strategies.strategy_a import build_strategy_a_candidates

AS_OF = datetime(2026, 7, 31, 20, tzinfo=UTC)


def config(**overrides: object) -> StrategyAConfig:
    values: dict[str, object] = {
        "config_version": "strategy-a-v1",
        "short_window": 2,
        "long_window": 3,
        "volume_window": 2,
        "volume_confirmation_threshold": Decimal("1"),
        "offensive_target_weight": Decimal("0.10"),
        "neutral_target_weight": Decimal("0.05"),
        "model_identity_policy_id": "exact-model-v1",
        "prompt_template_id": "strategy-a-decision-v1",
        "prompt_template_digest": "prompt-sha256:" + "a" * 64,
    }
    values.update(overrides)
    return StrategyAConfig(**values)  # type: ignore[arg-type]


def bar(day: int, close: str, volume: str, symbol: str = "IBM") -> Bar:
    session_date = date(2026, 7, day)
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        market=Market.US,
        session_date=session_date,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
        available_at=datetime(2026, 7, day, 20, tzinfo=UTC),
    )


def context(*bars: Bar, portfolio: PortfolioSnapshot | None = None) -> StrategyContext:
    snapshot = MarketSnapshot(as_of=AS_OF, market=Market.US, bars=tuple(bars))
    if portfolio is None:
        portfolio = PortfolioSnapshot(
            account_id="account-1",
            market=Market.US,
            cash=Decimal("1000"),
            nav=Decimal("1000"),
            peak_nav=Decimal("1000"),
            positions=(),
            as_of=AS_OF,
        )
    return StrategyContext(
        market_snapshot=snapshot,
        portfolio=portfolio,
        strategy_config_version="strategy-a-v1",
    )


def portfolio_with_market_value(market_value: str) -> PortfolioSnapshot:
    value = Decimal(market_value)
    positions = ()
    if value > 0:
        positions = (
            Position(
                symbol="IBM",
                quantity=Decimal("1"),
                average_cost=Decimal("10"),
                market_value=value,
            ),
        )
    return PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("1000") - value,
        nav=Decimal("1000"),
        peak_nav=Decimal("1000"),
        positions=positions,
        as_of=AS_OF,
    )


def test_builds_offensive_candidate_with_bounded_buy_and_hold_choices() -> None:
    evaluation = context(
        bar(28, "10", "100"),
        bar(29, "11", "100"),
        bar(30, "13", "250"),
    )

    candidates = build_strategy_a_candidates(evaluation, config())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.symbol == "IBM"
    assert candidate.regime is StrategyARegime.OFFENSIVE
    assert candidate.data_quality is StrategyADataQuality.COMPLETE
    assert candidate.short_sum == Decimal("24")
    assert candidate.long_sum == Decimal("34")
    assert candidate.prior_volume_sum == Decimal("200")
    assert candidate.latest_volume == Decimal("250")
    assert tuple((item.action, item.target_weight) for item in candidate.action_targets) == (
        (Side.BUY, Decimal("0.10")),
        (Side.HOLD, Decimal("0")),
    )
    assert candidate.reason_codes == ("positive-trend", "volume-confirmed")
    assert len(candidate.evidence_ids) == 3
    assert all(item.startswith("bar-sha256:") for item in candidate.evidence_ids)
    assert candidate.candidate_id.startswith("strategy-a-candidate-sha256:")


@pytest.mark.parametrize(
    ("bars", "market_value", "regime", "expected_actions"),
    [
        (
            (bar(28, "10", "100"), bar(29, "11", "100")),
            "50",
            StrategyARegime.INSUFFICIENT,
            ((Side.HOLD, Decimal("0.05")),),
        ),
        (
            (bar(28, "10", "100"), bar(29, "11", "100"), bar(30, "13", "0")),
            "0",
            StrategyARegime.NEUTRAL,
            ((Side.HOLD, Decimal("0")),),
        ),
        (
            (bar(28, "13", "100"), bar(29, "11", "100"), bar(30, "10", "100")),
            "100",
            StrategyARegime.DEFENSIVE,
            ((Side.SELL, Decimal("0")),),
        ),
        (
            (bar(28, "13", "100"), bar(29, "11", "100"), bar(30, "10", "100")),
            "0",
            StrategyARegime.DEFENSIVE,
            ((Side.HOLD, Decimal("0")),),
        ),
        (
            (bar(28, "10", "100"), bar(29, "11", "100"), bar(30, "13", "100")),
            "50",
            StrategyARegime.OFFENSIVE,
            ((Side.BUY, Decimal("0.10")), (Side.HOLD, Decimal("0.05"))),
        ),
        (
            (bar(28, "10", "100"), bar(29, "11", "100"), bar(30, "13", "100")),
            "100",
            StrategyARegime.OFFENSIVE,
            ((Side.HOLD, Decimal("0.10")),),
        ),
        (
            (bar(28, "10", "100"), bar(29, "11", "100"), bar(30, "13", "100")),
            "150",
            StrategyARegime.OFFENSIVE,
            ((Side.REDUCE, Decimal("0.10")),),
        ),
        (
            (bar(28, "10", "100"), bar(29, "10", "100"), bar(30, "10", "100")),
            "100",
            StrategyARegime.NEUTRAL,
            ((Side.REDUCE, Decimal("0.05")), (Side.SELL, Decimal("0"))),
        ),
        (
            (bar(28, "10", "100"), bar(29, "10", "100"), bar(30, "10", "100")),
            "50",
            StrategyARegime.NEUTRAL,
            ((Side.HOLD, Decimal("0.05")),),
        ),
    ],
)
def test_regime_and_hard_cap_action_matrix(
    bars: tuple[Bar, ...],
    market_value: str,
    regime: StrategyARegime,
    expected_actions: tuple[tuple[Side, Decimal], ...],
) -> None:
    candidate = build_strategy_a_candidates(
        context(*bars, portfolio=portfolio_with_market_value(market_value)),
        config(),
    )[0]

    assert candidate.regime is regime
    assert tuple(
        (item.action, item.target_weight) for item in candidate.action_targets
    ) == expected_actions


def test_zero_prior_volume_requires_positive_latest_volume() -> None:
    zero_latest = build_strategy_a_candidates(
        context(bar(28, "10", "0"), bar(29, "11", "0"), bar(30, "13", "0")),
        config(),
    )[0]
    positive_latest = build_strategy_a_candidates(
        context(bar(28, "10", "0"), bar(29, "11", "0"), bar(30, "13", "1")),
        config(),
    )[0]

    assert zero_latest.regime is StrategyARegime.NEUTRAL
    assert positive_latest.regime is StrategyARegime.OFFENSIVE


def test_zero_nav_never_authorizes_new_exposure() -> None:
    zero_nav = PortfolioSnapshot(
        account_id="account-1",
        market=Market.US,
        cash=Decimal("0"),
        nav=Decimal("0"),
        peak_nav=Decimal("0"),
        positions=(),
        as_of=AS_OF,
    )
    candidate = build_strategy_a_candidates(
        context(
            bar(28, "10", "100"),
            bar(29, "11", "100"),
            bar(30, "13", "100"),
            portfolio=zero_nav,
        ),
        config(),
    )[0]

    assert candidate.regime is StrategyARegime.OFFENSIVE
    assert tuple(
        (item.action, item.target_weight) for item in candidate.action_targets
    ) == ((Side.HOLD, Decimal("0")),)


def test_candidate_math_ignores_ambient_decimal_context() -> None:
    evaluation = context(
        bar(28, "1.234567", "100.123"),
        bar(29, "2.345678", "100.456"),
        bar(30, "3.456789", "250.789"),
    )
    expected = build_strategy_a_candidates(evaluation, config())

    with localcontext() as hostile:
        hostile.prec = 2
        hostile.traps[Inexact] = True
        hostile.traps[Rounded] = True
        actual = build_strategy_a_candidates(evaluation, config())

    assert actual == expected


def test_candidates_are_symbol_sorted_and_use_only_required_history() -> None:
    bars = (
        bar(27, "9", "90", "AAPL"),
        bar(28, "10", "100", "AAPL"),
        bar(29, "11", "100", "AAPL"),
        bar(30, "13", "100", "AAPL"),
        bar(27, "9", "90", "IBM"),
        bar(28, "10", "100", "IBM"),
        bar(29, "11", "100", "IBM"),
        bar(30, "13", "100", "IBM"),
    )

    candidates = build_strategy_a_candidates(context(*bars), config())

    assert tuple(item.symbol for item in candidates) == ("AAPL", "IBM")
    assert all(len(item.evidence_ids) == 3 for item in candidates)


def test_candidate_builder_rejects_config_version_mismatch() -> None:
    with pytest.raises(ValueError, match="versions"):
        build_strategy_a_candidates(
            context(bar(28, "10", "100")),
            config(config_version="other"),
        )
