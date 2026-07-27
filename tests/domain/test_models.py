from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

import stock_agent.domain as domain
from stock_agent.domain.models import (
    Bar,
    Currency,
    Instrument,
    Market,
    PortfolioSnapshot,
    Position,
    Side,
    StrategyIntent,
)


def test_instrument_strips_text_fields() -> None:
    instrument = Instrument(
        symbol=" 600519 ",
        market=Market.CN,
        currency=Currency.CNY,
        sector=" Consumer Staples ",
    )

    assert instrument.symbol == "600519"
    assert instrument.sector == "Consumer Staples"


@pytest.mark.parametrize("field", ["symbol", "sector"])
def test_instrument_rejects_blank_text(field: str) -> None:
    values = {
        "symbol": "600519",
        "market": Market.CN,
        "currency": Currency.CNY,
        "sector": "Consumer Staples",
    }
    values[field] = "   "

    with pytest.raises(ValidationError):
        Instrument(**values)


@pytest.mark.parametrize(
    ("market", "currency"),
    [(Market.CN, Currency.USD), (Market.US, Currency.CNY)],
)
def test_instrument_rejects_market_currency_mismatch(
    market: Market, currency: Currency
) -> None:
    with pytest.raises(ValidationError):
        Instrument(symbol="AAPL", market=market, currency=currency, sector="Technology")


def test_instrument_is_frozen() -> None:
    instrument = Instrument(
        symbol="AAPL", market=Market.US, currency=Currency.USD, sector="Technology"
    )

    with pytest.raises(ValidationError):
        instrument.symbol = "MSFT"


def test_instrument_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Instrument(
            symbol="AAPL",
            market=Market.US,
            currency=Currency.USD,
            sector="Technology",
            exchange="NASDAQ",
        )


def make_intent(**overrides: object) -> StrategyIntent:
    values = {
        "strategy_id": "momentum-v1",
        "symbol": "AAPL",
        "market": Market.US,
        "side": Side.BUY,
        "target_weight": Decimal("0.25"),
        "confidence": 80,
        "as_of": datetime(2026, 7, 27, 12, tzinfo=UTC),
        "thesis": "Earnings momentum",
        "invalidation": "Guidance cut",
    }
    values.update(overrides)
    return StrategyIntent(**values)


@pytest.mark.parametrize("target_weight", [Decimal("-0.01"), Decimal("1.01")])
def test_strategy_intent_rejects_target_weight_outside_unit_interval(
    target_weight: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        make_intent(target_weight=target_weight)


@pytest.mark.parametrize("target_weight", [Decimal("0"), Decimal("1")])
def test_strategy_intent_accepts_target_weight_boundaries(target_weight: Decimal) -> None:
    assert make_intent(target_weight=target_weight).target_weight == target_weight


def test_strategy_intent_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        make_intent(as_of=datetime(2026, 7, 27, 12))


@pytest.mark.parametrize(
    "tzinfo", [UTC, timezone(timedelta(hours=5, minutes=30))]
)
def test_strategy_intent_accepts_timezone_aware_as_of(tzinfo: timezone) -> None:
    as_of = datetime(2026, 7, 27, 12, tzinfo=tzinfo)
    assert make_intent(as_of=as_of).as_of == as_of


def test_sell_strategy_intent_requires_zero_target_weight() -> None:
    with pytest.raises(ValidationError):
        make_intent(side=Side.SELL, target_weight=Decimal("0.1"))


@pytest.mark.parametrize("confidence", [-1, 101])
def test_strategy_intent_rejects_confidence_outside_percentage(confidence: int) -> None:
    with pytest.raises(ValidationError):
        make_intent(confidence=confidence)


@pytest.mark.parametrize("confidence", [0, 100])
def test_strategy_intent_accepts_confidence_boundaries(confidence: int) -> None:
    assert make_intent(confidence=confidence).confidence == confidence


@pytest.mark.parametrize(
    "field", ["strategy_id", "symbol", "thesis", "invalidation"]
)
def test_strategy_intent_rejects_blank_text(field: str) -> None:
    with pytest.raises(ValidationError):
        make_intent(**{field: "   "})


def test_strategy_intent_strips_text_and_freezes_evidence_ids() -> None:
    intent = make_intent(
        strategy_id=" momentum-v1 ",
        symbol=" AAPL ",
        thesis=" Earnings momentum ",
        invalidation=" Guidance cut ",
        evidence_ids=["filing-1", "price-1"],
    )

    assert intent.strategy_id == "momentum-v1"
    assert intent.symbol == "AAPL"
    assert intent.thesis == "Earnings momentum"
    assert intent.invalidation == "Guidance cut"
    assert intent.evidence_ids == ("filing-1", "price-1")
    assert isinstance(intent.evidence_ids, tuple)

    with pytest.raises(ValidationError):
        intent.confidence = 90


def make_bar(**overrides: object) -> Bar:
    values = {
        "symbol": "AAPL",
        "market": Market.US,
        "session_date": date(2026, 7, 27),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": Decimal("1000000"),
        "available_at": datetime(2026, 7, 27, 21, tzinfo=UTC),
    }
    values.update(overrides)
    return Bar(**values)


def test_bar_preserves_decimal_values() -> None:
    bar = make_bar()
    assert all(isinstance(value, Decimal) for value in (bar.open, bar.high, bar.low, bar.close))
    assert isinstance(bar.volume, Decimal)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", 100.0),
        ("high", 110.0),
        ("low", 90.0),
        ("close", 105.0),
        ("volume", 1000000.0),
    ],
)
def test_bar_rejects_float_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_bar(**{field: value})


