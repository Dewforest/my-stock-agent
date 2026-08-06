# Strategy A Bounded-LLM Vertical Slice Implementation Plan

> Execute in order. Each capability gets one RED, one GREEN/refactor pass, and verification before the next capability. The normative contract is `docs/designs/2026-08-06-strategy-a-bounded-llm-vertical-slice.md`.

**Goal:** Produce the first auditable simulated trade from real PIT daily bars through a deterministic candidate envelope, a provider-neutral bounded LLM decision, existing risk, next-open execution, and the append-only ledger.

**Architecture:** Keep the Phase 1 `Strategy` tuple boundary. Add immutable Strategy A models, a pure deterministic envelope builder, a provider-neutral decision protocol, an append-only decision journal, a recorded/replay provider, and a bounded strategy adapter. Vendor HTTP adapters are deferred. Decision-record IDs enter `StrategyIntent.evidence_ids`; the existing runner remains vendor-blind.

**Tech stack:** Python 3.11, strict frozen Pydantic v2 models, `Decimal`, DuckDB where durable append-only storage is required, pytest, Ruff, stdlib hashing/JSON.

**Base:** `feature/strategy-a-vertical-slice` from `b117731a39e6ce91c7fcd0b71b4571fa028df785` in `/Users/amezf/my-stock-agent-strategy-a`.

---

## Task 1: Freeze exact provider-neutral LLM contracts

**Files:**

- Create: `src/stock_agent/strategies/llm_contract.py`
- Create: `tests/strategies/test_llm_contract.py`
- Modify: `src/stock_agent/strategies/__init__.py`

### RED

Write tests for exact fields and invariants of:

- `StrategyARegime`
- `StrategyADataQuality`
- `LLMInvocationMode`
- `LLMDecisionStatus`
- `StrategyAConfig`
- `StrategyACandidateEnvelope`
- `LLMDecisionRequest`
- `LLMDecisionSelection`
- `LLMDecisionResponse`
- `LLMDecisionRecord`
- `LLMInvocationAttempt`
- `LLMRunAttestation`

Tests must reject:

- subclasses and polluted/constructed nested models;
- mutable/non-tuple collections;
- floats in Decimal fields;
- unsorted or duplicate candidates/selections/evidence IDs;
- extra/missing symbols;
- actions outside candidate bounds;
- inconsistent request/response fingerprints;
- successful decision records without selections;
- failed attempts that claim a successful decision ID or selections;
- replay attestations that attempt to replace a canonical decision;
- vendor secrets or secret-shaped fields;
- copy updates and ambient Decimal-context leakage.

Run:

```bash
uv run pytest tests/strategies/test_llm_contract.py -q
```

Expected: fail because the module does not exist.

### GREEN

Implement strict, frozen, exact models and canonical fingerprint helpers using `stock_agent.audit`. Export only the approved public symbols.

Run:

```bash
uv run pytest tests/strategies/test_llm_contract.py tests/strategies/test_protocol.py -q
uv run ruff check src/stock_agent/strategies tests/strategies
```

Commit:

```bash
git add src/stock_agent/strategies tests/strategies

git commit -m "feat: define bounded LLM decision contracts"
```

---

## Task 2: Build deterministic Strategy A candidate envelopes

**Files:**

- Create: `src/stock_agent/strategies/strategy_a.py`
- Create: `tests/strategies/test_strategy_a_candidates.py`

### RED

Use literal bars and independently calculated expected values to test:

- symbol grouping and lexical order;
- minimum-history `INSUFFICIENT` behavior;
- `OFFENSIVE`, `NEUTRAL`, and `DEFENSIVE` exact comparisons;
- volume confirmation using prior bars only;
- current-weight calculation;
- allowed action/target mappings for unheld, below-cap, at-cap, above-cap, neutral-held, and defensive-held cases;
- exact evidence bar set and digest;
- no look-ahead when future bars exist outside the supplied snapshot;
- insertion-order independence;
- repeated/concurrent byte-identical results;
- hostile ambient Decimal contexts and no input mutation;
- zero NAV and extreme supported Decimal boundaries;
- exact config-version mismatch failure.

Run:

```bash
uv run pytest tests/strategies/test_strategy_a_candidates.py -q
```

Expected: fail because the builder does not exist.

### GREEN

Implement a pure `build_strategy_a_candidates(context, config)` function. It may import domain/audit types but no data store, network, clock, risk, execution, or ledger authority.

Run:

```bash
uv run pytest tests/strategies/test_strategy_a_candidates.py tests/strategies/test_fixture.py -q
uv run ruff check src/stock_agent/strategies tests/strategies
```

Commit:

```bash
git add src/stock_agent/strategies/strategy_a.py tests/strategies/test_strategy_a_candidates.py

git commit -m "feat: build deterministic Strategy A candidates"
```

