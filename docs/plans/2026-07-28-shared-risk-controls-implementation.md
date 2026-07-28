# Shared Portfolio Risk Controls Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a deterministic, stateless risk engine that clamps or rejects BUY intents under the approved portfolio limits and emits a portfolio exposure-halving directive at 20% drawdown.

**Architecture:** Add a self-contained `stock_agent.risk` package. Frozen Pydantic models carry all risk inputs and outputs; `RiskEngine.evaluate()` is a pure function using a private Decimal context. The engine reads existing domain models but never mutates portfolios, tracks hidden daily state, sizes orders, or evaluates investment quality.

**Tech Stack:** Python 3.11+, Pydantic 2, `Decimal`, pytest, Ruff.

**Design:** `docs/designs/2026-07-28-shared-risk-controls-design.md`

---

### Task 1: Define the immutable risk contract

**Objective:** Create strict public input/output models before implementing decisions.

**Files:**
- Create: `src/stock_agent/risk/__init__.py`
- Create: `src/stock_agent/risk/engine.py`
- Create: `tests/risk/test_models.py`

**Step 1: Write the first failing import test**

```python
from stock_agent.risk import (
    RiskContext,
    RiskDecision,
    RiskDecisionStatus,
    RiskEngine,
    RiskReductionTarget,
)


def test_risk_public_contract_is_importable() -> None:
    assert RiskDecisionStatus.APPROVED == "APPROVED"
```

**Step 2: Run the focused test and verify RED**

Run:

```bash
uv run pytest tests/risk/test_models.py::test_risk_public_contract_is_importable -v
```

Expected: FAIL because `stock_agent.risk` does not exist.

**Step 3: Implement the minimum public types**

In `engine.py`, define:

```python
class RiskDecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    CLAMPED = "CLAMPED"
    REJECTED = "REJECTED"


class RiskContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    portfolio: PortfolioSnapshot
    instruments: tuple[Instrument, ...]
    day_start_available_cash: StrictRiskDecimal
    new_position_notional_committed_today: StrictRiskDecimal


class RiskReductionTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    current_gross_exposure: StrictUnitRiskDecimal
    target_gross_exposure: StrictUnitRiskDecimal
    review_required: Literal[True] = True


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    original_intent: StrategyIntent
    status: RiskDecisionStatus
    approved_target_weight: StrictUnitRiskDecimal | None
    rule_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    risk_reduction: RiskReductionTarget | None = None
```

Add validators:

- strict finite Decimal inputs; reject int, float, string, NaN, and infinity
- nonnegative cash/commitment and unit interval weights/exposures
- unique instrument symbols; instrument markets match portfolio market
- `REJECTED` requires `approved_target_weight is None`
- `APPROVED`/`CLAMPED` require a target
- rule IDs are nonblank and unique
- reasons are nonblank
- applied rules and reasons have equal lengths
- all models are frozen and forbid extra fields

Expose exactly these five names from `risk.__init__`.

`RiskEngine` may initially be an empty class so the import contract is complete; no evaluation behavior yet.

**Step 4: Add model boundary tests one vertical slice at a time**

Cover:

- valid 100% cash context
- duplicate instruments
- cross-market instruments
- strict/nonfinite Decimal rejection under hostile ambient Decimal context
- decision status/target invariants
- frozen models and forbidden extra fields
- exact `__all__`

For each behavior, add one test, observe RED, implement the smallest validator, then observe GREEN.

**Step 5: Verify Task 1**

```bash
uv run pytest tests/risk/test_models.py -v
uv run ruff check src/stock_agent/risk tests/risk
```

Expected: all focused tests pass without warnings.

**Step 6: Commit**

```bash
git add src/stock_agent/risk tests/risk/test_models.py
git commit -m "feat: define immutable risk decision models"
```

---

### Task 2: Approve safe non-BUY intents and emit drawdown directives

**Objective:** Implement market isolation, 100% cash behavior, non-BUY pass-through, and the 15%/20% drawdown rules.

**Files:**
- Modify: `src/stock_agent/risk/engine.py`
- Create: `tests/risk/test_drawdown.py`

**Step 1: Write a failing 100% cash test**

