# Unattended Dual-Market Paper Runtime Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a crash-recoverable macOS `run-once` paper-trading runtime for an immutable 10-CN/10-US universe, with durable market/run/order/ledger state, bounded DeepSeek decisions, Keychain secret injection, local reports, and a user LaunchAgent.

**Architecture:** Add a new `stock_agent.runtime` package rather than making the historical backtest runner stateful. One SQLite WAL database owns coordination, claims, attempts, budgets, orders, executions, and append-only ledger events so fill and accounting commit atomically; existing DuckDB stores remain the PIT and LLM audit authorities. Deterministic identities and replay repair only the remaining cross-store decision/runtime windows without claiming cross-database ACID.

**Tech Stack:** Python 3.11, Pydantic 2, sqlite3, DuckDB, Typer, macOS `security`/`launchctl`/`plutil`, pytest, Ruff.

**Design source:** `docs/designs/2026-08-12-unattended-dual-market-paper-runtime-design.md`

**Development worktree:** `/Users/amezf/my-stock-agent-unattended-runtime`

---

## Global implementation rules

- Follow RED → focused GREEN → refactor → full verification for every task.
- Do not read Keychain, call providers, or modify launchd from pytest.
- Do not run live provider calls without an explicit `▶ 即将执行` disclosure and user authorization. Phase 0 authority proofs occur before dependent production code; activation remains a separate final authorization.
- Keep CN and US accounts, calendars, runs, budgets, and failures independent.
- Never encode provider names or credentials into domain decision models.
- Every durable identity is idempotent only for byte-identical canonical content; different content under the same identity is a conflict.
- A failed model/data/runtime attempt is not a HOLD decision.
- Do not merge or push protected `main` without explicit user authorization.

## Verification commands used after every committed capability

```bash
uv run pytest -q
uv run ruff check .
git diff --check
```

Before push or completion, also run the repository credential scan used by `scripts/verify_strategy_a_real_data.py` and confirm `git status --short` is clean.

---

### Task 1: Freeze the versioned deployment profile and universe

**Objective:** Introduce immutable runtime configuration, account profiles, provider/model profile, and `dual-market-20/v1` without network or secret authority.

**Files:**
- Create: `src/stock_agent/runtime/__init__.py`
- Create: `src/stock_agent/runtime/models.py`
- Create: `src/stock_agent/runtime/universe.py`
- Create: `config/paper-runtime-v1.toml`
- Test: `tests/runtime/test_universe_and_config.py`

**RED tests:**

- exact immutable universe has 10 canonical CN and 10 canonical US symbols;
- canonical digest is independent of declaration/container order but emitted order is stable;
- duplicate symbol/market, unsupported symbol, missing sector/currency, or polluted nested model fails;
- CN and US accounts bind exact native currency and approved initial cash;
- deployment profile fixes Strategy A config, DeepSeek `deepseek-v4-pro`, 1024 max tokens, Keychain service names, provider IDs, universe digest, and schedule versions;
- runtime configuration contains no credential value or arbitrary provider URL;
- editing v1 content without changing its expected digest fails.

**Minimal implementation:**

