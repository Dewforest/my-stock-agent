# Shared Portfolio Risk Controls Design

Date: 2026-07-28
Status: Approved design for Phase 1 Task 8

## 1. Objective

Implement a deterministic public risk layer that clamps or rejects strategy BUY intents using only the approved hard portfolio limits. The layer does not judge investment quality, mutate portfolios, invent trades, or keep hidden day-level state.

Approved limits:

- maximum target weight per stock: 15%
- maximum exposure per sector: 30%
- maximum concurrent holdings per market: 10
- cumulative new-position commitments per day: 30% of day-start available cash
- drawdown of 15% or more: block BUY intents, permit SELL intents
- drawdown of 20% or more: target one-half of current gross exposure and require review
- a 100% cash portfolio is valid

## 2. Architectural Choice

Use a stateless pure-function engine:

```python
RiskEngine.evaluate(intent: StrategyIntent, context: RiskContext) -> RiskDecision
```

`RiskContext` contains every fact required for a decision. The same intent and context must always produce the same result.

Rejected alternatives:

1. Stateful engine that accumulates daily usage internally. It makes rollback, reversal, parallel strategy evaluation, and replay dependent on call order and hidden mutable state.
2. Separate policy graph and portfolio projector. It is extensible but unnecessary for the fixed Phase 1 rules.

## 3. Public Models

All public risk models are frozen Pydantic models with `extra="forbid"` and strict finite `Decimal` fields.

### 3.1 `RiskContext`

Fields:

- `portfolio: PortfolioSnapshot`
- `instruments: tuple[Instrument, ...]`
- `day_start_available_cash: Decimal`
- `new_position_notional_committed_today: Decimal`

Rules:

- instrument symbols are unique
- every instrument belongs to the portfolio market
- cash and committed notional are nonnegative, finite Decimals
- committed notional may exceed the configured budget; that represents a fully consumed budget, not an invalid context
- instrument metadata may be incomplete so SELL can still pass, but a BUY is rejected if sector exposure cannot be calculated without guessing

The daily commitment field includes both filled and still-reserved opening-position notional. The caller updates or releases that reservation as orders fill, reject, or cancel. The risk engine never mutates it.

### 3.2 `RiskDecisionStatus`

Values:

- `APPROVED`
- `CLAMPED`
- `REJECTED`

### 3.3 `RiskReductionTarget`

Fields:

- `current_gross_exposure: Decimal`
- `target_gross_exposure: Decimal`
- `review_required: bool = True`

At drawdown of 20% or more:

```python
target_gross_exposure = current_gross_exposure / 2
```

This directive is portfolio-level metadata. It does not synthesize a BUY, SELL, HOLD, or REDUCE intent.

### 3.4 `RiskDecision`

Fields:

- `original_intent: StrategyIntent`
- `status: RiskDecisionStatus`
- `approved_target_weight: Decimal | None`
- `rule_ids: tuple[str, ...]`
- `reasons: tuple[str, ...]`
- `risk_reduction: RiskReductionTarget | None`

Invariants:

- `REJECTED` has no approved target weight
- `APPROVED` and `CLAMPED` have an approved target weight
- the original intent is preserved unchanged
- no field changes the intent side
- `rule_ids` are unique and ordered by evaluation precedence
- every applied rule has a human-readable reason

## 4. Evaluation Semantics

### 4.1 Common calculations

All arithmetic uses a private high-precision Decimal context and is independent of ambient Decimal precision, rounding, exponent limits, or traps.

```python
drawdown = (peak_nav - nav) / peak_nav
current_gross_exposure = sum(position.market_value) / nav
current_symbol_weight = position.market_value / nav
```

If `peak_nav == 0`, drawdown is zero. If `nav == 0`, gross exposure is zero and a BUY is rejected because a target notional cannot be derived.

Thresholds are inclusive.

### 4.2 Market isolation

The intent market must match the portfolio market. Mismatch is rejected. Risk is evaluated independently per account and market.

### 4.3 Drawdown precedence

1. At drawdown of 20% or more, attach `RiskReductionTarget(current, current / 2)` and the `DRAWDOWN_RISK_REDUCTION` rule.
2. At drawdown of 15% or more, reject every BUY with `DRAWDOWN_BUY_BLOCK`.
3. SELL remains approved with target weight zero and retains any 20% risk-reduction directive.
4. HOLD and REDUCE are not converted to another side and retain their submitted target weight; a 20% directive may still be attached.

The risk-reduction directive tells the later orchestration layer what portfolio exposure is required. Task 8 does not invent symbol-level reduction trades.

### 4.4 BUY limit evaluation

Only BUY can create or increase exposure, so the four exposure limits below apply to BUY intents. SELL, HOLD, and REDUCE are not converted into BUY-like behavior.

Evaluation order after drawdown checks:

1. validate required instrument and sector metadata
2. reject a new symbol if 10 holdings already exist
3. clamp target to the 15% single-stock ceiling
4. clamp target to remaining 30% sector capacity
5. for a symbol not currently held, clamp target to the remaining daily new-position cash budget

The final approved target is the minimum positive target permitted by all applicable limits.

If the result is zero or negative, reject the BUY. Do not change it into HOLD.

If the result equals the submitted target, status is `APPROVED`. If it is positive but smaller, status is `CLAMPED`.

### 4.5 Sector capacity

Sector names are compared after stripping and Unicode case-folding.

For the intent symbol, its approved target replaces its current portfolio weight. Therefore:

```python
other_sector_weight = sum(
    current weights of same-sector positions excluding the intent symbol
)
sector_target_cap = max(0, 0.30 - other_sector_weight)
```

Every current holding and the intent symbol must have instrument metadata before a BUY is approved. Missing data rejects the BUY; the engine never guesses a sector.

### 4.6 Holding count

The 10-holding limit applies only when the intent symbol is not currently held. Increasing an existing holding does not consume a new holding slot.

### 4.7 Daily new-position budget

The budget applies only when opening a symbol that is not currently held.

```python
daily_budget = day_start_available_cash * 0.30
remaining_budget = max(0, daily_budget - new_position_notional_committed_today)
daily_target_cap = remaining_budget / portfolio.nav
```

The denominator is day-start available cash, not a shrinking current-cash value. This keeps sequential decisions deterministic. Existing-position increases do not count as new-position commitments in Phase 1.

## 4.5 Atomic Batch Evaluation

`RiskEngine.evaluate_many(intents, context)` is the authoritative interface when a
session produces more than one intent before orders execute. It evaluates an exact
tuple of unique-symbol intents in caller-supplied priority order and returns an
aligned tuple of decisions.

The method keeps a private, invocation-local projection of approved BUY targets:

- approved new symbols reserve a holding slot;
- approved targets replace that symbol's projected portfolio weight for later
  sector checks;
- approved new-symbol target notionals consume the remaining daily opening budget;
- rejected intents and non-BUY intents do not release or reserve projected capacity.

This is deliberately conservative: an unexecuted SELL or REDUCE cannot finance or
make room for a later BUY because the future exit may fail. Duplicate symbols are
invalid batch input rather than two orders against one target. The supplied order is
the explicit strategy-priority order and therefore part of deterministic replay.

`evaluate(intent, context)` remains the single-intent convenience API and is
equivalent to evaluating a one-item batch. The engine stores no projection between
calls. Task 10 must gather one market/session's intents and call `evaluate_many`
once; repeated separate `evaluate` calls intentionally do not share reservations.

## 5. Rule IDs

The implementation exposes stable string rule IDs:

- `MARKET_MISMATCH`
- `MISSING_INSTRUMENT_METADATA`
- `ZERO_NAV_BUY_BLOCK`
- `SINGLE_STOCK_MAX_15`
- `SECTOR_MAX_30`
- `HOLDING_COUNT_MAX_10`
- `DAILY_NEW_POSITION_CASH_MAX_30`
- `DRAWDOWN_BUY_BLOCK_15`
- `DRAWDOWN_RISK_REDUCTION_20`

A decision records only rules that affected or blocked that decision, plus the 20% portfolio directive when applicable.

## 6. Error and Safety Boundaries

- Invalid model construction raises Pydantic validation errors.
- A valid context with insufficient capacity returns a structured rejection rather than raising.
- The engine does not mutate the intent, context, portfolio, instruments, or tuples.
- The engine does not inspect theses, confidence, evidence, valuation, strategy identity, or investment quality.
- The engine does not calculate order quantities, prices, fees, lots, or executions.
- The execution simulator and immutable ledger remain the authorities for tradability, no-short, cash, and settlement constraints.

## 7. Test Strategy

Use vertical RED→GREEN TDD slices:

1. import contract and a valid 100% cash portfolio
2. 15% single-stock clamp
3. 30% sector capacity, including replacement of an existing symbol weight
4. 10-holding rejection and existing-holding exemption
5. cumulative daily opening-position budget and existing-holding exemption
6. 15% drawdown BUY rejection with SELL approval
7. 20% gross-exposure-halving directive and review flag
8. simultaneous binding limits choose the smallest positive target and preserve ordered rule IDs
9. missing metadata, market mismatch, zero NAV, strict Decimal, hostile ambient context
10. immutability, no side invention, deterministic replay, exact public exports

Focused, full-suite, Ruff, and diff checks must pass without warnings before completion.

## 8. Out of Scope

- strategy-specific stop losses or thesis invalidation
- alpha, valuation, confidence, or evidence scoring
- sector taxonomy inference
- volatility, beta, VaR, leverage, margin, or derivatives
- automatic symbol-level liquidation at 20% drawdown
- persistent daily usage storage
- execution sizing and order generation
