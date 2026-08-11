# Chronological Backtest Runner Design

Date: 2026-07-29
Status: approved after architecture review

## 1. Purpose and Boundary

Task 10 creates the first complete deterministic vertical slice:

`session open execution -> immutable ledger -> close PIT snapshot -> strategy -> atomic risk -> target/order planning -> next session open`

A run covers exactly one account, one market, one fixed Phase 1 instrument universe, one explicit calendar, and one strategy. Multi-market orchestration, historical universe membership, durable run storage, parallel execution, and live scheduling remain outside Phase 1.

The runner owns orchestration only. Point-in-time data selection, strategy logic, hard risk, market execution rules, and accounting remain their existing authorities.

## 2. Ledger Events for Atomic Valuation

### 2.1 Shared immutable items

Add exact frozen models:

```python
PositionMark(symbol: str, price: Decimal)
BookedFill(
    fill_id: str,
    symbol: str,
    side: Side,       # BUY or SELL only
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
)
```

Symbols are normalized by the existing domain convention. Numeric fields use the ledger's supported Decimal boundary. Subclasses, mutable collections, duplicate symbols where uniqueness is required, and model-copy corruption are rejected.

### 2.2 Atomic open booking

Add one ledger event:

```python
OpenExecutionBatchBooked(
    event_id,
    account_id,
    market,
    occurred_at,
    session_date,
    fills: tuple[BookedFill, ...],
    marks: tuple[PositionMark, ...],
)
```

The event applies all FILLED executions sequentially in tuple order for cash, FIFO lots, fees, and realized P&L. It then requires `marks` to equal the complete resulting held-symbol set, applies every open price, and computes positions, NAV, and peak exactly once. Thus no intermediate mixture of previous-close and current-open marks can create a false peak. `fills` is nonempty, exact, and has unique `fill_id`; `marks` is exact, unique, and symbol-sorted.

The runner emits at most one such event per session, at exact `open_at`. Rejected executions remain runner audit data and never enter the ledger. Existing `BuyFilled` and `SellFilled` remain backward compatible for non-runner callers.

### 2.3 Atomic close valuation

Add:

```python
PortfolioMarked(
    event_id,
    account_id,
    market,
    occurred_at,
    session_date,
    marks: tuple[PositionMark, ...],
)
```

`marks` must be an exact symbol-sorted tuple whose symbol set equals the complete currently held set. Empty marks are valid only for an empty portfolio. Replay applies all marks first and computes NAV/peak once. This event advances an all-cash ledger to the close and records one auditable session-end snapshot.

Existing `PositionMarked` remains backward compatible; the runner never uses it. Both new events join `LedgerEvent`, public exports, runtime checks, replay, snapshot replay, and reversal handling.

### 2.4 Reversal dependency semantics and atomic correction append

Both batch events are ordinary reversible events. Reversal always replays the complete active stream. If reversing an earlier fill/batch changes the held-symbol set expected by a later complete mark, a lone reversal fails atomically. A correction must reverse every affected downstream complete-mark or execution-batch event in one candidate stream, then append replacement complete events. `OpenExecutionBatchBooked` is the runner's correction unit; an individual booked fill inside it is not independently reversible.

Add:

```python
PortfolioLedger.append_many(events: tuple[LedgerEvent, ...]) -> None
```

It accepts an exact tuple of exact ledger events, validates account/market, IDs, initialization, and strictly increasing UTC instants across the existing and candidate streams, then computes active events, replays, and commits the complete candidate stream once. Empty input is a no-op; any failure leaves all ledger state unchanged. `append(event)` delegates to the one-item path. This API exists for atomic dependent corrections and general batch commit; it does not define valuation grouping. Atomic valuation remains solely the responsibility of `OpenExecutionBatchBooked` and `PortfolioMarked`.

## 3. Cash-Aware Next-Open Execution

Extend `ExecutionSimulator.process_session` with optional exact cash state:

```python
available_cash_by_account: Mapping[str, Decimal] | None = None
```

When supplied, validation is atomic and every eligible pending account must have an entry. In deterministic pending-order order:

- FILLED SELL net proceeds become available to later orders in the same open batch;
- BUY cost is `quantity * open + fees`;
- a BUY exceeding remaining cash becomes terminal REJECTED with reason `insufficient available cash`;
- a SELL whose fees exceed proceeds becomes terminal REJECTED with reason `fees exceed sell proceeds`;
- only successful fills update simulated cash;
- existing lot, T+1, lot-size, suspension, and price-limit checks remain authoritative.

This is normal execution rejection, not a ledger failure. The ledger repeats all cash invariants when booking the successful batch. Without the optional mapping, existing simulator behavior remains backward compatible.

## 4. Runner Input Contracts

`BacktestSession` is frozen and contains:

- plain `session_date`;
- aware `open_at` and `close_at`, with `open_at < close_at`;
- exact, symbol-sorted `open_bars`;
- exact, symbol-sorted `cn_session_states`.

