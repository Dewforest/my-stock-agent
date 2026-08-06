from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    FloatOperation,
    Inexact,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)

from stock_agent.domain import Bar, Position, Side, StrategyIntent
from stock_agent.strategies.evidence import bar_evidence_id_for
from stock_agent.strategies.llm_contract import (
    DecisionPhase,
    LLMDecisionRecord,
    LLMDecisionRequest,
    StrategyAActionTarget,
    StrategyACandidateEnvelope,
    StrategyAConfig,
    StrategyADataQuality,
    StrategyARegime,
    candidate_id_for,
    portfolio_snapshot_id_for,
    request_fingerprint_for,
)
from stock_agent.strategies.llm_provider import LLMDecisionProvider
from stock_agent.strategies.protocol import StrategyContext

STRATEGY_A_ID = "strategy-a-bounded-llm"
_ADAPTER_ERROR = "Strategy A bounded decision failed"


class StrategyAAdapterError(RuntimeError):
    """Stable, secret-free failure at the bounded Strategy A decision boundary."""


@dataclass(frozen=True, slots=True)
class BoundedLLMStrategyA:
    config: StrategyAConfig
    provider: LLMDecisionProvider
    model_identity_policy_id: str
    prompt_template_id: str
    prompt_template_digest: str
    strategy_id: str = field(default=STRATEGY_A_ID, init=False)
    config_version: str = field(init=False)

    def __post_init__(self) -> None:
        validated = _exact_config(self.config)
        if not isinstance(self.provider, LLMDecisionProvider):
            raise TypeError("provider must satisfy LLMDecisionProvider")
        provenance = (
            self.model_identity_policy_id,
            self.prompt_template_id,
            self.prompt_template_digest,
        )
        if any(type(value) is not str for value in provenance) or provenance != (
            validated.model_identity_policy_id,
            validated.prompt_template_id,
            validated.prompt_template_digest,
        ):
            raise ValueError("Strategy A constructor provenance must exactly match config")
        object.__setattr__(self, "config_version", validated.config_version)

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]:
        validated_config = _exact_config(self.config)
        validated_context = _exact_strategy_context(context)
        if (
            self.strategy_id != STRATEGY_A_ID
            or self.config_version != validated_config.config_version
            or validated_context.strategy_config_version != validated_config.config_version
        ):
            raise ValueError("strategy context and Strategy A config versions do not match")
        if (
            self.model_identity_policy_id != validated_config.model_identity_policy_id
            or self.prompt_template_id != validated_config.prompt_template_id
            or self.prompt_template_digest != validated_config.prompt_template_digest
        ):
            raise ValueError("Strategy A adapter provenance does not match config")

        candidates = build_strategy_a_candidates(validated_context, validated_config)
        if not candidates:
            return ()

        request = _request_for(validated_context, validated_config, candidates)
        try:
            record = _exact_record(self.provider.decide(request))
            _require_record_matches_request(record, request)
            return _intents_for(record, request)
        except Exception:
            raise StrategyAAdapterError(_ADAPTER_ERROR) from None


def _exact_strategy_context(context: object) -> StrategyContext:
    if type(context) is not StrategyContext:
        raise TypeError("context must be an exact StrategyContext")
    if set(context.__dict__) != set(StrategyContext.model_fields):
        raise ValueError("StrategyContext is polluted")
    values = {name: getattr(context, name) for name in StrategyContext.model_fields}
    with localcontext(_context_validation_context(context)):
        return StrategyContext.model_validate(values, strict=True)


def _context_validation_context(context: StrategyContext) -> Context:
    portfolio = context.portfolio
    values = (
        portfolio.cash,
        portfolio.nav,
        portfolio.peak_nav,
        *(position.market_value for position in portfolio.positions),
    )
    max_digits = max(len(value.as_tuple().digits) for value in values)
    precision = max_digits + len(str(len(portfolio.positions) + 1)) + 2
    return Context(
        prec=min(MAX_PREC, max(50, precision)),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )


