from typing import Final, Literal, TypeAlias

BUSINESS_AVAILABLE_PIT_POLICY: Final = "business-available-at/v1"
CURRENT_VIEW_BASELINE_PIT_POLICY: Final = "current-view-baseline/v1"
FIXTURE_PRICE_POLICY: Final = "fixture-supplied/v1"
RAW_UNADJUSTED_PRICE_POLICY: Final = "raw-unadjusted/no-corporate-actions/v1"

PitKnowledgePolicy: TypeAlias = Literal[
    "business-available-at/v1",
    "current-view-baseline/v1",
]
MarketDataPricePolicy: TypeAlias = Literal[
    "fixture-supplied/v1",
    "raw-unadjusted/no-corporate-actions/v1",
]
MarketDataPolicyPair: TypeAlias = tuple[
    PitKnowledgePolicy,
    MarketDataPricePolicy,
]

VALID_MARKET_DATA_POLICY_PAIRS: Final[frozenset[MarketDataPolicyPair]] = frozenset(
    {
        (BUSINESS_AVAILABLE_PIT_POLICY, FIXTURE_PRICE_POLICY),
        (CURRENT_VIEW_BASELINE_PIT_POLICY, RAW_UNADJUSTED_PRICE_POLICY),
    }
)


def is_real_market_data_policy(
    pit: PitKnowledgePolicy,
    price: MarketDataPricePolicy,
) -> bool:
    return (pit, price) == (
        CURRENT_VIEW_BASELINE_PIT_POLICY,
        RAW_UNADJUSTED_PRICE_POLICY,
    )


__all__ = [
    "BUSINESS_AVAILABLE_PIT_POLICY",
    "CURRENT_VIEW_BASELINE_PIT_POLICY",
    "FIXTURE_PRICE_POLICY",
    "RAW_UNADJUSTED_PRICE_POLICY",
    "VALID_MARKET_DATA_POLICY_PAIRS",
    "MarketDataPolicyPair",
    "MarketDataPricePolicy",
    "PitKnowledgePolicy",
    "is_real_market_data_policy",
]