```python
def test_sell_is_approved_in_a_full_cash_portfolio() -> None:
    decision = RiskEngine().evaluate(sell_intent(), full_cash_context())
    assert decision.status is RiskDecisionStatus.APPROVED
    assert decision.approved_target_weight == Decimal("0")
    assert decision.original_intent.side is Side.SELL
```

Run the test and confirm RED because `evaluate` is missing.

**Step 2: Implement minimum pure evaluation**

Add stable private rule constants:

```python
_MARKET_MISMATCH = "MARKET_MISMATCH"
_ZERO_NAV_BUY_BLOCK = "ZERO_NAV_BUY_BLOCK"
_DRAWDOWN_BUY_BLOCK = "DRAWDOWN_BUY_BLOCK_15"
_DRAWDOWN_REDUCTION = "DRAWDOWN_RISK_REDUCTION_20"
```

Implement `RiskEngine.evaluate(intent, context)` with strict `StrategyIntent`/`RiskContext` instance checks. For SELL, HOLD, and REDUCE, preserve the original target and side. Reject market mismatch structurally.

**Step 3: Add and implement the 15% drawdown slice**

Test inclusive threshold behavior:

- `drawdown == 0.15`: BUY rejected
- `drawdown > 0.15`: BUY rejected
- just below 0.15: BUY continues to later limits
- SELL remains approved

Use a private `Context(prec=128, ROUND_HALF_EVEN, ...)`; never use ambient Decimal arithmetic.

**Step 4: Add and implement the 20% directive slice**

Test a portfolio with 60% gross exposure and exactly 20% drawdown:

```python
assert decision.risk_reduction.current_gross_exposure == Decimal("0.6")
assert decision.risk_reduction.target_gross_exposure == Decimal("0.3")
assert decision.risk_reduction.review_required is True
assert "DRAWDOWN_RISK_REDUCTION_20" in decision.rule_ids
```

Requirements:

- BUY is rejected and carries both drawdown rules in precedence order
- SELL remains SELL and approved target zero while carrying the reduction directive
- HOLD/REDUCE retain their side and submitted target
- no positions means gross exposure zero and target zero
- `peak_nav == 0` produces zero drawdown without division failure
- the input models remain unchanged

**Step 5: Verify Task 2**

```bash
uv run pytest tests/risk/test_drawdown.py -v
uv run pytest tests/risk -q
uv run ruff check src/stock_agent/risk tests/risk
```

Expected: all focused and risk tests pass.

**Step 6: Commit**

```bash
git add src/stock_agent/risk/engine.py tests/risk/test_drawdown.py
git commit -m "feat: enforce portfolio drawdown controls"
```

---

### Task 3: Clamp BUY targets by stock, sector, and holding count

**Objective:** Enforce exposure ceilings without changing the intent side or guessing metadata.

**Files:**
- Modify: `src/stock_agent/risk/engine.py`
- Create: `tests/risk/test_exposure_limits.py`

**Step 1: Write and run a failing single-stock clamp test**

```python
def test_buy_target_is_clamped_to_fifteen_percent() -> None:
    decision = RiskEngine().evaluate(buy_intent(target="0.40"), context())
    assert decision.status is RiskDecisionStatus.CLAMPED
    assert decision.approved_target_weight == Decimal("0.15")
    assert decision.rule_ids == ("SINGLE_STOCK_MAX_15",)
```

Expected RED: current BUY path has no exposure evaluation.

Implement the 15% cap and preserve the original intent.

**Step 2: Add the sector-capacity slice**

Tests:

- other same-sector holdings at 20% leave a 10% target cap
- existing intent symbol is excluded from `other_sector_weight`, so its target replaces its current weight
- sector names compare by stripped Unicode case-folded value
- unrelated sectors do not consume capacity
- if existing same-sector exposure is already 30% or higher, BUY is rejected
- missing intent or current-holding instrument metadata rejects BUY with `MISSING_INSTRUMENT_METADATA`
- SELL does not require instrument metadata

Use current position market values divided by NAV. Do not infer sector from symbols.

**Step 3: Add the holding-count slice**

Tests:

- opening an 11th symbol is rejected with `HOLDING_COUNT_MAX_10`
- increasing one of 10 existing holdings is allowed through to the other caps
- zero target after caps is rejected, never converted to HOLD

**Step 4: Add simultaneous-limit composition**