def test_bar_rejects_naive_available_at() -> None:
    with pytest.raises(ValidationError):
        make_bar(available_at=datetime(2026, 7, 27, 21))


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1")])
def test_bar_rejects_non_positive_prices(field: str, value: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_bar(**{field: value})


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        make_bar(volume=Decimal("-1"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"high": Decimal("99")},
        {"low": Decimal("101")},
        {"high": Decimal("104"), "close": Decimal("105")},
        {"low": Decimal("91"), "close": Decimal("90")},
    ],
)
def test_bar_rejects_invalid_ohlc_relationships(overrides: dict[str, Decimal]) -> None:
    with pytest.raises(ValidationError):
        make_bar(**overrides)


def make_position(**overrides: object) -> Position:
    values = {
        "symbol": "AAPL",
        "quantity": Decimal("10"),
        "average_cost": Decimal("100"),
        "market_value": Decimal("1050"),
    }
    values.update(overrides)
    return Position(**values)


@pytest.mark.parametrize("field", ["quantity", "average_cost"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1")])
def test_position_rejects_non_positive_quantity_and_average_cost(
    field: str, value: Decimal
) -> None:
    with pytest.raises(ValidationError):
        make_position(**{field: value})


def test_position_rejects_negative_market_value() -> None:
    with pytest.raises(ValidationError):
        make_position(market_value=Decimal("-1"))


def test_position_strips_symbol_and_accepts_zero_market_value() -> None:
    position = make_position(symbol=" AAPL ", market_value=Decimal("0"))
    assert position.symbol == "AAPL"
    assert position.market_value == Decimal("0")


def make_snapshot(**overrides: object) -> PortfolioSnapshot:
    values = {
        "account_id": "brokerage-1",
        "market": Market.US,
        "cash": Decimal("1000"),
        "nav": Decimal("10000"),
        "peak_nav": Decimal("12000"),
        "as_of": datetime(2026, 7, 27, 21, tzinfo=UTC),
    }
    values.update(overrides)
    return PortfolioSnapshot(**values)


@pytest.mark.parametrize("field", ["cash", "nav", "peak_nav"])
def test_portfolio_snapshot_rejects_negative_amounts(field: str) -> None:
    with pytest.raises(ValidationError):
        make_snapshot(**{field: Decimal("-0.01")})


def test_portfolio_snapshot_rejects_peak_below_nav() -> None:
    with pytest.raises(ValidationError):
        make_snapshot(nav=Decimal("100"), peak_nav=Decimal("99.99"))


def test_portfolio_snapshot_rejects_duplicate_position_symbols() -> None:
    positions = [make_position(symbol="AAPL"), make_position(symbol="AAPL")]
    with pytest.raises(ValidationError):
        make_snapshot(positions=positions)


def test_portfolio_snapshot_positions_are_an_immutable_tuple() -> None:
    snapshot = make_snapshot(positions=[make_position()])
    assert snapshot.positions == (make_position(),)
    assert isinstance(snapshot.positions, tuple)

    with pytest.raises(ValidationError):
        snapshot.cash = Decimal("500")


def test_portfolio_snapshot_defaults_to_empty_positions() -> None:
    assert make_snapshot().positions == ()


def test_portfolio_snapshot_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        make_snapshot(as_of=datetime(2026, 7, 27, 21))


def test_portfolio_snapshot_strips_account_id_and_accepts_zero_amounts() -> None:
    snapshot = make_snapshot(
        account_id=" brokerage-1 ", cash=Decimal("0"), nav=Decimal("0"), peak_nav=Decimal("0")
    )
    assert snapshot.account_id == "brokerage-1"
    assert snapshot.cash == snapshot.nav == snapshot.peak_nav == Decimal("0")


def test_domain_public_api_exports_only_domain_types() -> None:
    expected_names = {
        "Market",
        "Currency",
        "Side",
        "Instrument",
        "Bar",
        "StrategyIntent",
        "Position",
        "PortfolioSnapshot",
    }
    assert set(domain.__all__) == expected_names
    assert {name for name in expected_names if hasattr(domain, name)} == expected_names


def test_enum_members_are_stable_strings() -> None:
    assert list(Market) == [Market.CN, Market.US]
    assert list(Currency) == [Currency.CNY, Currency.USD]
    assert list(Side) == [Side.BUY, Side.HOLD, Side.REDUCE, Side.SELL]
    assert Market.CN == "CN"
    assert Currency.USD == "USD"
    assert Side.REDUCE == "REDUCE"
