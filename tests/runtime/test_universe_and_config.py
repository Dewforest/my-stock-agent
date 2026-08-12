from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_agent.domain import Currency, Market
from stock_agent.runtime import (
    EXPECTED_RUNTIME_CONFIG_V1_DIGEST,
    EXPECTED_UNIVERSE_V1_DIGEST,
    MarketAccountProfile,
    ModelRuntimeProfile,
    PaperRuntimeConfig,
    RuntimeConfigLoadError,
    UniverseSnapshot,
    canonical_runtime_config_digest,
    canonical_universe_digest,
    load_paper_runtime_config,
)

CONFIG_PATH = Path(__file__).parents[2] / "config" / "paper-runtime-v1.toml"


def test_v1_config_loads_exact_dual_market_profile() -> None:
    config = load_paper_runtime_config(CONFIG_PATH)

    assert config.schema_version == "paper-runtime-config/v1"
    assert canonical_runtime_config_digest(config) == config.config_digest
    assert config.config_digest == (
        "runtime-config-sha256:a9bcc9ab70e3d818082415f130f2ac5d546153cc992272c677feaf92ec220e14"
    )
    assert config.universe.universe_id == "dual-market-20/v1"
    cn_symbols = tuple(
        member.symbol for member in config.universe.members if member.market is Market.CN
    )
    assert cn_symbols == (
        "000333",
        "000858",
        "300750",
        "600030",
        "600036",
        "600276",
        "600519",
        "600900",
        "601088",
        "601318",
    )
    us_symbols = tuple(
        member.symbol for member in config.universe.members if member.market is Market.US
    )
    assert us_symbols == (
        "AAPL",
        "AMZN",
        "GOOGL",
        "JNJ",
        "JPM",
        "META",
        "MSFT",
        "NVDA",
        "PG",
        "XOM",
    )
    assert config.accounts[0].market is Market.CN
    assert config.accounts[0].currency is Currency.CNY
    assert config.accounts[0].initial_cash == Decimal("1000000")
    assert config.accounts[1].market is Market.US
    assert config.accounts[1].currency is Currency.USD
    assert config.accounts[1].initial_cash == Decimal("100000")
    assert config.model.model == "deepseek-v4-pro"
    assert config.model.max_tokens == 1024


def test_universe_digest_is_declaration_order_independent_and_emission_is_stable() -> None:
    config = load_paper_runtime_config(CONFIG_PATH)
    reversed_snapshot = UniverseSnapshot(
        universe_id=config.universe.universe_id,
        digest=config.universe.digest,
        members=tuple(reversed(config.universe.members)),
    )

    assert reversed_snapshot.members == config.universe.members
    assert canonical_universe_digest(reversed_snapshot) == config.universe.digest
    assert config.universe.digest == (
        "universe-sha256:79619ee5617807e252f9a35ce6c95a4d3a43a97f85f54b300c6493d98b67a177"
    )


def test_loader_rejects_v1_content_changed_without_digest_update(tmp_path: Path) -> None:
    tampered = tmp_path / "runtime.toml"
    tampered.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            'display_name = "Apple"', 'display_name = "Tampered Apple"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigLoadError) as captured:
        load_paper_runtime_config(tampered)
    assert captured.value.code == "invalid_config"


def test_v1_rejects_self_consistent_but_unapproved_universe(tmp_path: Path) -> None:
    original = load_paper_runtime_config(CONFIG_PATH)
    source_member = original.universe.members[0]
    changed_member = type(source_member)(
        **{
            **source_member.model_dump(),
            "display_name": "Tampered Name",
        }
    )
    changed = UniverseSnapshot.model_construct(
        universe_id=original.universe.universe_id,
        digest=original.universe.digest,
        members=(changed_member, *original.universe.members[1:]),
    )
    changed_digest = canonical_universe_digest(changed)
    tampered = tmp_path / "runtime.toml"
    tampered.write_text(
        CONFIG_PATH.read_text(encoding="utf-8")
        .replace(original.universe.digest, changed_digest)
        .replace(
            f'display_name = "{original.universe.members[0].display_name}"',
            'display_name = "Tampered Name"',
        ),
        encoding="utf-8",
    )

    assert changed_digest != EXPECTED_UNIVERSE_V1_DIGEST
    with pytest.raises(RuntimeConfigLoadError) as captured:
        load_paper_runtime_config(tampered)
    assert captured.value.code == "invalid_config"


def test_snapshot_rejects_approved_digest_over_changed_member_content() -> None:
    original = load_paper_runtime_config(CONFIG_PATH).universe
    source_member = original.members[0]
    changed_member = type(source_member)(
        **{**source_member.model_dump(), "display_name": "Changed without digest"}
    )

    with pytest.raises(ValidationError, match="canonical content"):
        UniverseSnapshot(
            universe_id=original.universe_id,
            digest=original.digest,
            members=(changed_member, *original.members[1:]),
        )


def test_config_rejects_polluted_exact_universe_model() -> None:
    original = load_paper_runtime_config(CONFIG_PATH)
    polluted = UniverseSnapshot.model_validate(original.universe.model_dump())
    object.__setattr__(polluted, "members", [])

    with pytest.raises(ValidationError, match="universe"):
        PaperRuntimeConfig(
            schema_version=original.schema_version,
            profile_id=original.profile_id,
            config_digest=original.config_digest,
            universe=polluted,
            accounts=original.accounts,
            market_data=original.market_data,
            model=original.model,
            strategy=original.strategy,
            schedules=original.schedules,
        )