Create strict Pydantic models for `UniverseMember`, `UniverseSnapshot`, `MarketAccountProfile`, `ModelRuntimeProfile`, and `PaperRuntimeConfig`. Load TOML with stdlib `tomllib`, exact-revalidate all nested values, and compare the canonical universe digest with a checked-in literal.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_universe_and_config.py -q
```

**Commit:** `feat: freeze dual-market paper runtime profile`

---

### Task 2: Prove and then freeze authoritative annual market schedules

**Objective:** First prove an authoritative/licensed CN/US calendar source; only a `VALIDATED` proof may produce immutable aware schedules with DST, half-day, cross-year, and provenance semantics.

**Files:**
- Create: `spikes/001-authoritative-calendar/README.md`
- Create: `spikes/001-authoritative-calendar/probe.py`
- Create after `VALIDATED`: `src/stock_agent/runtime/calendars.py`
- Create after `VALIDATED`: `config/calendars/cn-sse-szse-2026.json`
- Create after `VALIDATED`: `config/calendars/us-nyse-nasdaq-2026.json`
- Test: `tests/runtime/test_calendars.py`

**RED tests:**

- schedules contain exact plain session dates and aware open/close instants;
- US DST UTC offsets change correctly and at least one approved half-day closes early;
- CN/US closure dates are explicit rather than weekday-derived;
- schedule rows sort strictly with no duplicates/overlaps;
- runtime schedule can create 10 per-symbol requests without duplicating calendar data;
- schedule digest/provenance mismatch fails closed;
- date outside published schedule produces a stable calendar-not-covered result and zero provider/Keychain calls.

**Minimal implementation:**

Run the bounded proof with user-authorized network access and record `VALIDATED`, `PARTIAL`, or `INVALIDATED`, source/licensing/install policy, and exact evidence. Stop dependent work on non-validated authority. After validation, create a strict schedule loader and a `RuntimeMarketSchedule` projection over existing schedule semantics. Keep annual files immutable and source-attributed; require adjacent approved versions for cross-year next-session resolution.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_calendars.py -q
```

**Commit:** `feat: add versioned dual-market runtime calendars`

---

### Task 3: Build the first durable run-claim tracer bullet

**Objective:** Prove `discover → claim → crash/reopen → resume/terminal idempotency` before connecting market data or LLMs.

**Files:**
- Create: `src/stock_agent/runtime/store.py`
- Create: `src/stock_agent/runtime/identities.py`
- Create: `src/stock_agent/runtime/state.py`
- Test: `tests/runtime/test_run_claim_and_recovery.py`

**RED tests:**

- deterministic decision-cycle run ID excludes phase, PID, wake time, hostname, and attempt number;
- first `BEGIN IMMEDIATE` claim creates one run and one immutable attempt;
- concurrent identical claims produce one run authority;
- same run/config returns the existing terminal result;
- same run key with different config digest is a conflict;
- active unexpired lease blocks a second worker;
- expired lease creates exactly one recovery attempt;
- process close/reopen preserves all state;
- invalid transition and mutation of terminal history fail without partial writes;
- SQLite enables WAL, foreign keys, busy timeout, and user-only file permissions;
- kill switches have independent GLOBAL/MARKET/ACCOUNT scopes and one account corruption does not block the other market.

**Minimal implementation:**

Create normalized `RuntimeRun`, `RuntimeAttempt`, and state enums. Implement a SQLite repository with explicit transactions, unique constraints, immutable attempt rows, monotonic transitions, leases, and stable sanitized errors.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_run_claim_and_recovery.py -q
```

**Commit:** `feat: add durable runtime claims and recovery attempts`

---

### Task 4: Add durable provider budgets and per-symbol ingestion recovery

**Objective:** Refresh 10 symbols per market with durable request budgets, retry/circuit rules, and per-symbol isolation while reusing existing providers/ingestor.

**Files:**
- Create: `src/stock_agent/runtime/market_data.py`
- Modify: `src/stock_agent/runtime/store.py`
- Test: `tests/runtime/test_market_data_orchestration.py`
- Test: `tests/runtime/test_provider_budgets.py`

**RED tests:**

- one US session consumes at most 10 normal Alpha Vantage requests;
- completed symbol attempts are not requested again after restart;
- one allowed retry consumes durable recovery budget and survives restart;
- five Alpha Vantage requests remain unavailable to automation;
- `THROTTLED` opens a durable same-day circuit and stops remaining calls;
- permanent errors never retry;
- CN uses bounded serial/low-concurrency attempts and one retry maximum;
- one symbol failure preserves other successful ingestions;
- each symbol's ingestion remains all-or-nothing;
- retry/recovery never reads unrelated secrets and reports only safe error codes.

**Minimal implementation:**

Add `provider_day_budgets` and `symbol_fetch_attempts` tables. Implement an injected `MarketDataBatchOrchestrator` whose provider factory is called only after a budget claim. Preserve existing `IncrementalBarIngestor` as the single-symbol transaction boundary.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_market_data_orchestration.py tests/runtime/test_provider_budgets.py -q
```

