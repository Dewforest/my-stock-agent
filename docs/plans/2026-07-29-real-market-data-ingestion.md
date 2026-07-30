# Real Market Data Ingestion Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Ingest recent real CN and US daily OHLCV into the point-in-time store and prove both paths through the chronological backtester.

**Architecture:** Exact provider models and a small provider protocol isolate external schemas. Eastmoney and Alpha Vantage adapters normalize response strings into Decimal-backed bars; an incremental ingestor applies the explicit current-view baseline/correction policy before appending provenance-bearing revisions. A focused spec builder derives execution-only open frames from persisted real daily bars without exposing full daily data at the open.

**Tech Stack:** Python 3.11+, Pydantic 2, DuckDB, urllib/JSON standard library, Decimal, pytest, Ruff.

**Design authority:** `docs/designs/2026-07-29-real-market-data-ingestion-design.md`

---

## Global execution rules

- Work in `/Users/amezf/my-stock-agent-phase1` on `feature/phase-1-trading-core`.
- Do not merge `main`.
- TDD every production change: behavioral RED, minimal GREEN, refactor, focused/full gates.
- Never print, persist, dump, snapshot, or commit `ALPHA_VANTAGE_API_KEY`.
- Never replace failed live data with fixtures or fabricated responses.
- Provider parser fixtures prove deterministic parsing; only live CN and US runs prove Task 10D completion.
- Use exact Decimal strings end to end; floats are invalid input.
- Standard gates after every task:
  - focused pytest;
  - `uv run ruff check .`;
  - `git diff --check`.
- Full gate after integration tasks: `uv run pytest -q`.

---

### Task 1: Define provider and knowledge-policy contracts

**Objective:** Freeze the exact public models, protocol, errors, and manifest policy before adding HTTP code.

**Files:**
- Create: `src/stock_agent/data/providers/__init__.py`
- Create: `src/stock_agent/data/providers/models.py`
- Create: `src/stock_agent/data/providers/protocol.py`
- Create: `src/stock_agent/data/providers/errors.py`
- Modify: `src/stock_agent/backtest/models.py`
- Modify: `src/stock_agent/backtest/runner.py`
- Modify: `src/stock_agent/data/__init__.py`
- Test: `tests/data/providers/test_models.py`
- Test: `tests/backtest/test_runner.py`

**Step 1: Write failing model tests**

Test exact frozen `DailyBarRequest`, `FetchedDailyBar`, and `IngestionReport` contracts:

- exact `Market`, plain dates, inclusive non-inverted range;
- raw mode only;
- exact finite Decimal OHLCV with domain and DECIMAL(38,12) bounds;
- date-sorted/unique immutable report identities;
- extra fields, subclasses, polluted `model_construct`, `copy(include/exclude/update)`, and `model_copy(update)` rejected;
- provider ID/native symbol/source identity stripped but nonblank;
- protocol runtime-check accepts a conforming provider and rejects missing methods.

Expected RED: imports do not exist.

**Step 2: Add minimal contracts**

Use fixed fields, `ConfigDict(frozen=True, extra="forbid", strict=True, revalidate_instances="always")`, exact-type validators, and copy/subclass guards consistent with Task 10 audit models.

Define a narrow error hierarchy:

```python
class MarketDataError(RuntimeError): ...
class ProviderTransportError(MarketDataError): ...
class ProviderResponseError(MarketDataError): ...
class IngestionError(MarketDataError): ...
```

Do not store raw response bodies or credentials on exceptions.

**Step 3: Extend knowledge policy**

Allow exact manifest policy values:

- `business-available-at/v1`;
- `current-view-baseline/v1`.

Add a frozen runner constructor parameter defaulting to the existing policy. Include it in manifest construction and therefore the existing spec fingerprint. Invalid strings fail at construction or manifest validation.

Test default compatibility and fingerprint divergence between policies.

**Step 4: Verify**

Run:

```bash
uv run pytest tests/data/providers/test_models.py tests/backtest/test_runner.py -q
uv run ruff check .
git diff --check
```

Expected: all pass.

**Step 5: Commit**

```bash
git add src/stock_agent/data src/stock_agent/backtest tests/data/providers tests/backtest/test_runner.py
git commit -m "feat: define real market data contracts"
```

---

### Task 2: Add a redacting HTTP transport and Eastmoney adapter

**Objective:** Fetch and parse exact raw CN daily bars without credentials or third-party HTTP dependencies.

**Files:**
- Create: `src/stock_agent/data/providers/http.py`
- Create: `src/stock_agent/data/providers/eastmoney.py`
- Create: `tests/data/providers/fixtures/eastmoney_600000_daily.json`
- Test: `tests/data/providers/test_http.py`
- Test: `tests/data/providers/test_eastmoney.py`