def _exact_config(config: object) -> StrategyAConfig:
    if type(config) is not StrategyAConfig:
        raise TypeError("config must be an exact StrategyAConfig")
    if set(config.__dict__) != set(StrategyAConfig.model_fields):
        raise ValueError("StrategyAConfig is polluted")
    values = {name: getattr(config, name) for name in StrategyAConfig.model_fields}
    return StrategyAConfig.model_validate(values, strict=True)


def _request_for(
    context: StrategyContext,
    config: StrategyAConfig,
    candidates: tuple[StrategyACandidateEnvelope, ...],
) -> LLMDecisionRequest:
    values = {
        "schema_version": "llm-decision-request/v1",
        "strategy_id": STRATEGY_A_ID,
        "config_version": config.config_version,
        "market": context.market_snapshot.market,
        "as_of": context.market_snapshot.as_of,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "model_identity_policy_id": config.model_identity_policy_id,
        "prompt_template_id": config.prompt_template_id,
        "prompt_template_digest": config.prompt_template_digest,
        "candidates": candidates,
    }
    provisional = LLMDecisionRequest.model_construct(
        **values,
        request_fingerprint="llm-decision-request-sha256:" + "0" * 64,
    )
    return LLMDecisionRequest(
        **values,
        request_fingerprint=request_fingerprint_for(provisional),
    )


def _exact_record(record: object) -> LLMDecisionRecord:
    if type(record) is not LLMDecisionRecord:
        raise TypeError("provider must return an exact LLMDecisionRecord")
    if set(record.__dict__) != set(LLMDecisionRecord.model_fields):
        raise ValueError("LLMDecisionRecord is polluted")
    values = {name: getattr(record, name) for name in LLMDecisionRecord.model_fields}
    return LLMDecisionRecord.model_validate(values, strict=True)


def _require_record_matches_request(
    record: LLMDecisionRecord,
    request: LLMDecisionRequest,
) -> None:
    if (
        record.request_fingerprint != request.request_fingerprint
        or record.config_version != request.config_version
        or record.model_identity_policy_id != request.model_identity_policy_id
        or record.prompt_template_id != request.prompt_template_id
        or record.prompt_template_digest != request.prompt_template_digest
    ):
        raise ValueError("decision provenance does not match request")
    if tuple(selection.symbol for selection in record.selections) != tuple(
        candidate.symbol for candidate in request.candidates
    ):
        raise ValueError("decision candidates do not match request")
    if any(
        selection.action not in {target.action for target in candidate.action_targets}
        for selection, candidate in zip(record.selections, request.candidates, strict=True)
    ):
        raise ValueError("decision action violates candidate envelope")