**Commit:** `feat: orchestrate bounded multi-symbol market refresh`

---

### Task 5: Freeze complete market snapshots

**Objective:** Require 10/10 warm coverage and persist an immutable revision manifest before any model call.

**Files:**
- Create: `src/stock_agent/runtime/snapshots.py`
- Modify: `src/stock_agent/runtime/store.py`
- Test: `tests/runtime/test_snapshot_coverage.py`

**RED tests:**

- 10/10 with the exact Strategy A warm-up grid freezes one canonical manifest;
- 9/10 is `DATA_INCOMPLETE` and causes zero LLM/Keychain calls;
- missing current close for a held symbol halts account progression;
- reordered equivalent revisions produce the same manifest digest;
- selected revision content/provenance changes under a frozen run causes conflict;
- future-visible or post-cutoff revision is rejected;
- frozen manifest survives process restart and rebuilds byte-identical `MarketSnapshot`;
- CN and US manifests cannot be mixed.

**Minimal implementation:**

Build a coverage matrix from `PointInTimeStore.latest_bar_revision_as_of()`, canonicalize symbol/session/revision order, persist the manifest and digest, and reconstruct the exact Strategy context from it.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_snapshot_coverage.py -q
```

**Commit:** `feat: freeze complete runtime market snapshots`

---

### Task 6: Connect bounded decision with ambiguous-invocation recovery

**Objective:** Produce exactly one market-level 10-symbol canonical decision or a durable no-order failure, with zero automatic recall after an ambiguous crash.

**Files:**
- Create: `src/stock_agent/runtime/decision.py`
- Modify: `src/stock_agent/runtime/store.py`
- Modify: `src/stock_agent/strategies/llm_provider.py` only if a provider-neutral invocation boundary hook is required
- Test: `tests/runtime/test_decision_recovery.py`

**RED tests:**

- one complete 10-candidate request uses canonical symbol order;
- missing/extra/duplicate/reordered/out-of-envelope selections reject atomically;
- Alpha Vantage and DeepSeek use separate credential lifecycle tests;
- `CREDENTIAL_READY` follows a successful timeout-bounded credential read;
- `SEND_INTENT_RECORDED` persists immediately before the transport send boundary;
- credential/proven pre-send failure is distinct from ambiguity;
- crash after send intent and possible send before canonical decision transitions to `NEEDS_RECONCILIATION` and never automatically invokes again;
- operator-only `reconcile abandon` appends an audit record and transitions to `ABANDONED_NO_ORDER`; it never imports unverified output or retries;
- existing canonical decision by request fingerprint resumes with zero transport/Keychain calls;
- decision-persistence failure emits no intents;
- failed invocation is not HOLD and creates no order;
- model/prompt/profile identity must match the versioned deployment profile.

**Minimal implementation:**

Add a runtime decision coordinator around existing candidate/request construction and `RecordedLLMDecisionProvider`. Persist the invocation marker in runtime SQLite, query the LLM journal by fingerprint before Keychain access, and use replay after decision commit.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_decision_recovery.py -q
```

**Commit:** `feat: add crash-safe bounded decision coordination`

---

### Task 7: Persist batch risk results and the exact pending-order set

**Objective:** Pass the complete market intent tuple through shared risk and atomically persist deterministic next-open paper orders.

**Files:**
- Create: `src/stock_agent/runtime/orders.py`
- Modify: `src/stock_agent/runtime/store.py`
- Test: `tests/runtime/test_risk_and_orders.py`

**RED tests:**

- risk uses one `evaluate_many()` call with independent CN/US account context;
- daily cash, sector, holding-count, and drawdown projection match existing scalar/batch contracts;
- risk rejection is an audited normal result, not runtime failure;
- complete expected order set commits atomically;
- crash during insertion rolls back the set;
- restart after commit verifies exact order-set digest and repairs run terminal state;
- identical deterministic order reappend is idempotent;
- same order ID with different content is conflict;
- T close orders target the next explicit calendar session only;
- no failed/data-incomplete/ambiguous run produces an order.

**Minimal implementation:**

