# Strategy Protocol Implementation Plan

> Execute in the existing Phase 1 worktree with strict RED-GREEN-REFACTOR discipline. The approved design is `docs/designs/2026-07-28-strategy-protocol-design.md`.

**Goal:** Add a minimal immutable strategy boundary and deterministic moving-average fixture without granting strategies access to data stores, execution, or ledger mutation.

**Architecture:** Put strict snapshot/context contracts in `strategies/protocol.py`, the structural protocol beside them, and fixture behavior in `strategies/fixture.py`. Reuse domain `Bar`, `PortfolioSnapshot`, and `StrategyIntent`; do not duplicate them.

---

### Task 1: Define immutable strategy-boundary contracts

**Files:**
- Create: `src/stock_agent/strategies/protocol.py`
- Create: `src/stock_agent/strategies/__init__.py`
- Create: `tests/strategies/test_protocol.py`

**RED tests:**

- exact public exports;
- valid empty and populated `MarketSnapshot`;
- reject naive `as_of`, wrong market, future `available_at`, duplicate symbol/session, unstable bar order, list/tuple subclass, nested `Bar` subclass, extra fields;
- valid `StrategyContext` and rejection of market mismatch, any unequal portfolio/snapshot timestamp, blank version, nested subclasses, extra fields;
- frozen top-level and transitively immutable nested tuples/models;
- runtime protocol accepts a conforming adapter and rejects an object missing required behavior;
- context has no store/execution/order/fill/ledger/network/callback fields.

**Implementation:**

Use strict frozen Pydantic models and after validators. Use a runtime-checkable `Protocol` whose output is an exact tuple contract. Keep package exports exactly the four approved names.

**Verification:**

Run focused tests, Ruff, and diff check. Commit:

`feat: define isolated strategy protocol`

---

### Task 2: Implement the deterministic moving-average fixture

**Files:**
- Create: `src/stock_agent/strategies/fixture.py`
- Extend: `tests/strategies/test_protocol.py`

**RED tests:**

- stable identity and config version;
- exact context/config validation;
- fewer than three bars emits HOLD at current weight;
- rising 2/3 moving-average state emits BUY below 10%, HOLD at 10%, and REDUCE above 10%;
- falling state emits SELL only for a held symbol and HOLD for an unheld symbol;
- equal averages emit HOLD at current weight;
- zero NAV defines current weight as zero, including when a zero-market-value position exists;
- one intent per represented symbol in lexical order;
- intent content depends only on immutable context and fixed strategy constants; evidence IDs depend only on bars actually used;
- evidence IDs exactly match the specified canonical JSON/SHA-256 format, and same-`available_at` revisions with different OHLCV produce different IDs;
- a bar unavailable at `as_of` is rejected at the snapshot boundary and cannot affect output;
- an early legal PIT snapshot and a later legal revised snapshot produce their respective evidence and decision without leakage;
- histories longer than three rows use only the final 2/3 windows;
- repeated calls, hostile Decimal ambient contexts, and concurrent calls are byte-identical;
- context, portfolio, bars, strategy, and ambient Decimal context are unchanged;
- fixture exposes no method for orders, fills, execution, or ledger writes.

**Implementation:**

Use immutable constants/configuration. Group already sorted bars by symbol. Compare the final short and long windows in a private deterministic Decimal context; do not use float or ambient arithmetic. Compute current weight as `market_value / nav` in that context and use zero for zero NAV. Return an exact tuple of validated domain `StrategyIntent` models.

**Verification:**

Run focused, full strategy, full repository, Ruff, diff check, and independent hand-calculated trend probes. Commit:

`feat: add deterministic fixture strategy`

---

### Task 3: Final Task 9 review

Run independent specification review against the approved design, followed by code-quality review. Verify the exact public surface, no authority leakage, deterministic replay, and no Task 10 implementation. Run full tests and Ruff. Fix any Critical/Important findings before marking Task 9 complete.
