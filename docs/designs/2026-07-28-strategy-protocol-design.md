# Strategy Protocol and Deterministic Fixture Design

Date: 2026-07-28
Status: approved continuation of the Phase 1 trading-core design

## 1. Purpose

Task 9 proves the minimum bars-only boundary between a Phase 1 fixture and the execution core. A strategy may inspect only an immutable point-in-time market view, its own immutable portfolio snapshot, and the selected configuration version. It emits immutable `StrategyIntent` values. It cannot query the store, call a broker, book fills, write ledger events, or mutate account state through this interface.

This is version 1 of the adapter boundary, not a claim that the future A/B/C/D/F adapters can already operate on bars alone. Phase 2 must evolve it with versioned universe and PIT research snapshots; Phase 3 must add model and prompt provenance without weakening this isolation boundary.

The moving-average implementation is an integration fixture, not strategy A and not an investment result.

## 2. Public Surface

`stock_agent.strategies` exports exactly:

- `MarketSnapshot`
- `StrategyContext`
- `Strategy`
- `MovingAverageFixtureStrategy`

`Strategy` is a runtime-checkable structural protocol:

```python
class Strategy(Protocol):
    strategy_id: str
    config_version: str

    def evaluate(self, context: StrategyContext) -> tuple[StrategyIntent, ...]: ...
```

The tuple return type is intentional: adapter output order is deterministic strategy priority and feeds `RiskEngine.evaluate_many` unchanged.

## 3. MarketSnapshot

`MarketSnapshot` is a frozen, extra-forbidden, strict Pydantic model with:

- `as_of`: timezone-aware datetime
- `market`: `Market`
- `bars`: exact tuple of exact `Bar` instances

Invariants:

- every bar belongs to `market`;
- every `bar.available_at <= as_of`;
- `(symbol, session_date)` pairs are unique, representing the latest PIT revision selected upstream;
- bars are already sorted by `(symbol, session_date, available_at)` so evaluation and evidence order cannot depend on insertion order;
- nested bars and their tuple remain immutable.

The snapshot carries data, not a store handle. No strategy can ask it for a future row.

## 4. StrategyContext

`StrategyContext` is a frozen, extra-forbidden, strict Pydantic model with:

- `market_snapshot`: exact `MarketSnapshot`
- `portfolio`: exact `PortfolioSnapshot`
- `strategy_config_version`: nonblank exact string

Invariants:

- portfolio market equals snapshot market;
- portfolio `as_of == market_snapshot.as_of` so weights and signals share one valuation instant;
- context exposes no point-in-time store, execution simulator, order, fill, ledger, network client, callback, or mutable service.

A strategy rejects a context whose selected config version differs from its own `config_version`. This prevents an audit record from claiming one configuration while executing another.

## 5. Fixture Strategy

`MovingAverageFixtureStrategy` is immutable and has stable identity:

- `strategy_id = "fixture-moving-average"`
- `config_version = "1"`
- short window = 2 sessions
- long window = 3 sessions
- BUY target = 10%

For each symbol in lexical order, bars are evaluated chronologically using only rows in the supplied snapshot:

- fewer than three bars: emit `HOLD` at the current portfolio weight (zero if not held);
- short average above long average: target 10%; emit `BUY` when current weight is below 10%, `HOLD` when equal, and `REDUCE` when above;
- short average below long average: emit `SELL` at zero when held, otherwise `HOLD` at zero;
- equal averages: emit `HOLD` at current portfolio weight.

The fixture emits one intent per symbol represented in the snapshot. Intent `as_of` equals snapshot `as_of`. Intent content depends only on the immutable context and fixed strategy constants. Evidence IDs depend only on bars actually used: the last three chronological bars when enough history exists, otherwise every available bar. Their format is `bar-sha256:<64 lowercase hex characters>`. The digest input is a UTF-8 JSON array with fixed field order `[market, symbol, session_date, open, high, low, close, volume, available_at]`, `ensure_ascii=False`, and compact separators. Dates use ISO `YYYY-MM-DD`; UTC datetimes use six fractional digits and a trailing `Z`. A finite Decimal is `"0"` when numerically zero; otherwise it is `"<sign><coefficient>e<exponent>"`, where trailing coefficient zeros are removed while increasing the base-10 exponent, for example `1.20 -> "12e-1"` and `100 -> "1e2"`. This canonical encoding makes numerically equal values stable and makes two revisions with the same availability timestamp but different content distinguishable. No database identity, current time, environment state, or ambient Decimal context is consulted.

For each symbol, only the final two and three chronological closes form the short and long windows; older rows do not enter the means. Current weight is `market_value / nav` in a private deterministic Decimal context, with zero weight when NAV is zero.

Decimal arithmetic runs in a private context and does not depend on or alter the ambient Decimal context. Inputs are never mutated, and repeated or concurrent calls return byte-identical model dumps.

## 6. Safety and Scope

- Invalid boundary construction raises validation errors.
- Invalid `evaluate` argument types or config-version mismatch raise `TypeError`/`ValueError` before strategy arithmetic.
- The protocol has no execution or ledger capability; Python cannot stop arbitrary malicious code from importing other modules, but normal adapters receive no authority through this boundary.
- Task 9 does not read DuckDB, place orders, run risk, execute fills, update a ledger, calculate performance, or implement A/B/C/D/F.
- Task 10 owns chronological snapshot construction and passes each returned tuple directly to atomic risk evaluation. After risk, it converts target decisions to orders by delta from current weight: HOLD and zero delta create no order, REDUCE becomes SELL, BUY requires a positive delta, SELL targets zero, and an inconsistent side/delta pair is rejected rather than silently reversed.

## 7. Verification

Tests cover strict construction, PIT rejection, deterministic ordering, no-look-ahead, nested immutability, protocol runtime conformance, stable identity/version, BUY/SELL/HOLD/insufficient-history behavior, evidence provenance, hostile Decimal contexts, concurrency, no input mutation, no service capabilities, exact exports, and absence of Task 10 side effects.