**Step 1: Capture a bounded real response fixture**

Store only the response fields needed to reproduce the verified Eastmoney shape for `600000` over a short historical range. Record its retrieval date and endpoint shape in a test comment. The fixture contains no cookies or credentials.

**Step 2: Write HTTP transport RED tests**

Test an injected `HttpTransport` implementation around urllib:

- explicit timeout;
- bytes/status return;
- non-2xx -> `ProviderTransportError`;
- URL/network exceptions sanitized;
- values passed in `redactions` absent from exception text/repr;
- no automatic retry.

Use monkeypatched `urlopen`; do not make network calls in standard tests.

**Step 3: Implement minimal transport**

The transport accepts a complete URL and a tuple of exact nonblank redaction values. Catch ordinary URL/HTTP/timeout errors, sanitize first, then raise without chaining an exception whose string may contain the secret (`raise ... from None`). Do not catch `MemoryError` or process-fatal exceptions.

**Step 4: Write Eastmoney parser RED tests**

Cover:

- exact `600000` Shanghai and a Shenzhen symbol mapping;
- unsupported/BSE/malformed symbol rejection;
- fixed endpoint params `klt=101`, `fqt=0`, explicit date bounds and field lists;
- exact fixture OHLCV Decimal values, ascending dates, market/symbol identity;
- provider rows returned unsorted become sorted;
- duplicate dates, wrong field count, nonzero return code, missing data, identity mismatch, invalid Decimal, inverted OHLC, negative volume, and empty range fail atomically;
- provider output is an exact tuple of exact `FetchedDailyBar`.

**Step 5: Implement Eastmoney provider**

Keep URL construction and pure response parsing as separate private functions. Parse JSON bytes, never float-coerce. Derive provider record identity from a fixed canonical tuple using the existing audit utility.

**Step 6: Verify and commit**

```bash
uv run pytest tests/data/providers/test_http.py tests/data/providers/test_eastmoney.py -q
uv run ruff check .
git diff --check
git add src/stock_agent/data/providers tests/data/providers
git commit -m "feat: add Eastmoney daily bar provider"
```

---

### Task 3: Add the Alpha Vantage adapter with credential redaction

**Objective:** Fetch recent raw US daily bars through a token-backed provider without leaking the token.

**Files:**
- Create: `src/stock_agent/data/providers/alpha_vantage.py`
- Create: `tests/data/providers/fixtures/alpha_vantage_daily.json`
- Test: `tests/data/providers/test_alpha_vantage.py`

**Step 1: Prepare a schema fixture**

Use a bounded, redacted real-shape response. Do not include an API key or request URL containing one. If no live key is yet available, create the fixture from provider documentation only for parser development and label it as documentation-derived; replace/confirm it during Task 7 before claiming live acceptance.

**Step 2: Write RED tests**

Cover:

- exact URL function/symbol/outputsize params;
- key appears only in outbound URL, never provider repr, errors, result models, or dumps;
- metadata symbol identity;
- exact OHLCV Decimal parsing;
- ascending filtered inclusive range;
- missing fields, duplicate dates, invalid numbers, empty filtered result;
- `Error Message`, `Information`, and `Note` produce stable `ProviderResponseError` without body/key leakage;
- transport failures remain redacted.

**Step 3: Implement minimal adapter**

Constructor validates an exact nonblank key but stores it only in a private slot. Define a redacted `__repr__`. Pass the key to transport redactions. Request `TIME_SERIES_DAILY` and `outputsize=compact`; do not promise deep history or adjusted prices.

**Step 4: Verify and commit**

```bash
uv run pytest tests/data/providers/test_alpha_vantage.py tests/data/providers/test_http.py -q
uv run ruff check .
git diff --check
git add src/stock_agent/data/providers tests/data/providers
git commit -m "feat: add Alpha Vantage daily bar provider"
```

---

### Task 4: Implement incremental baseline and correction ingestion

**Objective:** Persist normalized provider bars with honest first-baseline and later-correction availability semantics.

**Files:**
- Create: `src/stock_agent/data/providers/ingestion.py`
- Modify: `src/stock_agent/data/providers/__init__.py`
- Test: `tests/data/providers/test_ingestion.py`

**Step 1: Write baseline RED test**

With a real in-memory `PointInTimeStore`, fake conforming provider, exact two-session schedule, and caller-supplied aware ingestion time:

- provider fetch called once with exact request;
- first bars receive session close `available_at`;
- store provenance source is provider ID;
- source record IDs equal canonical expected values;
- report counts requested/received/appended/unchanged and identities exactly;
- provider/native models remain unchanged.

**Step 2: Implement parse-before-write baseline**

