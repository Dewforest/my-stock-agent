# Real Market Data Ingestion Implementation Plan

> **For Hermes:** Execute five coherent vertical capabilities. Do not split them into A/B/C micro-tasks. Each capability gets one invariant-driven RED set, one implementation pass, one specification review, one quality review, and one commit.

**Goal:** Ingest recent real CN and US raw daily OHLCV into the point-in-time store and prove both paths through an auditable chronological backtest.

**Architecture:** The store separates local-observation authority from business-PIT authority and writes ingestion batches atomically. The runner freezes its full resolved revision matrix before cache or execution. Strict provider adapters feed one shared ingestor and spec builder; synthetic open frames link to persisted source revisions. Live evidence is valid by relevant-source digest, not by pretending external payloads never change.

**Tech Stack:** Python 3.11+, Pydantic 2, DuckDB, `http.client`, Decimal, ZoneInfo, pytest, Ruff.

**Design authorities:**

- `docs/designs/2026-07-29-real-market-data-ingestion-design.md`
- `docs/designs/2026-07-29-real-market-data-ingestion-contract.md`

The normative contract controls every identity, state transition, wire schema, error code, failure side effect, secret boundary, and completion gate.

---

## Execution discipline

- Work only in `/Users/amezf/my-stock-agent-phase1` on `feature/phase-1-trading-core`.
- Do not merge `main`.
- Do not restore the pre-preflight WIP stash; it was based on an incomplete contract.
- For each capability, write its complete adversarial RED set before production changes, verify failures are contract-related, then implement the whole vertical capability.
- If two Important findings expose the same missing invariant, stop implementation and revise the normative contract instead of stacking fixes.
- Standard tests never open sockets and never read `ALPHA_VANTAGE_API_KEY`.
- Live failure is never replaced with a fixture, skip, empty result, or fabricated evidence.
- Do not report every internal correction. Report only completed capability, verified gates, or a real blocker.

---

## Capability 1: Shared audit foundation

**Outcome:** The domain/store/backtest path has one complete authority model before either provider adapter exists.

**Files:**

- Create: `src/stock_agent/data/providers/__init__.py`
- Create: `src/stock_agent/data/providers/models.py`
- Create: `src/stock_agent/data/providers/protocol.py`
- Create: `src/stock_agent/data/providers/errors.py`
- Create: `src/stock_agent/data/providers/ingestion.py`
- Create: `src/stock_agent/backtest/real_data.py`
- Modify: `src/stock_agent/data/store.py`
- Modify: `src/stock_agent/data/__init__.py`
- Modify: `src/stock_agent/backtest/models.py`
- Modify: `src/stock_agent/backtest/runner.py`
- Modify: `src/stock_agent/backtest/__init__.py`
- Test: `tests/data/providers/test_contracts_and_ingestion.py`
- Test: `tests/data/test_pit_store.py`
- Test: `tests/backtest/test_real_data.py`
- Test: `tests/backtest/test_runner.py`
- Test: `tests/backtest/test_runner_integration.py`

### RED set

Write one adversarial contract suite covering all shared invariants before production code:

1. exact final frozen provider/request/report/error metadata models;
2. Decimal admission, effective scale, magnitude, hostile Decimal context and canonical trailing-zero equality;
3. provider protocol postconditions at the ingestor boundary, including provider-ID mutation and wrong containers/subclasses/pollution;
4. baseline A, repeat A, correction B, repeat B and rollback A;
5. strict per-stream ingestion clocks and different-provider event conflict;
6. `latest_observed_bar_revision` versus `latest_bar_revision_as_of` authority separation;
7. atomic store batch success, idempotency, conflict and rollback after a forced failure at every batch position;
8. canonical source-record golden vectors;
9. report arithmetic and deterministic appended identity order;
10. dense multi-symbol/session builder success and every missing-grid failure;
11. synthetic open OHLC/volume/time plus exact persisted provenance link;
12. close-after correction excluded from own-session open provenance;
13. both manifest policies and fingerprint divergence;
14. full runner revision matrix frozen before cache/mutable dependencies;
15. correction inserted after freeze cannot alter the running result;
16. same run/spec but changed resolved fingerprint is a conflict;
17. independent runners over the same frozen inputs are byte-equivalent.