Persist risk result envelopes and orders in one runtime SQLite transaction. Derive IDs from run/candidate/action/approved target/ordinal and store an exact set digest.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_risk_and_orders.py -q
```

**Commit:** `feat: persist audited risk and pending paper orders`

---

### Task 8: Add durable append-only ledger authority

**Objective:** Preserve cash, lots, positions, NAV, and realized P&L across restart using existing ledger event/replay semantics.

**Files:**
- Create: `src/stock_agent/runtime/ledger_store.py`
- Modify: `src/stock_agent/account/ledger.py` only to expose a safe exact replay constructor if required
- Test: `tests/runtime/test_durable_ledger.py`

**RED tests:**

- account initializes cash exactly once;
- event envelopes and indexed IDs cross-validate on every read/list;
- identical event reappend is idempotent, conflicting content is rejected;
- close/reopen rebuilds byte-identical events, lots, positions, cash, NAV, peak NAV, and realized P&L;
- A-share acquisition session survives restart for T+1;
- append batch is atomic;
- corrupted payload or row-key mismatch fails closed;
- CN and US event streams cannot cross accounts/markets/currencies;
- hostile Decimal context does not alter replay.

**Minimal implementation:**

Persist canonical ledger event JSON in the same runtime SQLite database that owns executions. Revalidate exact event types on write/read and rebuild `PortfolioLedger` by deterministic replay. A single transaction commits execution digest, ledger batch, and finalized state; no fill is visible without its events. Store and validate `intended_open_at`, `effective_at`, and `recorded_at`, and process unresolved obligations strictly by effective time.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_durable_ledger.py -q
```

**Commit:** `feat: add durable append-only paper ledger`

---

### Task 9: Prove and admit execution and corporate-action authorities

**Objective:** Prove real source contracts for execution-visible open observations, CN suspension/price-limit state, and corporate actions before enabling fills or position-bearing activation.

**Files:**
- Create: `spikes/002-cn-open-session-authority/README.md`
- Create: `spikes/002-cn-open-session-authority/probe.py`
- Create: `spikes/003-us-open-authority/README.md`
- Create: `spikes/003-us-open-authority/probe.py`
- Create: `spikes/004-corporate-actions/README.md`
- Create: `spikes/004-corporate-actions/probe.py`
- Create after `VALIDATED`: `src/stock_agent/runtime/execution_data.py`
- Create after `VALIDATED`: `src/stock_agent/runtime/corporate_actions.py`
- Create: `tests/fixtures/runtime/execution-data/`
- Create: `tests/fixtures/runtime/corporate-actions/`
- Test: `tests/runtime/test_execution_data_authority.py`
- Test: `tests/runtime/test_corporate_action_authority.py`

**RED tests:**

- a close-published daily row cannot construct an execution-open observation;
- observation requires source ID, provider record ID, observed/available/ingested timestamps, intended market/session/symbol, and canonical digest;
- CN execution state explicitly represents suspension and price-limit blocking;
- missing/stale/mismatched data produces `EXECUTION_DATA_MISSING`, never a fill;
- corrections append as revisions and frozen execution selection is deterministic;
- recorded real provider fixtures preserve exact normalized source rows and whole-response provenance;
- corporate-action schema distinguishes ex/effective/available/ingested instants and append-only revisions;
- split/bonus, cash dividend, symbol change, merger, and delisting events transform ledger state before same-effective-time valuation/execution;
- missing corporate-action authority blocks position-bearing activation; price-jump heuristics cannot authorize repair.

**Implementation gate:**

