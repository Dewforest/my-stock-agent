# Task 10 Chronological Backtest Runner Implementation Plan

> Execute with the repository superpowers workflow: one task at a time, strict RED-GREEN-REFACTOR, fresh spec review before quality review, and full-suite verification after every commit.

**Goal:** Build a deterministic, no-look-ahead, next-open backtest runner that composes PIT data, strategy, public hard risk, cash-aware execution, and an immutable auditable ledger.

**Architecture:** Introduce replayable atomic open/close valuation events, add optional cash-aware execution, expose selected PIT revision metadata and portfolio-level risk assessment, then build frozen runner contracts, pure order planning, and the chronological state machine. Each run owns fresh mutable internals and returns only immutable audit artifacts.

**Source of truth:** `docs/designs/2026-07-29-chronological-backtest-runner-design.md`

---

## Task 10.1: Extract canonical audit encoding without changing fixture evidence

**Files**
- Create: `src/stock_agent/audit/__init__.py`
- Create: `src/stock_agent/audit/canonical.py`
- Modify: `src/stock_agent/strategies/fixture.py`
- Create: `tests/audit/test_canonical.py`
- Modify: `tests/strategies/test_fixture.py`

**RED**

Add tests for:

- canonical Decimal zero and trailing-zero equivalence;
- canonical UTC datetime equivalence across offsets;
- fixed-field compact JSON UTF-8 encoding;
- tagged SHA-256 lowercase output;
- hostile ambient Decimal context isolation;
- the existing fixture evidence ID remains exactly `bar-sha256:481d5c3e8ceff71f4fc65db6cede2fdc79727edf011373e03be1e84ce8284b4e`.

Run:

`uv run pytest tests/audit/test_canonical.py tests/strategies/test_fixture.py -q`

Expected: import failure for the new audit module.

**GREEN**

Move the existing pure tuple-based Decimal and UTC datetime encoding into audit helpers. Add a fixed-array canonical JSON encoder and tagged SHA-256 helper. Do not use `Decimal.normalize`, float, repr, locale, ambient context, or Python `hash()`. Make the fixture call the shared helpers without changing its payload contract.

**Verify**

- focused tests;
- `uv run pytest -q`;
- `uv run ruff check .`;
- `git diff --check`.

**Commit**

`refactor: centralize canonical audit encoding`

---

## Task 10.2: Expose selected PIT revision metadata

**Files**
- Modify: `src/stock_agent/data/store.py`
- Modify: `src/stock_agent/data/__init__.py`
- Modify: `tests/data/test_store.py`

**RED**

Test immutable exact `SelectedBarRevision` fields:

- selected `Bar`;
- aware `ingested_at`;
- nonblank `source` and `source_record_id`;
- full revalidation/copy resistance and exact tuple-free immutable state.

Test `latest_bar_revision_as_of` uses the exact existing precedence `(available_at, ingested_at, source, source_record_id)` and returns metadata for the selected row. Verify corrections become selected only when `available_at <= as_of`. Verify `latest_bar_as_of` remains byte-compatible and delegates to `.bar` semantics. Include closed-store, exact market/date/time, and equivalent-offset cases.

**GREEN**

Add the frozen selected-revision model and one internal row-decoding path. Implement `latest_bar_revision_as_of`; reduce `latest_bar_as_of` to a thin compatibility wrapper. No schema migration and no batch query.

**Verify and commit**

`feat: expose selected point-in-time revisions`

---

## Task 10.3: Add atomic open booking and close valuation ledger events

**Files**
- Modify: `src/stock_agent/account/ledger.py`
- Modify: `src/stock_agent/account/__init__.py`
- Create: `tests/account/test_batch_events.py`
- Modify: `tests/account/test_ledger.py`

**RED — contracts**

Test exact frozen `PositionMark` and `BookedFill`, including supported Decimal boundaries, side BUY/SELL only, plain date, canonical symbol ordering, unique fill IDs, exact tuple/item types, subclass rejection, and copy/revalidation resistance.

Test `OpenExecutionBatchBooked` requires nonempty ordered fills and sorted complete marks. Test `PortfolioMarked` exact sorted marks including empty tuple.

**RED — replay**

Test one open batch:

- applies BUY/SELL in tuple order;
- preserves FIFO lots and exact fees/realized P&L;
- applies all resulting open marks before one NAV/peak update;
- prevents a two-symbol opposite-gap mixed-price false peak;
- rejects incomplete/extra marks atomically;
- rejects insufficient cash/oversell without mutation;
- is one reversible unit.

Test close marking:

- marks all positions simultaneously;
- all-cash empty mark advances `as_of`;
- mixed-price false peak is impossible;
- snapshot replay reproduces the same state;
- reversal of an upstream event fails atomically when a downstream complete mark becomes invalid;
- `append_many` accepts only an exact event tuple, commits a complete candidate stream once, treats empty input as a no-op, and leaves state unchanged on any failure;
- one `append_many` can reverse all dependent complete events and append a corrected stream atomically.

**GREEN**

Refactor ledger fill application into shared private operations used by legacy fill events and the new open batch. For the open batch, apply all accounting and all marks, then materialize positions/NAV/peak once. For the close event, apply all marks then materialize once. Add exact atomic `append_many`; make `append` delegate to it. Keep legacy behavior unchanged. Extend event union, append checks, exports, active-event replay, and tests.

**Verify and commit**

`feat: add atomic ledger valuation events`

---

## Task 10.4: Make next-open execution cash-aware

**Files**
- Modify: `src/stock_agent/execution/simulator.py`
- Modify: `tests/execution/test_simulator.py`
- Modify: `tests/execution/test_market_rules.py` if needed

**RED**

Test optional `available_cash_by_account`:

- omitted mapping preserves all existing behavior;
- mapping and values are atomically validated before timeline mutation;
- every eligible account must be present;
- a gap-up BUY plus fees that exceeds cash is terminal REJECTED with `insufficient available cash`;
- multiple BUYs compete in deterministic pending order;
- earlier SELL net proceeds fund a later BUY, but later SELL does not rescue an earlier rejected BUY;
- fees exceeding SELL proceeds are rejected;
- rejected orders do not change simulated cash;
- successful fills exactly reconcile with a subsequent `OpenExecutionBatchBooked`.

Include hostile Decimal context and unsupported numeric boundaries.

**GREEN**

Add the optional mapping. Copy and validate it before touching pending/timeline state. Maintain private per-account cash in pending order. Reuse the simulator's exact fee arithmetic. Keep lot/T+1/limit/suspension checks before cash mutation. Preserve old behavior when the argument is absent.

**Verify and commit**

`feat: reject unaffordable next-open orders`

---

## Task 10.5: Expose portfolio-level drawdown assessment

**Files**
- Modify: `src/stock_agent/risk/engine.py`
- Modify: `tests/risk/test_engine.py`

**RED**

Test `RiskEngine.assess_portfolio`:

- exact `RiskContext` only and full revalidation;
- returns `None` below 20% drawdown;
- returns the existing half-gross `RiskReductionTarget` at and above 20%;
- works with zero NAV and empty positions;
- repeated/concurrent calls are deterministic and do not mutate context;
- hostile Decimal state is unchanged;
- result agrees exactly with legacy decision-level directives from `evaluate` and every item of `evaluate_many`;
- empty-intent callers can still obtain the directive.

**GREEN**

Expose the existing private reduction calculation through a strict public method. Keep `evaluate` and `evaluate_many` behavior backward compatible and share one private implementation.

**Verify and commit**

`feat: expose portfolio drawdown assessment`

---

## Task 10.6: Define immutable backtest contracts and pure order planning

**Files**
- Create: `src/stock_agent/backtest/__init__.py`
- Create: `src/stock_agent/backtest/models.py`
- Create: `src/stock_agent/backtest/planning.py`
- Create: `tests/backtest/test_models.py`
- Create: `tests/backtest/test_planning.py`

**RED — models**

Test exact frozen:

- `BacktestSession` open/close ordering, plain date, execution-only open bars, complete sorted universe, CN/US state rules;
- `BacktestSpec` identity, supported initial cash, fixed sorted instruments, chronological sessions, and config version;
- `OrderPlan` state machine (`READY -> SUBMITTED`, plus terminal `SKIPPED` / `REJECTED`) and quantity audit;
- `SessionResult`, `BacktestInputManifest`, and `BacktestResult` exact immutable tuples and no live authority objects.

Test full revalidation, subclass rejection where contracts require exact types, model-copy corruption resistance, no ambient context mutation, and exact public exports.

**RED — planning**

Test a pure planner for:

- HOLD and zero delta skip;
- BUY positive delta;
- REDUCE negative delta becomes SELL;
- SELL target zero uses exact held quantity;
- side/delta mismatch is REJECTED;
- 12-place floor, dust skip, extreme Decimal stable rejection;
- deterministic canonical order IDs;
- 20% pro-rata cap uses at most half current weight, works with empty strategy output, preserves a lower approved target, and creates at most one order per symbol;
- no input mutation and lexical deterministic generated reductions.

**GREEN**