Run the new focused tests and confirm they fail because these contracts do not yet exist. Keep this RED evidence in the capability summary.

### Implementation

Implement the normative contract as one shared backbone:

- exact provider models and stable error code/retry metadata;
- latest-observed query keyed by provider/event;
- atomic DuckDB batch append with full rollback;
- complete ingestion state machine and source identity formula;
- exact bounded session schedule input;
- dense real-data spec builder and `open_frame_sources`;
- `pit_knowledge_policy` and `market_data_price_policy` in manifest/fingerprint;
- runner pre-resolution of the complete owning-session revision matrix;
- resolved-data fingerprint before cache lookup;
- cache conflict on either spec or resolved-data change;
- no session-loop store queries.

Do not introduce HTTP, provider-native JSON, retries, fallback, CLI, sparse data, corporate actions, or general calendars in this capability.

### Verification

```bash
uv run pytest tests/data/providers/test_contracts_and_ingestion.py tests/data/test_pit_store.py tests/backtest/test_real_data.py tests/backtest/test_runner.py tests/backtest/test_runner_integration.py -q
uv run pytest tests/data tests/backtest -q
uv run pytest -q
uv run ruff check .
git diff --check
```

Then perform:

- one specification review against the normative contract;
- one code-quality review focused on duplicated authority, transaction safety, cache side effects and immutable boundaries.

Fix Critical/Important findings only. Re-run the same gates once.

### Commit

```bash
git add src/stock_agent/data src/stock_agent/backtest tests/data tests/backtest
git commit -m "feat: establish real data audit foundation"
```

---

## Capability 2: Complete CN vertical slice

**Outcome:** A strict Eastmoney response travels through transport, parser, atomic ingestion, real-data builder and runner in deterministic tests.

**Files:**

- Create: `src/stock_agent/data/providers/http.py`
- Create: `src/stock_agent/data/providers/json.py`
- Create: `src/stock_agent/data/providers/eastmoney.py`
- Create: `tests/data/providers/fixtures/eastmoney_600000_daily.json`
- Create: `tests/data/providers/fixtures/eastmoney_600000_daily.metadata.json`
- Test: `tests/data/providers/test_http_and_json.py`
- Test: `tests/data/providers/test_eastmoney_vertical.py`

### RED set

Before production changes, test the complete CN boundary:

1. fixed HTTPS host/path/method and exact query parameters/order-independent decoding;
2. fixed Shanghai/Shenzhen mapping and explicit unsupported symbols;
3. one timeout applied to connect/read with stage-specific safe code;
4. redirect, status, TLS, size, content encoding, strict UTF-8/BOM and connection failures;
5. duplicate JSON keys at every level, non-finite constants and trailing garbage;
6. Eastmoney `rc`, exact code/market identity, exact 11-field row and fixed OHLCV indexes;
7. malformed/duplicate/out-of-range/empty rows fail all-or-nothing;
8. no binary float path;
9. fixture metadata and SHA-256 provenance;
10. fake transport → Eastmoney → ingestor → real DuckDB → builder → runner;
11. exact selected revisions, open provenance, policies, spec fingerprint and resolved fingerprint;
12. two independent assemblies from one normalized tuple are deterministic;
13. attempted socket access in standard tests fails.

### Implementation

Implement:

- fixed `http.client.HTTPSConnection` profile from the normative contract;
- shared strict JSON loader;
- exact Eastmoney adapter and provider record digest;
- a bounded captured fixture and metadata file;
- no retry, fallback, compression, broad symbol mapping or response excerpts.