Research provider documentation and execute separately authorized, bounded live proofs only after the user is told about network/credential implications and request budgets. Each proof records `VALIDATED`, `PARTIAL`, or `INVALIDATED` and the exact schema/source/timestamp/revision/expiry contract. If no trustworthy bounded source is available, stop dependent production work: keep fills disabled and block position-bearing activation. Do not synthesize session or corporate-action state from OHLCV.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_execution_data_authority.py tests/runtime/test_corporate_action_authority.py -q
```

**Commit:** `feat: admit forward execution data authority`

---

### Task 10: Execute pending orders and book ledger events idempotently

**Objective:** Process due orders at their intended open, persist terminal execution, and book one replayable ledger valuation boundary without duplicate fills.

**Files:**
- Create: `src/stock_agent/runtime/execution.py`
- Modify: `src/stock_agent/runtime/store.py`
- Modify: `src/stock_agent/runtime/ledger_store.py`
- Test: `tests/runtime/test_execution_and_booking.py`

**RED tests:**

- only orders persisted before the intended open are eligible;
- admitted open/session state drives existing market rules and cash checks;
- CN 100-share buy lot, T+1 sell, suspension, and price-limit rules survive restart;
- US naked sell remains rejected;
- same execution ID cannot fill twice after crash/restart;
- successful fills and complete marks book one atomic open batch;
- execution and matching ledger batch commit atomically; no intermediate visible FILLED state exists;
- same execution/event identity with a different canonical payload halts the account;
- unaffordable or unavailable execution is terminally rejected without ledger mutation;
- missed wake can book a pre-existing order from admitted historical open authority but cannot backfill a late-created order.

**Minimal implementation:**

Use the existing simulator's pure matching/rule logic behind a durable adapter. In one `runtime.sqlite` transaction, persist the canonical execution payload/digest, append the complete deterministic ledger event batch, and transition execution to its terminal finalized state. Any failure rolls back the entire transaction. Recovery may idempotently retry that whole transaction only when every identity and canonical digest matches; it never exposes or repairs a terminal execution without its ledger events.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_execution_and_booking.py -q
```

**Commit:** `feat: execute and book durable paper orders`

---

### Task 11: Compose the market `run-once` state machine

**Objective:** Wire discovery, execution, market refresh, snapshot, decision, risk/orders, and terminality into one injected, testable process.

**Files:**
- Create: `src/stock_agent/runtime/orchestrator.py`
- Create: `src/stock_agent/runtime/clock.py`
- Test: `tests/runtime/test_run_once_orchestration.py`
- Test: `tests/runtime/test_dual_market_isolation.py`

**RED tests:**

- non-session, too-early, terminal duplicate, and missed-deadline paths perform zero unnecessary provider/LLM/Keychain calls;
- each market advances chronologically and independently;
- one wake processes at most one missing session per market;
- CN failure does not block US and vice versa;
- prior pending execution is separately committed from current close decision failure;
- missed close may run only before next explicit open;
- post-open missed decision emits `MISSED_DECISION_DEADLINE` with no LLM/order;
- all stage transitions consume exact digest-matched prior artifacts;
- crash injection at every documented boundary produces the specified recovery state;
- kill switch blocks new decisions while preserving read/report/status paths.

**Minimal implementation:**

Implement a `RunOnceOrchestrator` with injected clock, stores, provider factories, secret sources, and report sink. Keep CN/US orchestration serial at the top level initially; independence comes from separate transactions/state, not threads.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_run_once_orchestration.py tests/runtime/test_dual_market_isolation.py -q
```

**Commit:** `feat: compose unattended dual-market run once`

---

### Task 12: Publish deterministic local reports

**Objective:** Rebuild safe JSON/Markdown reports from committed state and publish atomically.

**Files:**
- Create: `src/stock_agent/runtime/reports.py`
- Test: `tests/runtime/test_reports.py`

**RED tests:**

- report contains run/config/universe/calendar/source/decision/risk/order/execution/ledger identities;
- CNY and USD reports remain separate;
- report rebuilt after restart is byte-identical for the same committed state;
- temp write, fsync, and atomic rename prevent partial report visibility;
- interrupted publication repairs without changing report ID;
- reports exclude secret canaries, authorization headers, raw bodies, traceback locals, and secret-bearing URLs;
- terminal failure has safe normalized classification and never fabricates HOLD/fill.

**Minimal implementation:**

Generate canonical JSON first, derive Markdown from the same validated report model, and atomically publish under the user application-support report directory.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_reports.py -q
```

**Commit:** `feat: publish deterministic paper runtime reports`

---

### Task 13: Add last-moment macOS Keychain sources

**Objective:** Read only approved items at the last possible invocation boundary and prove all non-live paths have zero Keychain access.