def _intents_for(
    record: LLMDecisionRecord,
    request: LLMDecisionRequest,
) -> tuple[StrategyIntent, ...]:
    intents = []
    for selection, candidate in zip(record.selections, request.candidates, strict=True):
        targets = {item.action: item.target_weight for item in candidate.action_targets}
        evidence_ids = tuple(
            sorted(
                {
                    *candidate.evidence_ids,
                    candidate.portfolio_snapshot_id,
                    candidate.candidate_id,
                    record.decision_id,
                }
            )
        )
        intents.append(
            StrategyIntent(
                strategy_id=STRATEGY_A_ID,
                symbol=selection.symbol,
                market=request.market,
                side=selection.action,
                target_weight=targets[selection.action],
                confidence=selection.confidence,
                as_of=request.as_of,
                thesis=selection.thesis,
                invalidation=selection.invalidation,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(intents)


def build_strategy_a_candidates(
    context: StrategyContext,
    config: StrategyAConfig,
) -> tuple[StrategyACandidateEnvelope, ...]:
    if type(context) is not StrategyContext:
        raise TypeError("context must be an exact StrategyContext")
    if type(config) is not StrategyAConfig:
        raise TypeError("config must be an exact StrategyAConfig")
    try:
        with localcontext(_exact_context(context, config)):
            validated_context = StrategyContext.model_validate(context)
            validated_config = StrategyAConfig.model_validate(config)
            return _build_candidates(validated_context, validated_config)
    except DecimalException as error:
        raise ValueError("Strategy A candidate arithmetic failed") from error


def _build_candidates(
    context: StrategyContext,
    config: StrategyAConfig,
) -> tuple[StrategyACandidateEnvelope, ...]:
    if context.strategy_config_version != config.config_version:
        raise ValueError("strategy context and Strategy A config versions do not match")

    bars_by_symbol: defaultdict[str, list[Bar]] = defaultdict(list)
    for item in context.market_snapshot.bars:
        bars_by_symbol[item.symbol].append(item)
    positions = {item.symbol: item for item in context.portfolio.positions}
    portfolio_id = portfolio_snapshot_id_for(context.portfolio)

    return tuple(
        _candidate_for(
            symbol=symbol,
            bars=bars_by_symbol[symbol],
            position=positions.get(symbol),
            context=context,
            config=config,
            portfolio_id=portfolio_id,
        )
        for symbol in sorted(bars_by_symbol)
    )


def _candidate_for(
    *,
    symbol: str,
    bars: list[Bar],
    position: Position | None,
    context: StrategyContext,
    config: StrategyAConfig,
    portfolio_id: str,
) -> StrategyACandidateEnvelope:
    used_bars = bars[-config.required_history :]
    short_sum = sum(
        (item.close for item in used_bars[-config.short_window :]),
        start=Decimal(0),
    )
    long_sum = sum(
        (item.close for item in used_bars[-config.long_window :]),
        start=Decimal(0),
    )
    latest = used_bars[-1]
    prior_volume_bars = used_bars[-(config.volume_window + 1) : -1]
    prior_volume_sum = sum(
        (item.volume for item in prior_volume_bars),
        start=Decimal(0),
    )
    latest_volume = latest.volume

    complete = len(used_bars) >= config.required_history
    trend = _trend_direction(short_sum, long_sum, latest.close, config) if complete else 0
    volume_confirmed = complete and _volume_is_confirmed(
        latest_volume,
        prior_volume_sum,
        config,
    )
    regime, quality, reasons = _regime(trend, volume_confirmed, complete)

    market_value = position.market_value if position is not None else Decimal(0)
    current_weight = _current_weight(market_value, context.portfolio.nav)
    action_targets = _action_targets(
        regime=regime,
        held=position is not None,
        market_value=market_value,
        nav=context.portfolio.nav,
        current_weight=current_weight,
        config=config,
    )

    values = {
        "schema_version": "strategy-a-candidate/v1",
        "strategy_id": STRATEGY_A_ID,
        "config_version": config.config_version,
        "market": context.market_snapshot.market,
        "symbol": symbol,
        "as_of": context.market_snapshot.as_of,
        "decision_phase": DecisionPhase.POST_CLOSE,
        "regime": regime,
        "data_quality": quality,
        "short_window": config.short_window,
        "long_window": config.long_window,
        "volume_window": config.volume_window,
        "volume_confirmation_threshold": config.volume_confirmation_threshold,
        "short_sum": short_sum,
        "long_sum": long_sum,
        "latest_close": latest.close,
        "prior_volume_sum": prior_volume_sum,
        "latest_volume": latest_volume,
        "portfolio_snapshot_id": portfolio_id,
        "action_targets": action_targets,
        "reason_codes": reasons,
        "evidence_ids": tuple(sorted(bar_evidence_id_for(item) for item in used_bars)),
    }
    provisional = StrategyACandidateEnvelope.model_construct(
        **values,
        candidate_id="strategy-a-candidate-sha256:" + "0" * 64,
    )
    return StrategyACandidateEnvelope(
        **values,
        candidate_id=candidate_id_for(provisional),
    )


def _trend_direction(
    short_sum: Decimal,
    long_sum: Decimal,
    latest_close: Decimal,
    config: StrategyAConfig,
) -> int:
    short_cross = short_sum * config.long_window
    long_cross = long_sum * config.short_window
    latest_cross = latest_close * config.long_window
    if short_cross > long_cross and latest_cross > long_sum:
        return 1
    if short_cross < long_cross and latest_cross < long_sum:
        return -1
    return 0


def _volume_is_confirmed(
    latest_volume: Decimal,
    prior_volume_sum: Decimal,
    config: StrategyAConfig,
) -> bool:
    if prior_volume_sum.is_zero():
        return latest_volume > 0
    return (
        latest_volume * config.volume_window
        >= prior_volume_sum * config.volume_confirmation_threshold
    )


def _regime(
    trend: int,
    volume_confirmed: bool,
    complete: bool,
) -> tuple[StrategyARegime, StrategyADataQuality, tuple[str, ...]]:
    if not complete:
        return (
            StrategyARegime.INSUFFICIENT,
            StrategyADataQuality.INSUFFICIENT,
            ("insufficient-history",),
        )
    if trend > 0 and volume_confirmed:
        return (
            StrategyARegime.OFFENSIVE,
            StrategyADataQuality.COMPLETE,
            ("positive-trend", "volume-confirmed"),
        )
    if trend < 0:
        return (
            StrategyARegime.DEFENSIVE,
            StrategyADataQuality.COMPLETE,
            ("negative-trend",),
        )
    if trend > 0:
        reasons = ("positive-trend", "volume-unconfirmed")
    else:
        reasons = ("mixed-or-equal-trend",)
    return StrategyARegime.NEUTRAL, StrategyADataQuality.COMPLETE, reasons


def _action_targets(
    *,
    regime: StrategyARegime,
    held: bool,
    market_value: Decimal,
    nav: Decimal,
    current_weight: Decimal,
    config: StrategyAConfig,
) -> tuple[StrategyAActionTarget, ...]:
    if regime is StrategyARegime.INSUFFICIENT:
        return (_target(Side.HOLD, current_weight),)
    if regime is StrategyARegime.DEFENSIVE:
        return (_target(Side.SELL, Decimal(0)),) if held else (_target(Side.HOLD, Decimal(0)),)
    if nav.is_zero():
        return (_target(Side.HOLD, Decimal(0)),)

    cap = (
        config.offensive_target_weight
        if regime is StrategyARegime.OFFENSIVE
        else config.neutral_target_weight
    )
    direction = _compare_weight_to_cap(market_value, nav, cap)
    if regime is StrategyARegime.OFFENSIVE:
        if direction < 0:
            return (_target(Side.BUY, cap), _target(Side.HOLD, current_weight))
        if direction == 0:
            return (_target(Side.HOLD, current_weight),)
        return (_target(Side.REDUCE, cap),)

    if direction <= 0:
        return (_target(Side.HOLD, current_weight),)
    if cap.is_zero():
        return (_target(Side.SELL, Decimal(0)),)
    return (_target(Side.REDUCE, cap), _target(Side.SELL, Decimal(0)))


def _target(side: Side, weight: Decimal) -> StrategyAActionTarget:
    return StrategyAActionTarget(action=side, target_weight=weight)


def _compare_weight_to_cap(
    market_value: Decimal,
    nav: Decimal,
    cap: Decimal,
) -> int:
    cap_value = nav * cap
    return (market_value > cap_value) - (market_value < cap_value)


def _current_weight(market_value: Decimal, nav: Decimal) -> Decimal:
    if nav.is_zero():
        return Decimal(0)
    return _weight_context(market_value, nav).divide(market_value, nav)


def _exact_context(context: StrategyContext, config: StrategyAConfig) -> Context:
    values = (
        context.portfolio.cash,
        context.portfolio.nav,
        context.portfolio.peak_nav,
        *(position.market_value for position in context.portfolio.positions),
        *(item.close for item in context.market_snapshot.bars),
        *(item.volume for item in context.market_snapshot.bars),
        config.volume_confirmation_threshold,
        config.offensive_target_weight,
        config.neutral_target_weight,
    )
    max_digits = max(len(value.as_tuple().digits) for value in values)
    product_digits = max_digits * 2 + len(str(config.required_history)) + 4
    return Context(
        prec=min(MAX_PREC, max(50, product_digits)),
        rounding=ROUND_HALF_EVEN,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )


def _weight_context(market_value: Decimal, nav: Decimal) -> Context:
    precision = min(
        MAX_PREC,
        max(50, len(market_value.as_tuple().digits) + len(nav.as_tuple().digits) + 2),
    )
    context = Context(
        prec=precision,
        rounding=ROUND_FLOOR,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
    )
    for signal in (Clamped, FloatOperation, Inexact, Rounded, Subnormal, Underflow):
        context.traps[signal] = False
    return context