Materialize and fully revalidate the exact provider tuple and every schedule mapping before the first append. Build all candidate `Bar` and provenance values first, then append in session order.

**Step 3: Write overlap/correction RED tests**

- unchanged second fetch appends zero and reports unchanged rows;
- changed one-session payload appends one correction with `available_at == ingested_at`;
- correction invisible immediately before ingestion and visible exactly at ingestion;
- earlier selected revision object remains unchanged;
- same changed payload on third fetch is unchanged;
- missing schedule, malformed provider tuple, identity mismatch, duplicate rows, naive ingestion time, ingestion before baseline close, and parse failure write nothing;
- provider exception leaves store unchanged;
- a simulated store failure after the first append surfaces the successful prefix and never returns a success report.

**Step 4: Implement correction comparison**

Compare only normalized domain event payload (market, symbol, session, OHLCV), not provenance timestamps. Query latest revision at the ingestion instant. For changed payloads assign correction availability to ingestion time. Derive source record identity from canonical event payload plus the selected policy/availability instant.

**Step 5: Verify and commit**

```bash
uv run pytest tests/data/providers/test_ingestion.py tests/data/test_pit_store.py -q
uv run pytest tests/data -q
uv run ruff check .
git diff --check
git add src/stock_agent/data/providers tests/data/providers
git commit -m "feat: ingest incremental market data revisions"
```

---

### Task 5: Build real-data backtest specs without open look-ahead

**Objective:** Assemble explicit sessions from persisted real closes while exposing only daily open at execution time.

**Files:**
- Create: `src/stock_agent/backtest/real_data.py`
- Modify: `src/stock_agent/backtest/__init__.py`
- Test: `tests/backtest/test_real_data.py`

**Step 1: Write RED tests**

Define a pure builder API with keyword-only inputs:

- run/account/market/cash/instruments/config;
- explicit `TradingCalendar`;
- exact session date range or exact date tuple;
- exact date -> `(open_at, close_at)` schedule;
- `PointInTimeStore` read authority.

Test:

- session dates exactly match the requested contiguous calendar slice;
- every persisted close revision is selected at its close;
- open frames use daily open for all OHLC, volume zero, `available_at == open_at`;
- open frames do not expose daily high/low/close/volume;
- bar/source models are not mutated;
- symbol-major deterministic ordering;
- missing close/schedule/session, market mismatch, identity pollution, non-aware or non-increasing clocks fail;
- output is an exact `BacktestSpec` and runs with `ChronologicalBacktestRunner(pit_knowledge_policy="current-view-baseline/v1")`.

**Step 2: Implement minimal builder**

Do not infer holidays from missing bars or one security. Do not query future revisions. The builder has no provider dependency and performs no writes.

**Step 3: Add deterministic integration test**

Use a fake provider -> real ingestor -> real DuckDB -> builder -> runner chain. Assert exact selected provenance, manifest policy, fingerprints, and equivalent independent output.

This test is deterministic infrastructure proof, not live acceptance.

**Step 4: Verify and commit**

```bash
uv run pytest tests/backtest/test_real_data.py tests/backtest/test_runner_integration.py -q
uv run pytest tests/backtest tests/data -q
uv run ruff check .
git diff --check
git add src/stock_agent/backtest tests/backtest
git commit -m "feat: build backtests from persisted market data"
```

---

### Task 6: Run and preserve CN live end-to-end evidence

**Objective:** Prove the Eastmoney path with live public data, temporary DuckDB persistence, and a real chronological backtest.

**Files:**
- Create: `scripts/verify_live_market_data.py`
- Test: `tests/data/providers/test_live_verifier.py`
- Modify: `.gitignore` only if the script creates a documented local artifact directory

**Step 1: Write verifier unit tests**

The composition function accepts injected provider/store/clock and returns a small immutable verification summary. Test that output contains only:

- provider ID;
- market/symbol/date range;
- received/appended counts;
- session count;
- spec and resolved-data fingerprints;
- final NAV and selected revision count.

It must not contain raw response bodies or credentials.

**Step 2: Implement the live script**

Use argparse or a minimal main function, not Typer CLI integration. Modes:

```bash
uv run python scripts/verify_live_market_data.py --market CN --symbol 600000 --start YYYY-MM-DD --end YYYY-MM-DD
uv run python scripts/verify_live_market_data.py --market US --symbol IBM --start YYYY-MM-DD --end YYYY-MM-DD
```

Use `ZoneInfo("Asia/Shanghai")` and `ZoneInfo("America/New_York")` to produce explicit clocks. Use provider-returned dates only after checking they match an explicit expected session list supplied/derived for the bounded verification range; do not silently call missing rows holidays.