**Files:**
- Create: `src/stock_agent/runtime/keychain.py`
- Test: `tests/runtime/test_keychain_boundary.py`

**RED tests:**

- source uses fixed `security find-generic-password -a <account> -s <service> -w` argument vector without a shell and enforces a bounded timeout;
- only approved DeepSeek and Alpha Vantage services are addressable;
- secret never appears in repr/str/argv/error/log/report;
- subprocess nonzero and malformed secret become stable errors without stderr leakage;
- Alpha Vantage is zero-access before a claimed provider budget/symbol attempt and after a successful prior fetch;
- DeepSeek is zero-access before complete frozen coverage/request revalidation and on existing or ambiguous decision recovery;
- help, config load, dry-run, replay, non-session, too-early, missed-deadline, incomplete-data, and duplicate-run call both fake Keychain sources zero times;
- live provider construction alone does not read Keychain; invocation does;
- no code enumerates Keychain or uses `-A`.

**Minimal implementation:**

Implement an injected `KeychainSecretSource` returning an opaque short-lived value. Keep subprocess output in a bounded private variable and clear references after provider invocation.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_keychain_boundary.py -q
```

**Commit:** `feat: add invocation-only Keychain secret sources`

---

### Task 14: Add operational CLI and process lock

**Objective:** Provide safe `run-once`, `status`, `report`, `dry-run`, `pause`, and `resume` commands with an OS-level duplicate-process guard.

**Files:**
- Create: `src/stock_agent/runtime/cli.py`
- Create: `src/stock_agent/runtime/lock.py`
- Modify: `pyproject.toml` to register `stock-agent`
- Test: `tests/runtime/test_cli.py`
- Test: `tests/runtime/test_process_lock.py`

**RED tests:**

- `--help`, `status`, and `dry-run` are zero-network/zero-Keychain;
- invalid arguments never echo supplied values;
- runtime paths are absolute, user-owned, non-symlink, and not group/world accessible;
- lock excludes concurrent local processes but DB claim remains final authority;
- stale process exit releases lock without deleting another process's authority;
- stdout/stderr contain one bounded safe result envelope;
- nonzero exit codes distinguish no-work, retryable failure, reconciliation, kill switch, and internal corruption without leaking details.

**Minimal implementation:**

Use Typer for explicit commands and a no-shell wrapper. Use a user-only advisory file lock with symlink rejection. Keep command construction separate from runtime composition.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_cli.py tests/runtime/test_process_lock.py -q
```

**Commit:** `feat: add safe paper runtime operations CLI`

---

### Task 15: Generate and manage a secret-free LaunchAgent

**Objective:** Install a validated user LaunchAgent that only wakes `run-once` and never owns business semantics.

**Files:**
- Create: `src/stock_agent/runtime/launchd.py`
- Create: `config/launchd/com.dewforest.my-stock-agent.paper-runtime.plist.template`
- Create: `scripts/install_paper_runtime_launchagent.py`
- Test: `tests/runtime/test_launchd.py`
- Create: `docs/runbooks/macos-paper-runtime.md`

**RED tests:**

- generated plist contains absolute executable/config/log paths and no secret/environment credential;
- plist uses `StartCalendarInterval`, `ProcessType=Background`, and no `KeepAlive` loop;
- multiple wake entries are allowed but business scheduling remains runtime-owned;
- output passes `plutil -lint` in a temporary directory;
- install/status/pause/resume/uninstall command vectors use the user GUI domain and no shell;
- pytest never calls real `launchctl` or writes `~/Library/LaunchAgents`;
- installer rejects symlink, non-regular file, wrong owner/mode, and secret-shaped content for config, wrapper, plist, stores, locks, logs, and report paths;
- generated plist sets `Umask=0077` and an explicit absolute working directory;
- logs are user-only and size-bounded/rotated;
- a separately authorized GUI-domain probe proves noninteractive timeout-bounded access to only the named Keychain items before activation;
- runbook documents login-session/Keychain limitation, scoped kill switches, reports, recovery, and uninstall.

**Minimal implementation:**