An open bar is execution-only data. It must match market/session, satisfy `available_at <= open_at`, have `open == high == low == close`, and volume zero. This prevents close information from entering open execution. For the fixed universe, every session has exactly one open bar per instrument. CN sessions also have exactly one state per instrument; US sessions have none. Every eligible order therefore terminates at its immediately following open rather than drifting across sessions.

`BacktestSpec` is frozen and contains:

- nonblank `run_id` and `account_id`;
- exact `market`;
- supported nonnegative `initial_cash`;
- exact symbol-sorted, unique `instruments`;
- exact chronological `sessions`;
- nonblank `strategy_config_version`.

Every instrument and session belongs to the spec market. Session dates exactly match a contiguous slice of the runner's explicit `TradingCalendar`; timestamps are strictly increasing across sessions. At least two sessions are required.

`ChronologicalBacktestRunner` receives a `PointInTimeStore`, matching `TradingCalendar`, and optional transaction-cost configuration. It creates a fresh ledger and simulator per uncached run.

Before the session loop it appends deterministic `CashInitialized`:

- `occurred_at = first.open_at - 1 microsecond`;
- stable event ID from canonical `(run_id, account_id, market, "cash-initialized")`;
- amount equals `initial_cash`.

Spec validation rejects a first open for which that timestamp cannot be represented.

## 5. PIT Selection and Revision Audit

Add immutable `SelectedBarRevision` and store query:

```python
PointInTimeStore.latest_bar_revision_as_of(...) -> SelectedBarRevision | None
```

It exposes the selected `Bar` plus `ingested_at`, `source`, and `source_record_id`. Existing `latest_bar_as_of` delegates to it and returns only `.bar`.

At each close, for every instrument and spec session through the current date, the runner selects the latest revision with `available_at <= close_at`. Selected bars are sorted into `MarketSnapshot`; matching revision metadata is saved in `SessionResult`. A current-session close bar must exist for every instrument before a close ledger event is appended.

Current holdings are marked with current close prices through one `PortfolioMarked`. The resulting `PortfolioSnapshot.as_of` exactly equals `MarketSnapshot.as_of == close_at`.

Later revisions affect later close snapshots only. Completed results are immutable and never recomputed. Phase 1 explicitly records knowledge policy `business-available-at/v1`; strict `ingested_at <= historical_query_time` replay is deferred.

## 6. Session State Machine

For each session in chronological order:

1. Capture cash and held symbols before open processing.
2. Call cash-aware `process_session` with the complete open frame, acquisition lots, and account cash.
3. Convert all FILLED results into one deterministic `OpenExecutionBatchBooked`, including full open marks for every resulting holding; append it. Audit REJECTED results without booking.
4. Compute today's new-position committed notional from FILLED BUYs for symbols absent before open; fees are excluded.
5. Resolve the complete close PIT snapshot and append one `PortfolioMarked` at `close_at`.
6. Save the close portfolio snapshot and NAV.
7. On every nonfinal session, evaluate the strategy with exact matching `StrategyContext`.
8. Validate an exact tuple of exact intents matching strategy ID, market, close instant, fixed-universe symbols, and unique symbols.
9. Build one `RiskContext` with close portfolio, fixed instruments, day-start cash, and today's new-position committed notional.
10. Call `RiskEngine.assess_portfolio(context)` exactly once, even for empty intents.
11. Call `RiskEngine.evaluate_many(intents, same_context)` exactly once in strategy priority order. Any legacy decision-level reduction directive must equal the session directive.
12. Plan targets and submit orders with `decision_date=session_date`.

The final session still executes and books orders eligible at its open, then performs close PIT valuation. Its close emits exact empty intents, risk decisions, plans, and submissions, with no portfolio assessment call.

## 7. Portfolio-Level Drawdown Reduction

Add stateless:

```python
RiskEngine.assess_portfolio(context: RiskContext) -> RiskReductionTarget | None
```

It exposes the existing 20% drawdown gross-exposure directive independently of strategy output and uses the same hostile-Decimal-safe arithmetic as `evaluate`.

When present, the runner allocates it pro rata. Every held symbol's target is capped at one half of its current close weight, rounded downward in a private Decimal context. A strategy-approved lower target, including SELL zero, remains lower. A higher target or no intent becomes the half-weight target. New positions stay at zero because the 15% BUY block applies. Generated reductions are SELL orders and override strategy-side planning for that symbol. There is at most one order per symbol.

If NAV is zero, held-symbol reduction targets are zero. Decision-level and portfolio-level directives must agree exactly or the runner fails before submitting orders.

## 8. Target-to-Order Planning

For non-reduction strategy decisions:

- REJECTED: no order;
- HOLD: no order;
- zero target delta: no order;
- BUY requires positive delta;
- REDUCE requires negative delta and becomes SELL;
- SELL requires target zero and sells exact held quantity;
- inconsistency is an explicit planning rejection, never reversal.

For BUY/REDUCE:

```text
target_notional = approved_target_weight * close_nav
current_notional = marked position value or zero
delta_notional = target_notional - current_notional
raw_quantity = abs(delta_notional) / current_close
submitted_quantity = floor(raw_quantity, 12 decimal places)
```

Zero dust is skipped. CN BUY lot normalization stays in `submit`; target-zero SELL uses exact held quantity. Private full-exponent Decimal contexts isolate ambient state. Arithmetic failures become stable planning rejections.

Order IDs use canonical SHA-256 over `(run_id, decision_session, strategy_id, symbol, executable_side)`. Every plan records raw calculated quantity, submitted quantity, and the simulator's effective requested quantity from its submission result.

## 9. Audit and Result Models

`OrderPlan` is frozen with status `READY`, `SUBMITTED`, `SKIPPED`, or `REJECTED`, source `STRATEGY` or `RISK_REDUCTION`, symbol, target weight, raw/submitted/effective quantity, optional `OrderIntent`, optional exact submission `Fill`, and stable reason. Pure planning returns READY with an order but no submission. A pure `record_submission` transition converts READY to SUBMITTED and attaches the exact simulator result/effective requested quantity. SUBMITTED means the simulator was called and must carry a submission result; immediate simulator rejection is preserved. SKIPPED/REJECTED plans carry neither order submission nor simulator result; REJECTED never silently changes side.

`SessionResult` is frozen and records session date, terminal open execution results, selected revision metadata, close market/portfolio snapshots, intents, risk decisions, optional reduction directive, order plans, and submission results. Terminal fills link to prior submissions by `order_id`.

`BacktestInputManifest` freezes account/market/initial cash, universe, calendar slice, open frames/CN states, strategy ID/config version, resolved transaction-cost policy, and PIT knowledge policy. It stores no live objects.

`BacktestResult` freezes manifest, fingerprints, ordered session results, complete ledger events, final lots, final snapshot, and realized P&L.

## 10. Canonical IDs, Fingerprints, and Idempotency

Extract the fixture strategy's pure canonical Decimal, UTC datetime, compact JSON, and SHA-256 functions into an internal audit utility; fixture evidence hashes must remain unchanged.

Canonical encoding rules are fixed-field JSON arrays, UTF-8, `ensure_ascii=False`, compact separators, Decimal coefficient/exponent form, UTC six-microsecond timestamps, enum values, and ordered tuples. Python `hash()`, repr, locale, ambient Decimal state, and ambiguous delimiter concatenation are forbidden.

Deterministic IDs use tagged canonical payloads:

- init event: `(run_id, account, market, "init")`;
- open batch: `(run_id, session, "open-execution-batch")`;
- each booked fill: `(run_id, session, order_id, "booked-fill")`;
- close mark: `(run_id, session, "portfolio-mark")`;
- order: `(run_id, decision_session, strategy_id, symbol, side)`.

`spec_fingerprint` covers the manifest except `run_id`, including calendar slice, cost policy, strategy identity, open frames, and CN states. Runner construction requires `spec.strategy_config_version == strategy.config_version`.

`resolved_data_fingerprint` covers every selected revision in session-result order. Both use tagged SHA-256. The first successful run stores:

```text
run_id -> (spec_fingerprint, immutable BacktestResult)
```

Same ID and fingerprint returns the cached result without querying mutable store state. Same ID with a different fingerprint raises conflict. A failed run is never cached. Store changes after success do not mutate the cached result; revision metadata and resolved-data fingerprint preserve what the first run used. Restart durability is deferred.

## 11. Error Boundaries

- Invalid frozen models raise Pydantic validation errors.
- Missing current close bars, malformed strategy output, nonterminal eligible orders, ledger invariant failures, or directive disagreement raise stable runner errors and do not cache.
- Market-rule, cash, dust, and side/delta planning rejections are audit data, not fatal.
- The local ledger/simulator make failed attempts externally atomic.
- `MemoryError` and process-fatal failures are not caught.

## 12. Verification

Tests cover:

- batch event arithmetic, FIFO, fees, realized P&L, complete open marks, reversal, and two-symbol mixed-price false-peak prevention;
- atomic close marks, all-cash close events, replay, and dependent reversal failure;
- cash-aware execution with gaps, fees, SELL proceeds ordering, batch BUY competition, and backward compatibility;
- selected-revision metadata and existing query compatibility;
- five-session next-open BUY/SELL with hand-calculated cash, quantity, P&L, and NAV;
- PIT correction visibility and resolved-data audit;
- A-share lot normalization, T+1, suspension/limit rejection;
- complete-frame next-open terminality and final-close empty decision fields;
- day-start cash and committed-notional reset;
- `assess_portfolio` call semantics and empty-intent 20% pro-rata reduction;
- side/delta and dust planning audit;
- canonical IDs, equivalent Decimal/time offsets, same-run idempotency/conflict, failed-run noncaching;
- hostile Decimal isolation, exact immutable outputs, scope and authority boundaries.