---

## Task 3: Add durable append-only decision journal

**Files:**

- Create: `src/stock_agent/strategies/llm_journal.py`
- Create: `tests/strategies/test_llm_journal.py`
- Modify: `src/stock_agent/strategies/__init__.py`

### RED

Test:

- atomic append of one complete terminal record;
- byte-identical idempotent append;
- same record ID with different content conflicts;
- lookup by request fingerprint;
- no partial visibility on write failure;
- two concurrent conflicting writers yield one winner and one conflict;
- close/reopen preserves decisions, attempts, and attestations;
- exactly one successful decision may exist per request fingerprint;
- record and replay access history never changes the canonical decision ID;
- returned records are exact immutable values;
- no raw authorization/key fields can enter stored payloads.

Run:

```bash
uv run pytest tests/strategies/test_llm_journal.py -q
```

Expected: fail because the journal does not exist.

### GREEN

Implement `DuckDBLLMDecisionJournal` as a separate audit store. One transaction appends a terminal attempt plus its optional successful canonical decision, or neither. Failed attempts append only the attempt. Do not modify `PointInTimeStore` and do not add generic repository abstractions.

Run:

```bash
uv run pytest tests/strategies/test_llm_journal.py -q
uv run ruff check src/stock_agent/strategies tests/strategies
```

Commit:

```bash
git add src/stock_agent/strategies/llm_journal.py tests/strategies/test_llm_journal.py src/stock_agent/strategies/__init__.py

git commit -m "feat: persist append-only LLM decisions"
```

---

## Task 4: Implement recorded/replay provider and atomic validation

**Files:**

- Create: `src/stock_agent/strategies/llm_provider.py`
- Create: `tests/strategies/test_llm_provider.py`
- Modify: `src/stock_agent/strategies/__init__.py`

### RED

Define a runtime-checkable `LLMDecisionProvider` and test a recorded/replay implementation for:

- exact provider identity and model identity;
- one batch response covering every candidate exactly once;
- rejection of extra, missing, duplicate, reordered, malformed, unsupported-action, and fingerprint-mismatched output;
- no partial success;
- record mode atomically appends the successful attempt and canonical decision before returning;
- journal failure prevents success;
- replay resolves the exact request/model-identity policy, returns the original decision ID, and makes no transport call;
- missing/conflicting replay fails closed;
- normalized timeout/transport/schema/envelope/audit error codes;
- attempt IDs and timestamps supplied by an injected invocation boundary, never ambient `now()` inside domain code;
- optional record/replay run attestations remain outside intent evidence and `BacktestResult`;
- exceptions and records contain no credentials.

Run:

```bash
uv run pytest tests/strategies/test_llm_provider.py -q
```

Expected: fail because the provider module does not exist.

### GREEN

Implement only provider-neutral ports plus recorded/replay behavior. Do not implement a vendor HTTP adapter.

Run:

```bash
uv run pytest tests/strategies/test_llm_provider.py tests/strategies/test_llm_journal.py -q
uv run ruff check src/stock_agent/strategies tests/strategies
```

Commit:

```bash
git add src/stock_agent/strategies/llm_provider.py tests/strategies/test_llm_provider.py src/stock_agent/strategies/__init__.py

git commit -m "feat: add replayable LLM decision provider"
```

---

## Task 5: Implement the bounded Strategy A adapter

**Files:**

- Modify: `src/stock_agent/strategies/strategy_a.py`
- Create: `tests/strategies/test_strategy_a.py`
- Modify: `src/stock_agent/strategies/__init__.py`

### RED

Test `BoundedLLMStrategyA.evaluate` for:

- exact `Strategy` conformance and immutable identity/version;
- candidate request construction and exact prompt/request fingerprints;
- provider sees only frozen candidate data;
- model-selected action maps to the envelope-owned target weight;
- intent evidence includes market evidence and persisted decision-record ID;
- lexical intent order;
- insufficient candidates cannot become BUY;
- out-of-envelope selection emits zero intents and raises a stable error;
- provider/journal failure emits zero intents;
- no store/risk/execution/ledger/network authority is reachable through `StrategyContext`;
- same recorded request yields byte-identical intents under repetition and concurrency;
- context/config mismatch and polluted values fail before invocation.

Run:

```bash
uv run pytest tests/strategies/test_strategy_a.py -q
```

Expected: fail because the adapter does not exist.

### GREEN

Implement the adapter as orchestration only:

```text
revalidate context
-> build envelopes
-> canonical request
-> provider decision already journaled
-> map action to envelope target
-> immutable StrategyIntent tuple
```

Run:

```bash
uv run pytest tests/strategies -q
uv run ruff check src/stock_agent/strategies tests/strategies
```

Commit:

```bash
git add src/stock_agent/strategies tests/strategies

git commit -m "feat: add bounded Strategy A adapter"
```

---

## Task 6: Prove runner, risk, execution, ledger, and replay integration

**Files:**

- Create: `tests/backtest/test_strategy_a_integration.py`
- Modify only if a proven integration defect requires it:
  - `src/stock_agent/backtest/runner.py`
  - `src/stock_agent/backtest/models.py`

### RED

Construct a multi-session PIT fixture with one offensive candidate and a recorded bounded BUY. Assert:

- no fill on decision close;
- order is pending for the next session;
- next-open fill occurs at the simulator price;
- risk can reduce/reject the target without Strategy A override;
- ledger cash/lots/position/NAV reflect the fill;
- decision record resolves from every decision evidence ID;
- record then fresh replay returns byte-identical intents and `BacktestResult` while external attestations differ;
- changed market data/config/prompt/model policy cannot reuse the record;
- provider failure produces no pending order or ledger trade event.

Run:

```bash
uv run pytest tests/backtest/test_strategy_a_integration.py -q
```

Expected: fail before integration is complete.

### GREEN

Prefer no runner changes. If the existing runner contract cannot preserve a required invariant, add the smallest generic change and regression tests for the fixture strategy.

Run:

```bash
uv run pytest tests/backtest/test_strategy_a_integration.py tests/backtest -q
uv run ruff check src tests
```

Commit:

```bash
git add tests/backtest/test_strategy_a_integration.py src/stock_agent/backtest

git commit -m "test: prove Strategy A trading lifecycle"
```

---

## Task 7: Produce reproducible provider-derived Strategy A evidence

**Files:**

- Create: `scripts/verify_strategy_a_real_data.py`
- Create: `tests/fixtures/strategy_a/<frozen-provider-dataset>.json`
- Create or modify: `scripts/strategy_a_live_schedule.py`
- Create: `docs/verification/strategy-a-real-data.json`
- Create: `tests/scripts/test_verify_strategy_a_real_data.py`

### RED

Fetch and freeze normalized provider-derived rows outside pytest, then test the proof assembler offline with that committed fixture and a recorded LLM selection. Require evidence fields for:

- frozen schedule and provenance;
- market-data provider/source-record digests;
- PIT and raw-price policies;
- Strategy A config/prompt/request/response/decision-record digests;
- explicit `recorded-fixture` LLM mode disclosure and separate record/replay attestations;
- candidate/envelope/action/target summary;
- risk decision;
- submitted order and next-open terminal fill;
- final cash, lot, position, NAV, and realized P&L;
- record, journal close/reopen, fresh replay, and byte-identical result equality;
- exact `current-view-baseline/v1` business-availability PIT disclosure rather than historical ingestion-time replay;
- tracked-secret scan and atomic evidence write.

The fixture proof must reject an empty strategy, no-order result, no-fill result, unchanged NAV, missing decision record, stale source digest, or any claim that a recorded fixture was a live LLM call.

Run:

```bash
uv run pytest tests/scripts/test_verify_strategy_a_real_data.py -q
```

Expected: fail because the proof script does not exist.

### GREEN

Implement the proof script using the committed provider-derived fixture and existing runner. Pin the smallest schedule with enough warm-up, a nonfinal offensive session, one following execution session, and one envelope-permitted recorded BUY. Do not weaken thresholds merely to manufacture a trade; disclose the exact config.

Live network refresh is a separate opt-in verification action and is never called by pytest. A vendor-neutral recorded decision is acceptable and must be labeled honestly.

Run:

```bash
uv run python scripts/verify_strategy_a_real_data.py --schedule <frozen-schedule-id>
uv run pytest tests/scripts/test_verify_strategy_a_real_data.py -q
```

Commit:

```bash
git add scripts docs/verification tests/scripts

git commit -m "test: record Strategy A real-data proof"
```

---

## Task 8: Completion gates, one quality review, and checkpoint push

Run from `/Users/amezf/my-stock-agent-strategy-a`:

```bash
set -euo pipefail
uv run pytest tests/strategies -q
uv run pytest tests/backtest tests/data -q
uv run pytest -q
uv run ruff check .
git diff --check feature/phase-1-trading-core..HEAD
git status --short --branch
```

Then perform exactly one independent code-quality review over:

```text
feature/phase-1-trading-core..HEAD
```

Review only Critical/Important findings against the approved design. Fix confirmed blockers with targeted RED tests. For a confirmed fix, perform one narrow closure review rather than restarting the whole review.

Finally verify:

- evidence digest equals current relevant sources;
- tracked files contain no API keys or authorization values;
- branch contains only Strategy A scope;
- `main` is untouched.

Push only the feature branch:

```bash
git push -u origin feature/strategy-a-vertical-slice
```

Do not merge to `main` without explicit user approval.