Generate plist via `plistlib`, validate with `plutil`, and require explicit CLI installation. Before activation, create and run `spikes/005-launchagent-keychain/` with explicit user authorization; record `VALIDATED`, `PARTIAL`, or `INVALIDATED`. Only `VALIDATED` permits installation. Keep installation idempotent and reversible.

**Focused verification:**

```bash
uv run pytest tests/runtime/test_launchd.py -q
```

**Commit:** `feat: add macOS paper runtime LaunchAgent`

---

### Task 16: Execute the offline crash/restart acceptance matrix

**Objective:** Prove the full capability with frozen providers, fake Keychain, injected clocks, process restarts, and no external effects.

**Files:**
- Create: `tests/runtime/test_unattended_vertical.py`
- Create: `tests/runtime/test_crash_matrix.py`
- Create: `tests/fixtures/runtime/cn/`
- Create: `tests/fixtures/runtime/us/`
- Create: `scripts/verify_unattended_runtime_offline.py`

**RED/acceptance scenarios:**

- CN and US both discover, ingest 10/10, decide, risk, and persist next-open orders;
- process restart executes due orders exactly once and rebuilds both ledgers;
- one market throttled/data-incomplete/decision-failed while the other succeeds;
- every crash point from the design matrix resumes to the exact permitted state;
- ambiguous invocation never recalls automatically;
- missed wake before/after next open follows the deadline rule;
- reports and state digests are deterministic across independent equivalent runs;
- credential canaries do not appear in tracked/untracked files, SQLite/DuckDB text exports, reports, stdout/stderr, or exception graphs.

**Verification:**

```bash
uv run python scripts/verify_unattended_runtime_offline.py
uv run pytest -q
uv run ruff check .
git diff --check
```

**Commit:** `test: prove unattended runtime crash recovery offline`

---

### Task 17: Independent capability reviews

**Objective:** Run one specification review and one quality/security review over the complete staged capability.

**Actions:**

1. Stage the intended commit set or use intent-to-add so new files are visible.
2. Run repository credential scan over tracked and untracked nonignored files.
3. Dispatch specification review against the approved design and this plan.
4. Fix only confirmed Critical/Important findings with regression-first TDD.
5. Dispatch one quality/security review focusing on time leakage, duplicate execution, SQLite/DuckDB recovery, Keychain secrecy, launchd behavior, and financial replay.
6. Re-review only the exact blocker surface after fixes.

**Fresh verification:**

```bash
uv run pytest -q
uv run ruff check .
git diff --check
git status --short
```

**Commit:** `fix: close unattended runtime review findings` only if fixes are required.

---

### Task 18: Explicit local live proof and activation

**Objective:** With user authorization, verify real Keychain-backed providers and install the LaunchAgent without exposing secrets or enabling real brokerage authority.

**Preconditions:**

- all offline acceptance and reviews pass;
- worktree is clean;
- user is shown the exact operations, network calls, expected API usage, paths, and rollback;
- user explicitly authorizes Keychain reads, real provider calls, and LaunchAgent installation.

**Actions:**

1. `stock-agent dry-run` proves zero Keychain/network access.
2. Validate only the named DeepSeek and Alpha Vantage Keychain items.
3. Execute one bounded US and one bounded CN market-data refresh.
4. Execute one bounded `deepseek-v4-pro` decision only if an eligible frozen session exists; otherwise use the existing verified transport evidence and do not fabricate eligibility.
5. Verify runtime DB, PIT store, LLM journal, ledger store, and reports contain no secret.
6. Generate and lint the LaunchAgent plist.
7. Show the user exact installation path and schedule.
8. Install only after explicit final installation authorization.
9. Run one manual `launchctl kickstart` and verify safe terminal/no-work behavior or a real eligible paper run.
10. Document pause/uninstall rollback.

**No real broker adapter or real order is introduced.**

**Final verification and remote milestone:**

```bash
uv run pytest -q
uv run ruff check .
git diff --check
git status --short
```

Push the feature branch as a reviewed milestone. Create a stacked PR against the transport branch unless that branch has already merged to `main`. Merge requires separate explicit user authorization.