def test_runtime_models_reject_coercion_and_duplicate_market_bindings() -> None:
    with pytest.raises(ValidationError):
        MarketAccountProfile.model_validate(
            {
                "account_id": "paper-cn-v1",
                "market": "CN",
                "currency": "CNY",
                "initial_cash": "1000000",
            }
        )

    original = load_paper_runtime_config(CONFIG_PATH)
    with pytest.raises(ValidationError, match="accounts"):
        PaperRuntimeConfig(
            schema_version=original.schema_version,
            profile_id=original.profile_id,
            config_digest=original.config_digest,
            universe=original.universe,
            accounts=(original.accounts[0], original.accounts[0]),
            market_data=original.market_data,
            model=original.model,
            strategy=original.strategy,
            schedules=original.schedules,
        )


def test_v1_profile_rejects_unapproved_model_or_authority_upgrade() -> None:
    original = load_paper_runtime_config(CONFIG_PATH)
    changed_model = ModelRuntimeProfile(
        provider_id=original.model.provider_id,
        model="other-model",
        max_tokens=original.model.max_tokens,
        identity_policy_id=original.model.identity_policy_id,
        prompt_template_id=original.model.prompt_template_id,
        keychain_service=original.model.keychain_service,
        keychain_account=original.model.keychain_account,
    )
    with pytest.raises(ValidationError, match="model"):
        PaperRuntimeConfig(
            schema_version=original.schema_version,
            profile_id=original.profile_id,
            config_digest=original.config_digest,
            universe=original.universe,
            accounts=original.accounts,
            market_data=original.market_data,
            model=changed_model,
            strategy=original.strategy,
            schedules=original.schedules,
        )

    schedule_type = type(original.schedules[0])
    upgraded_schedule = schedule_type(
        market=original.schedules[0].market,
        version=original.schedules[0].version,
        authority_status="OFFICIAL",
    )
    with pytest.raises(ValidationError, match="schedule"):
        PaperRuntimeConfig(
            schema_version=original.schema_version,
            profile_id=original.profile_id,
            config_digest=original.config_digest,
            universe=original.universe,
            accounts=original.accounts,
            market_data=original.market_data,
            model=original.model,
            strategy=original.strategy,
            schedules=(upgraded_schedule, original.schedules[1]),
        )


def test_config_digest_and_immutable_copy_boundary_cannot_be_bypassed() -> None:
    original = load_paper_runtime_config(CONFIG_PATH)
    with pytest.raises(ValidationError, match="config digest"):
        PaperRuntimeConfig(
            schema_version=original.schema_version,
            profile_id=original.profile_id,
            config_digest="runtime-config-sha256:" + "0" * 64,
            universe=original.universe,
            accounts=original.accounts,
            market_data=original.market_data,
            model=original.model,
            strategy=original.strategy,
            schedules=original.schedules,
        )

    with pytest.raises(TypeError, match="copy updates"):
        original.model_copy(update={"profile_id": "tampered"})

    with pytest.raises(TypeError, match="partial copies"):
        original.copy(exclude={"profile_id"})


def test_v1_freezes_strategy_keychain_and_runtime_digest_literals() -> None:
    config = load_paper_runtime_config(CONFIG_PATH)

    assert config.config_digest == EXPECTED_RUNTIME_CONFIG_V1_DIGEST
    assert config.strategy.short_window == 3
    assert config.strategy.long_window == 5
    assert config.strategy.volume_window == 4
    assert config.strategy.required_history == 5
    assert config.strategy.volume_confirmation_threshold == Decimal("1.20")
    assert config.strategy.offensive_target_weight == Decimal("0.10")
    assert config.strategy.neutral_target_weight == Decimal("0.05")
    assert config.strategy.prompt_template_id == "strategy-a-openai-compatible-json/v1"
    assert config.strategy.prompt_template_digest == (
        "prompt-sha256:64cfe18f171b3ff4acdd325d7bbf1f1a4c78f571ac58a2b8af7a27fd97d49045"
    )
    assert config.model.keychain_account == "amezf"
    assert config.market_data[1].keychain_account == "amezf"


def test_loader_rejects_raw_toml_type_coercion_with_stable_error(tmp_path: Path) -> None:
    tampered = tmp_path / "runtime.toml"
    tampered.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(
            'initial_cash = "1000000"', "initial_cash = 1000000"
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigLoadError) as captured:
        load_paper_runtime_config(tampered)
    assert captured.value.code == "invalid_config"


@pytest.mark.parametrize(
    "old,new",
    [
        ('symbol = "600519"', 'ticker = "600519"'),
        ('market = "CN"', 'market = "XX"'),
        ('initial_cash = "1000000"', 'initial_cash = { value = "1000000" }'),
        ('initial_cash = "1000000"', 'initial_cash = "not-a-decimal"'),
        (
            'volume_confirmation_threshold = "1.20"',
            'volume_confirmation_threshold = "not-a-decimal"',
        ),
    ],
)
def test_loader_normalizes_invalid_config_errors(tmp_path: Path, old: str, new: str) -> None:
    tampered = tmp_path / "runtime.toml"
    tampered.write_text(
        CONFIG_PATH.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigLoadError) as captured:
        load_paper_runtime_config(tampered)
    assert captured.value.code == "invalid_config"
