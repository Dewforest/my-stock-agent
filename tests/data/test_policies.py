from stock_agent.backtest.models import BacktestInputManifest, BacktestSpec
from stock_agent.data.policies import (
    BUSINESS_AVAILABLE_PIT_POLICY,
    CURRENT_VIEW_BASELINE_PIT_POLICY,
    FIXTURE_PRICE_POLICY,
    RAW_UNADJUSTED_PRICE_POLICY,
    VALID_MARKET_DATA_POLICY_PAIRS,
    is_real_market_data_policy,
)

FIXTURE_POLICY_PAIR = (
    BUSINESS_AVAILABLE_PIT_POLICY,
    FIXTURE_PRICE_POLICY,
)
REAL_POLICY_PAIR = (
    CURRENT_VIEW_BASELINE_PIT_POLICY,
    RAW_UNADJUSTED_PRICE_POLICY,
)


def test_shared_policy_authority_defines_only_supported_atomic_pairs() -> None:
    assert VALID_MARKET_DATA_POLICY_PAIRS == frozenset(
        {FIXTURE_POLICY_PAIR, REAL_POLICY_PAIR}
    )
    assert not is_real_market_data_policy(*FIXTURE_POLICY_PAIR)
    assert is_real_market_data_policy(*REAL_POLICY_PAIR)
    assert not is_real_market_data_policy(
        CURRENT_VIEW_BASELINE_PIT_POLICY,
        FIXTURE_PRICE_POLICY,
    )


def test_backtest_model_defaults_reference_shared_fixture_policy_pair() -> None:
    assert BacktestSpec.model_fields["pit_knowledge_policy"].default is (
        BUSINESS_AVAILABLE_PIT_POLICY
    )
    assert BacktestSpec.model_fields["market_data_price_policy"].default is (
        FIXTURE_PRICE_POLICY
    )
    assert BacktestInputManifest.model_fields["market_data_price_policy"].default is (
        FIXTURE_PRICE_POLICY
    )