The vertical integration uses the shared Capability 1 ingestor/builder without CN-specific branches outside the adapter and symbol mapping.

### Verification

```bash
uv run pytest tests/data/providers/test_http_and_json.py tests/data/providers/test_eastmoney_vertical.py -q
uv run pytest tests/data tests/backtest -q
uv run pytest -q
uv run ruff check .
git diff --check
```

Perform one specification review and one quality/security review of the entire CN vertical slice. Re-run once after any Critical/Important fix.

### Commit

```bash
git add src/stock_agent/data/providers tests/data/providers
git commit -m "feat: add auditable Eastmoney data path"
```

---

## Capability 3: Complete US vertical slice and secret boundary

**Outcome:** Alpha Vantage recent daily data uses the same backbone while the key is absent from every observable surface.

**Files:**

- Create: `src/stock_agent/data/providers/alpha_vantage.py`
- Create: `tests/data/providers/fixtures/alpha_vantage_daily.json`
- Create: `tests/data/providers/fixtures/alpha_vantage_daily.metadata.json`
- Test: `tests/data/providers/test_alpha_vantage_vertical.py`
- Test: `tests/data/providers/test_secret_boundary.py`

### RED set

Before production changes, test:

1. fixed host/path and exact `TIME_SERIES_DAILY`, symbol, compact, JSON and key params;
2. exact uppercase Phase 1 symbol subset and no punctuation conversion;
3. full metadata/series validation before filtering;
4. exact record keys and full-series duplicate/numeric admission;
5. `Error Message`, throttle `Note`/`Information`, other `Information`, schema and identity categories;
6. compact earliest/latest coverage and expected-closed-session completeness;
7. empty versus compact-coverage distinction;
8. all-or-nothing parser behavior;
9. fake transport → Alpha → shared ingestor → DuckDB → builder → runner;
10. deterministic independent assembly;
11. a canary key absent from provider/transport repr, copy, deepcopy, pickle result or failure, model dumps, reports, source IDs, exception args/attributes/cause/context, traceback, logs and verifier-facing errors;
12. standard tests never read the environment or open a socket.

### Implementation

Implement the adapter with a private key slot and explicit copy/deepcopy/pickle rejection. Build the target with `urlencode`, pass it only to the fixed transport, and construct safe errors outside original exception handlers. Do not retain raw response bytes after parsing.

The parser validates the complete compact response before inclusive filtering. It rejects insufficient earliest coverage and delegates closed-session completeness to the explicit schedule/builder boundary.

If no live key exists yet, the fixture may be documentation-derived but must say so. Capability 4 replaces or confirms it from a redacted live shape before completion.

### Verification

```bash
uv run pytest tests/data/providers/test_alpha_vantage_vertical.py tests/data/providers/test_secret_boundary.py -q
uv run pytest tests/data tests/backtest -q
uv run pytest -q
uv run ruff check .
git diff --check
```

Perform one specification review and one quality/security review over the complete US slice and secret graph. Re-run once after Critical/Important fixes.

### Commit

```bash
git add src/stock_agent/data/providers tests/data/providers
git commit -m "feat: add secure Alpha Vantage data path"
```

---

## Capability 4: Both live provider-to-runner proofs

**Outcome:** CN and US each produce contemporaneous, safe, reproducible evidence against one relevant-source digest.

**Files:**

- Create: `scripts/verify_live_market_data.py`
- Create: `scripts/live_market_schedules.py`
- Create: `tests/data/providers/test_live_verifier.py`
- Create: `docs/verification/real-market-data-cn.json`
- Create: `docs/verification/real-market-data-us.json`

### RED set

Test the verifier with injected providers and clock:

1. explicit bounded schedules include aware clocks, IANA zone, provenance and generation date;
2. local dates, monotonic sessions, DST offsets, early-close representation and closed-at-ingestion checks;
3. expected dense session coverage and minimum five sessions;
4. temporary DuckDB cleanup after success and every failure;
5. output/evidence safe schema and relevant-source digest;
6. classified errors print only code/safe metadata and exit nonzero;
7. unknown errors print only type name/run ID, never repr/traceback;
8. evidence freshness ignores evidence-only commits but rejects relevant source changes;
9. key never enters argv or evidence;
10. same normalized tuple independently assembled twice yields equal fingerprints.

### Implementation

The live verifier is a small script, not the Task 12 product CLI. It accepts market, symbol and exact schedule name; US key comes only from `ALPHA_VANTAGE_API_KEY`.

Freeze two audited schedule inputs:

- CN `600000`, 2025-07-01 through 2025-07-10, with at least five explicit closed sessions;
- US `IBM`, a compact-window range selected at execution time and then committed as exact explicit sessions, with exchange-calendar provenance and no early-close guessing.

The US schedule date is finalized only after checking current compact coverage and authoritative exchange calendar information. That is a live-data fact, not a placeholder production contract.

Run CN first. Then check only whether the US environment key is present; do not print or discover it elsewhere. If absent, stop Capability 4 and report a credential blocker. Do not inspect `.env`, Keychain, browsers, shell history or unrelated credential stores.

Each successful evidence JSON records the normative fields and current relevant-source digest. The two markets must use the same digest.

### Verification

```bash
uv run pytest tests/data/providers/test_live_verifier.py tests/data/providers/test_secret_boundary.py -q
uv run python scripts/verify_live_market_data.py --schedule cn-600000-2025-07
uv run python scripts/verify_live_market_data.py --schedule us-ibm-current-compact
uv run pytest -q
uv run ruff check .
git diff --check
```

A missing key, network/TLS/throttle/coverage/data-gap/evidence failure leaves Task 10D in progress. There is no skipped success.

Perform one audit of both evidence records and the secret scan.

### Commit

```bash
git add scripts tests/data/providers docs/verification
git commit -m "test: prove live CN and US market data paths"
```

---

## Capability 5: Final integration gate and branch checkpoint

**Outcome:** One final review proves the five-module system is coherent; the feature branch is pushed without merging main.

### Specification review

Review the full diff against both design authorities, with one matrix covering:

- authority separation;
- baseline/correction/rollback;
- atomicity;
- canonical identity;
- dense schedules;
- open provenance;
- runner freeze/cache;
- both wire contracts;
- error/retry semantics;
- secret graph;
- live evidence freshness and both policy disclosures.

Fix Critical/Important findings only. If a finding changes an invariant, revise the normative contract first instead of patching implementation locally.

### Independent quality review

Review transaction handling, query plans, immutable boundaries, parser duplication, exception graph, deterministic ordering, test realism and maintainability. Do not run another review cycle after both reviews pass.

### Mandatory gates

```bash
uv run pytest tests/data tests/backtest -q
uv run pytest -q
uv run ruff check .
git diff --check 31f2ce5..HEAD
git status --short --branch
```

Verify:

- both live evidence files match the current relevant-source digest;
- secret canary and tracked-diff scan are clean;
- no standard test touched network/environment credentials;
- worktree is clean.

### Commit and push

Commit only review-driven changes if any, then:

```bash
git push origin feature/phase-1-trading-core
```

Do not merge `main`.

---

## Completion checklist

- [ ] Shared authorities, atomic ingestion, open provenance and frozen runner matrix.
- [ ] Complete deterministic Eastmoney vertical path.
- [ ] Complete deterministic Alpha Vantage vertical path and secret graph.
- [ ] Real CN live evidence.
- [ ] Real US live evidence.
- [ ] Same relevant-source digest for both evidence records.
- [ ] Both manifest policies and honest non-vintage/non-total-return disclosure.
- [ ] One final specification review and one final quality review.
- [ ] Full tests, Ruff, diff, secret and clean-tree gates.
- [ ] Feature branch pushed; main untouched.