Implement only frozen contracts, validation, canonical manifest payloads, pure target/order planning, and the pure READY-to-SUBMITTED audit transition. Do not query store, execute, or mutate ledger in this task.

**Verify and commit**

`feat: define backtest contracts and order planning`

---

## Task 10.7: Implement the chronological runner core

**Files**
- Create: `src/stock_agent/backtest/runner.py`
- Modify: `src/stock_agent/backtest/__init__.py`
- Create: `tests/backtest/test_runner.py`

**RED — five-session vertical slice**

Create a fixed five-session US fixture and a minimal immutable scripted strategy:

- BUY after session 1 close;
- fill at session 2 open;
- SELL after session 4 close;
- fill at session 5 open;
- exact fees, cash, quantity, FIFO realized P&L, NAV, peak, and final lots match hand calculations;
- final session executes the pending SELL and values close, but emits empty strategy/risk/planning/submission fields.

Assert exact chronological ordering and one `PortfolioMarked` per close. When an open has successful fills, assert one `OpenExecutionBatchBooked` containing all fills and complete marks.

**RED — orchestration invariants**

Test:

- deterministic `CashInitialized` immediately before first open;
- exact config-version match;
- complete current close data required before close append;
- strategy output exact tuple/identity/market/time/universe/uniqueness validation;
- one `assess_portfolio` and one `evaluate_many` per nonfinal session, including empty intents;
- day-start cash captured before open fills;
- new-position committed notional includes today's qualifying open fills and resets next session;
- eligible pending orders must terminate at the next complete open;
- normal execution rejection is audited and not booked;
- ledger invariant failure aborts and produces no result.

**GREEN**

Implement fresh local ledger/simulator construction, deterministic initialization, session loop, selected-revision snapshot construction, atomic open/close events, risk calls, planning, submission, and immutable session/result assembly. Keep all ordering serial and explicit.

**Verify and commit**

`feat: add chronological no-look-ahead backtester`

---

## Task 10.8: Complete PIT, drawdown, CN, and idempotency integration

**Files**
- Modify: `src/stock_agent/backtest/runner.py`
- Modify: `src/stock_agent/backtest/models.py`
- Modify: `tests/backtest/test_runner.py`
- Create: `tests/backtest/test_runner_integration.py`

**RED — PIT and audit**

Test a correction whose `available_at` is after session 3:

- invisible through session 3;
- selected from the first lawful later close;
- never rewrites earlier `SessionResult`;
- selected source/record/ingestion metadata is exact;
- `resolved_data_fingerprint` changes when selected data changes.

**RED — drawdown and CN**

Test:

- 20% drawdown plus empty strategy output still queues pro-rata reductions;
- lower strategy SELL/REDUCE target wins;
- all decision directives equal the portfolio directive;
- CN BUY normalization, next-open T+1 SELL behavior, suspension, limit-up/down, and exact lot state;
- complete frames prevent cross-session pending drift.

**RED — fingerprints/idempotency**

Test canonical `spec_fingerprint` includes manifest, calendar slice, transaction costs, open frames, CN states, and strategy identity but excludes `run_id`. Equivalent Decimal and datetime-offset representations hash equally. Test:

- same runner, run ID, and spec returns the exact cached immutable result without strategy/store/execution calls;
- same run ID with different spec fingerprint raises conflict;
- failed run is not cached and can be retried after correcting its input dependency;
- store revisions after a successful run do not mutate the cached result;
- event/order/fill IDs and result dumps are byte-identical across independent equivalent runs.

**GREEN**

Complete revision manifests, resolved-data hashing, pro-rata override, CN integration, registry conflict handling, and stable error types/messages. Do not add persistence or parallelism.

**Verify and commit**

`test: harden chronological backtest integration`

---

## Task 10.9: Final Task 10 review and checkpoint

**Scope review**

Verify only Task 10 and its required contract extensions were added. No metrics, benchmark comparison, CLI, CI, live API, durable run registry, multi-market orchestration, or parallel execution.

**Mandatory gates**

Run separately:

- `uv run pytest tests/audit tests/data tests/account tests/execution tests/risk tests/backtest -q`
- `uv run pytest -q`
- `uv run ruff check .`
- `git diff --check <task-10-base>..HEAD`
- `git status --short --branch`

Perform fresh specification review against the design, fix all Critical/Important findings, rerun gates, then perform a separate code-quality review. Do not recursively review reviews.

**Documentation checkpoint**

Commit the approved design and this plan if they were not included earlier:

`docs: design chronological backtest runner`

Push `feature/phase-1-trading-core` only after all Task 10 gates and both reviews pass. Do not merge `main`.