Use a temporary file-backed or in-memory DuckDB and clean it deterministically.

**Step 3: Execute CN live acceptance**

Choose a recent bounded range that currently returns at least five sessions. Run the actual command. Record the command, HTTP/provider identity, counts, fingerprints, and backtest summary in the commit message or a bounded generated evidence file under `docs/verification/` that contains no raw payload.

If network/provider fails, diagnose and report; do not commit fabricated evidence and do not mark this task complete.

**Step 4: Verify and commit**

```bash
uv run pytest tests/data/providers/test_live_verifier.py -q
uv run python scripts/verify_live_market_data.py --market CN --symbol 600000 --start 2025-07-01 --end 2025-07-10
uv run ruff check .
git diff --check
git add scripts tests/data/providers docs/verification
git commit -m "test: verify live CN market data backtest"
```

---

### Task 7: Run and preserve US live end-to-end evidence

**Objective:** Prove the Alpha Vantage path using a caller-supplied environment key without credential leakage.

**Files:**
- Modify only if live behavior exposes a real schema defect:
  - `src/stock_agent/data/providers/alpha_vantage.py`
  - `tests/data/providers/test_alpha_vantage.py`
  - `scripts/verify_live_market_data.py`
- Create: bounded evidence under `docs/verification/` if Task 6 established that pattern

**Step 1: Check credential presence without printing it**

Use a command that reports only present/absent, never the value. If absent, stop and request the user set `ALPHA_VANTAGE_API_KEY` in their shell. Do not inspect `.env`, browser storage, Keychain, shell history, or unrelated credential stores.

**Step 2: Execute live acceptance**

```bash
uv run python scripts/verify_live_market_data.py --market US --symbol IBM --start 2026-07-22 --end 2026-07-28
```

Require at least five actual sessions, nonempty provenance, successful ingestion, and a completed backtest. A rate-limit/information payload is failure, not empty success.

**Step 3: Convert live schema surprises into TDD fixes**

If the response shape differs:

1. save a minimal redacted fragment;
2. write a failing parser test;
3. implement the smallest correction;
4. rerun focused tests;
5. rerun live verification.

Never paste or commit the key.

**Step 4: Secret scan and commit**

Search tracked diff for the environment value only through a script that prints match count/path, not the secret. Verify zero matches.

```bash
uv run pytest tests/data/providers tests/backtest/test_real_data.py -q
uv run ruff check .
git diff --check
git add src/stock_agent/data/providers tests scripts docs/verification
git commit -m "test: verify live US market data backtest"
```

Task 10D remains `in_progress` if this live step cannot run.

---

### Task 8: Final Task 10D review and branch checkpoint

**Objective:** Verify scope, audit semantics, credential hygiene, live evidence, and all repository gates before pushing the feature branch.

**Files:**
- Modify only to address verified review findings.

**Step 1: Specification review**

Review against the design, focusing on:

- current-view baseline disclosure;
- correction availability;
- no open-frame look-ahead;
- exact Decimal and provenance identity;
- empty/error provider responses;
- credential redaction;
- both live E2E proofs.

Fix all Critical/Important findings with RED tests.

**Step 2: Independent code-quality review**

Check duplicated parsing rules, oversized adapters, exception leakage, hidden mutable state, unstable provider ordering, and brittle tests. Fix only real Critical/Important findings.

Do not recursively review the reviews.

**Step 3: Mandatory gates**

```bash
uv run pytest tests/data tests/backtest -q
uv run pytest -q
uv run ruff check .
git diff --check 31f2ce5..HEAD
git status --short --branch
```

Expected: all pass; worktree clean.

**Step 4: Verify live evidence again**

Run both actual live commands. If either fails because of network, provider, rate limit, or missing credential, report the blocker and do not mark complete.

**Step 5: Push checkpoint**

Push only `feature/phase-1-trading-core`; do not merge `main`.

```bash
git push origin feature/phase-1-trading-core
```

---

## Acceptance checklist

- [ ] Exact provider contracts and stable error boundaries.
- [ ] Eastmoney raw CN daily parsing and live fetch.
- [ ] Alpha Vantage raw US daily parsing and live fetch with redacted key.
- [ ] Parse-before-write validation.
- [ ] Unchanged incremental fetch is idempotent.
- [ ] Changed payload appends a correction at ingestion time.
- [ ] Manifest fingerprints `current-view-baseline/v1`.
- [ ] Open execution frames expose only daily open.
- [ ] Real CN provider -> DuckDB -> runner succeeds.
- [ ] Real US provider -> DuckDB -> runner succeeds.
- [ ] No key or raw sensitive response in tracked files/logs/models.
- [ ] Full tests, Ruff, diff checks, reviews, and clean worktree pass.