Given an original 40% target, a 15% stock cap, and 10% sector room:

- approved target is 10%
- both binding rule IDs are present in evaluation order
- reasons are aligned with rule IDs
- status is `CLAMPED`

Do not include nonbinding rule IDs.

**Step 5: Verify Task 3**

```bash
uv run pytest tests/risk/test_exposure_limits.py -v
uv run pytest tests/risk -q
uv run ruff check src/stock_agent/risk tests/risk
```

Expected: all tests pass.

**Step 6: Commit**

```bash
git add src/stock_agent/risk/engine.py tests/risk/test_exposure_limits.py
git commit -m "feat: clamp portfolio exposure limits"
```

---

### Task 4: Enforce the cumulative daily opening-position budget

**Objective:** Limit new symbols to 30% of day-start available cash while exempting existing holdings.

**Files:**
- Modify: `src/stock_agent/risk/engine.py`
- Create: `tests/risk/test_daily_budget.py`

**Step 1: Write and run the failing budget test**

For day-start cash 1000, committed opening notional 200, and NAV 1000, only 100 remains:

```python
def test_new_symbol_is_clamped_to_remaining_daily_cash_budget() -> None:
    decision = RiskEngine().evaluate(buy_intent(target="0.15"), context(
        day_start_cash="1000",
        committed="200",
        nav="1000",
    ))
    assert decision.approved_target_weight == Decimal("0.10")
    assert "DAILY_NEW_POSITION_CASH_MAX_30" in decision.rule_ids
```

Expected RED: daily budget is not implemented.

**Step 2: Implement the budget calculation**

```python
daily_budget = day_start_available_cash * Decimal("0.30")
remaining = max(Decimal(0), daily_budget - committed)
daily_target_cap = remaining / portfolio.nav
```

Apply only when the symbol is not already held.

Tests:

- cumulative budget clamps a new symbol
- fully consumed/overconsumed budget rejects a new symbol
- existing-position BUY is exempt from this rule
- zero day-start cash rejects opening BUY
- current portfolio cash changing during the day does not change the denominator
- zero NAV BUY rejects cleanly with `ZERO_NAV_BUY_BLOCK`

**Step 3: Add deterministic and hostile-context tests**

Evaluate the same intent/context repeatedly under hostile ambient Decimal precision, rounding, exponent limits, and traps. Assert byte-for-byte equal model dumps and no Decimal exception leakage.

**Step 4: Verify Task 4**

```bash
uv run pytest tests/risk/test_daily_budget.py -v
uv run pytest tests/risk -q
uv run pytest -q
uv run ruff check .
git diff --check
```

Expected: focused, risk, and complete suites pass with no warnings; Ruff and diff check pass.

**Step 5: Commit**

```bash
git add src/stock_agent/risk/engine.py tests/risk/test_daily_budget.py
git commit -m "feat: enforce daily new-position budget"
```

---

### Task 5: Final Task 8 verification and review

**Objective:** Prove the complete public risk contract against the approved design and existing system.

**Files:**
- Review only unless a failing review requires a targeted TDD fix

**Step 1: Run all quality gates separately**

```bash
uv run pytest tests/risk -v
uv run pytest -q
uv run ruff check .
git diff --check
```

Expected: all pass without warnings.

**Step 2: Run independent lifecycle probes**

Verify:

- 100% cash context is accepted
- a 40% BUY can be clamped by stock, sector, and daily limits to the smallest positive target
- the 11th holding is rejected
- 15% drawdown rejects BUY and permits SELL
- 20% drawdown emits half-gross-exposure target and review flag
- no decision mutates or changes the side of the original intent
- missing metadata never causes sector guessing
- identical inputs produce identical outputs under hostile Decimal context

**Step 3: Perform two-stage review**

1. specification compliance review against `docs/designs/2026-07-28-shared-risk-controls-design.md`
2. code quality review focused on financial arithmetic, rule precedence, immutability, and Task 10 composability

Any review finding must first receive a failing regression test, then the smallest fix.

**Step 4: Completion criteria**

Task 8 is complete only when:

- all approved hard-limit tests pass
- full suite and Ruff pass
- both reviews approve
- working tree is clean
- no remote push or merge occurs until the user-requested checkpoint
