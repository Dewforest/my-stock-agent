# Unattended Dual-Market Paper Runtime Design

Date: 2026-08-12
Status: Approved direction; activation is gated by the Phase 0 authority proofs in §3.3

## 1. Goal

Build a macOS user-session runtime that runs Strategy A unattended for a fixed, versioned universe of 10 China A-shares and 10 US equities. The runtime refreshes point-in-time market data, obtains one bounded LLM decision per eligible market session, passes all intents through the shared `RiskEngine`, persists next-open paper orders, simulates execution, replays an append-only ledger, and publishes auditable local reports.

The runtime is paper-only. It has no broker authority and must never place a real order.

## 2. Approved deployment and product boundary

- Host: the user's Mac.
- Scheduler: a user-level `LaunchAgent` using `StartCalendarInterval`.
- Runtime entry: one bounded `stock-agent run-once` process per wake-up.
- Secrets: macOS login Keychain, read only immediately before a required live provider invocation.
- LLM: DeepSeek through the provider-neutral OpenAI-compatible adapter.
- Model profile: `deepseek-v4-pro`, `max_tokens=1024`.
- US daily data: Alpha Vantage.
- CN daily data: Eastmoney.
- Account isolation:
  - CN: CNY 1,000,000.
  - US: USD 100,000.
- The Mac is normally logged in and online. Wake-after-sleep catch-up is supported.
- No FX conversion, cross-market cash transfer, or combined global NAV.

“Unattended” means unattended while the user login session and login Keychain are available. It does not mean execution before login after a FileVault cold boot.

## 3. Existing components and explicit gaps

### 3.1 Reused components

The implementation reuses, rather than forks:

- `PointInTimeStore` and `IncrementalBarIngestor` for durable market revisions and PIT selection.
- `EastmoneyDailyBarProvider` and `AlphaVantageDailyBarProvider` for normalized raw daily OHLCV.
- `BoundedLLMStrategyA`, deterministic candidate construction, and action-to-target ownership.
- `RecordedLLMDecisionProvider`, `ReplayLLMDecisionProvider`, and `LLMDecisionJournal`.
- `OpenAICompatibleChatTransport` and `BoundedBearerHttpsClient`.
- `RiskEngine.assess_portfolio()` and `RiskEngine.evaluate_many()`.
- `plan_orders()` and deterministic order identity semantics.
- execution market rules and simulator matching logic.
- ledger event models and `PortfolioLedger` replay/accounting semantics.

### 3.2 Gaps this capability must close

The current repository has no forward `run-once` composition root. The backtest runner creates fresh in-memory ledger and simulator state per run and its run registry is process-local. The current execution pending queue, watermarks, used order IDs, and ledger events are not durable.

This capability therefore adds:

- a versioned universe registry;
- full-year authoritative market schedules;
- a durable runtime state store;
- append-only durable ledger event storage;
- durable order/execution state;
- deterministic run claims and recovery attempts;
- market-data request budgets and per-symbol fetch state;
- a production `run-once` entry;
- atomic reports;
- a user-level LaunchAgent installer and runbook.

The forward runtime is a new orchestration boundary. `ChronologicalBacktestRunner` must not be stretched into a stateful daemon.

### 3.3 Phase 0 authority gates

Production implementation starts with four bounded evidence tasks whose results become normative amendments to this design:

1. authoritative CN and US annual calendars, including provenance, redistribution/install policy, DST, half days, and cross-year next-session resolution;
2. execution-visible CN/US open observations plus authoritative CN suspension and price-limit state;
3. corporate-action authority for splits, dividends, symbol changes, mergers, and delistings;
4. noninteractive access to the two approved Keychain items from the installed GUI-domain LaunchAgent.

Each completed proof ends in `VALIDATED`, `PARTIAL`, or `INVALIDATED`. A production-authorizing `VALIDATED` proof must record the exact source/profile/schema/budget/timestamp/license contract and offline source artifacts with whole-response provenance. A research report or report digest without those source artifacts freezes the investigation result but does not close the authority gate. `PARTIAL` or `INVALIDATED` blocks only the dependent production authority; it cannot be bypassed with inferred weekdays, close-published daily opens, OHLCV-derived session state, guessed corporate actions, or interactive Keychain prompts.

The runtime may be developed offline through audited decision and pending-order persistence while an execution gate remains blocked. Automatic fills and LaunchAgent activation are not accepted until their corresponding Phase 0 proof is `VALIDATED` and this document is amended with the selected source contract.

Phase 0 evidence recorded on 2026-08-12:

- annual calendars: `PARTIAL`; official pages were manually cross-checked for 2026 schedule facts, including NYSE/Nasdaq half days and DST, but complete source-artifact closure, derived-data install/redistribution policy, and adjacent CN/US approved versions are unavailable;
- execution open/session state: `PARTIAL`, with CN price-limit inference explicitly `INVALIDATED`; neither market may auto-fill from the tested public web endpoints;
- corporate actions: official legal-disclosure source classes were identified, but there is no source-artifact manifest or complete standardized market-treatment feed; the result remains `PARTIAL` and position-bearing activation stays blocked;
- LaunchAgent Keychain ACL: not yet executed and remains an activation gate.

Versioned investigation evidence:

- `docs/verification/2026-08-12-cn-us-annual-calendar-authority.md`;
- `docs/verification/2026-08-12-cn-us-open-execution-and-cn-session-state-authority.md`;
- `docs/verification/2026-08-12-cn-us-corporate-action-authority.md`;
- raw execution candidate fixtures and manifest under `docs/verification/execution-authority-2026-08-12/`.

Only the execution-candidate probe currently has whole-response fixtures. The calendar and corporate-action reports are research summaries whose report hashes prevent silent mutation but do not prove source content. These verdicts allow implementation of durable runtime state, audit/decision paths, and capability gates. They do not authorize a production calendar marked `OFFICIAL`, automatic fills, automatic position holding, or LaunchAgent activation.

## 4. Architecture

```text
macOS LaunchAgent
  -> stock-agent run-once --config <absolute runtime.toml>
       -> acquire local process lock
       -> open runtime state store
       -> validate config, permissions, and kill switch
       -> discover eligible work independently for CN and US
       -> claim deterministic market/session run
       -> process due pending orders from prior sessions
       -> refresh per-symbol market data
       -> verify fixed-universe coverage
       -> freeze PIT revision manifest
       -> build deterministic Strategy A candidates
       -> read DeepSeek Keychain item only if no decision exists
       -> invoke bounded LLM and journal canonical decision
       -> derive immutable intents
       -> batch RiskEngine evaluation
       -> persist deterministic next-open paper orders
       -> append ledger events
       -> terminal run transition
       -> atomically publish JSON and Markdown reports
```

CN and US are independent run domains. One market's failure must not block discovery or progress for the other.

## 5. Universe v1

Universe ID: `dual-market-20/v1`.

Every entry records market, canonical symbol, display name, currency, sector, inclusion reason, effective date, and source/provenance. The canonical universe payload has a tagged digest. Runs reference the immutable universe ID and digest. Changes require a new version; v1 is never edited in place.

### 5.1 CN

| Symbol | Name | Sector |
|---|---|---|
| 600519 | 贵州茅台 | Consumer Staples |
| 601318 | 中国平安 | Financials |
| 600036 | 招商银行 | Financials |
| 600276 | 恒瑞医药 | Health Care |
| 600900 | 长江电力 | Utilities |
| 601088 | 中国神华 | Energy |
| 600030 | 中信证券 | Financials |
| 000333 | 美的集团 | Consumer Discretionary |
| 000858 | 五粮液 | Consumer Staples |
| 300750 | 宁德时代 | Industrials |

### 5.2 US

| Symbol | Name | Sector |
|---|---|---|
| AAPL | Apple | Technology |
| MSFT | Microsoft | Technology |
| GOOGL | Alphabet | Communication Services |
| AMZN | Amazon | Consumer Discretionary |
| META | Meta Platforms | Communication Services |
| NVDA | NVIDIA | Technology |
| JPM | JPMorgan Chase | Financials |
| XOM | Exxon Mobil | Energy |
| JNJ | Johnson & Johnson | Health Care |
| PG | Procter & Gamble | Consumer Staples |

The runtime does not allow the LLM to add, remove, or replace symbols.

## 6. Calendars and phases

Two immutable annual schedule versions are required:

- `cn-sse-szse-YYYY/vN`, timezone `Asia/Shanghai`;
- `us-nyse-nasdaq-YYYY/vN`, timezone `America/New_York`.

Schedules explicitly list aware open and close instants. US schedules encode DST and half days; weekdays are not inferred as sessions. A run binds the exact schedule version and row provenance.

Per market/session phases:

1. `SESSION_GATE`
2. `OPEN_EXECUTION`
3. `CLOSE_WAIT`
4. `FETCH`
5. `INGEST`
6. `COVERAGE_GATE`
7. `SNAPSHOT_FREEZE`
8. `LLM_DECIDE`
9. `RISK_BATCH`
10. `PLAN_AND_PERSIST`
11. `REPORT`

The close decision is eligible only after the authoritative close plus a configured data-ready grace period. A T close decision may only create orders for the next explicit session open.

## 7. Market data and request budgets

### 7.1 Per-symbol orchestration

Existing providers and ingestors remain single-symbol boundaries. The runtime orchestrates 10 independent symbol attempts per market. Each symbol attempt is independently durable and each symbol ingestion retains its existing atomic batch semantics.

A successful symbol is not fetched again during recovery for the same market/session/range. Failure of one symbol does not roll back successful symbols.

### 7.2 US Alpha Vantage

The free quota is treated as 25 requests per natural day:

- normal market batch: at most 10 requests;
- one bounded recovery batch: at most 10 additional requests;
- five requests remain reserved and cannot be spent automatically.

Each successful symbol is requested at most once per intended session. `THROTTLED` opens a provider circuit for the rest of that natural day. DNS, connect/read timeout, connection reset, and HTTP 5xx may receive one durable delayed retry. Authentication, schema, identity, numeric, and invalid-request failures are not retried.

The runtime requests a bounded five-session overlap and relies on append/revision idempotency. Alpha Vantage compact data is not represented as arbitrary deep-history authority.

### 7.3 CN Eastmoney

CN uses 10 bounded, serial or low-concurrency requests. Retry rules match the normalized retryable error codes and permit at most one delayed retry. Missing rows do not prove suspension or market closure.

### 7.4 Coverage gate

The v1 decision contract requires complete coverage for all 10 symbols in the market and the configured Strategy A warm-up window.

- 10/10 complete: freeze snapshot and continue.
- any candidate symbol incomplete: terminal `DATA_INCOMPLETE`; no new decision and no new order.
- any held symbol lacks a current authoritative close: halt normal account progression and emit a high-severity report.
- v1 does not silently create a partial-universe decision.

## 8. Open execution authority

A close-published daily row's `open` field is not automatically an execution-visible open observation. Forward execution requires a separate persisted observation/provenance boundary proving the open value was available after the open and before booking.

CN execution additionally requires authoritative session state sufficient to apply suspension and price-limit rules. Eastmoney raw daily OHLCV alone does not prove these states.

Therefore:

- pending orders may be persisted before this capability exists;
- no fill is booked without an admitted execution-open observation;
- CN fills also require admitted suspension/price-limit state;
- missing execution authority leaves the order `PENDING` and creates a retryable `EXECUTION_BLOCKED_DATA` obligation, never a fabricated fill;
- OHLCV values must not be used to guess suspension or limit state.

The 2026-08-12 public-endpoint probe did not validate an execution authority. Eastmoney `push2delay` and Nasdaq.com chart JSON are research candidates only: their field semantics, first-availability time, corrections, SLA, limits, and machine-use permission are insufficient. Nasdaq Opening Cross establishes official business semantics but the licensed feed/vendor path for the full US universe is not connected. CN exchange/vendor state for suspension and price limits is also not connected. Consequently automatic fills remain blocked as `EXECUTION_BLOCKED_DATA` until a later normative amendment names a validated product/profile.

This is an implementation acceptance gate, not optional hardening.

## 9. Durable stores

```text
runtime.sqlite
  runs, run_attempts, leases, symbol_fetches, request_budgets,
  snapshot_manifests, orders, executions, append-only ledger events,
  kill switches, reconciliation records, report index

pit.duckdb
  append-only/revisioned market data

llm-journal.duckdb
  invocation attempts, canonical decisions, attestations
```

SQLite runs in WAL mode with foreign keys enabled. Files and parent directories are user-only. Runtime coordination uses small transactional claims; PIT and LLM audit retain their existing DuckDB implementations.

One `runtime.sqlite` transaction is the sole commit authority for an execution, its canonical execution payload digest, the corresponding append-only ledger event batch, and the terminal execution state. Reports consume only finalized rows. This removes the execution/ledger split-brain window.

No ACID claim is made between runtime SQLite, PIT DuckDB, and LLM-journal DuckDB. Recovery uses deterministic identities and committed artifacts:

1. canonical decision commits to LLM journal;
2. runtime resumes by request fingerprint;
3. deterministic order set commits in one runtime SQLite transaction;
4. execution and ledger events commit atomically inside runtime SQLite;
5. run terminality is repaired from committed artifacts after a crash.

Execution rows move `PREPARED -> FINALIZED` in one transaction with their ledger batch. `PREPARED` is a local calculated value not visible as a fill and may be recomputed before commit. There is no externally visible `FILLED` state without the matching ledger events. Equal execution/event identity with a different canonical payload digest halts the account as corruption.

Every execution/ledger envelope distinguishes:

- `intended_open_at`: the market instant at which the pending order was eligible;
- `effective_at`: equal to the admitted intended open instant and used for acquisition-session, T+1, valuation, and chronological ledger replay;
- `recorded_at`: the later wall-clock instant at which the runtime committed the recovered result.

The runtime processes market obligations in `effective_at` order. It cannot advance a market ledger past an unresolved earlier obligation. A delayed result whose `effective_at` is not strictly after the last committed effective event is a stable chronology conflict; it is never appended using `recorded_at` as a substitute business time.

## 10. Deterministic identities

`run_id` identifies one market/account post-close decision cycle and is a tagged hash of:

- schema `paper-run/v1`;
- market and account ID;
- intended close session date;
- strategy ID/config version;
- universe ID/digest;
- calendar version;
- model/prompt policy version.

It excludes phase/state, wake time, PID, hostname, attempt number, credentials, retries, errors, and report generation time. State-machine phases are durable attributes of this one run, not separate business identities. Execution of an order on the next session is identified by `execution_id` and links back to the originating decision `run_id`.

Additional identities:

- request fingerprint: existing frozen candidate/model/prompt contract;
- order ID: run ID, candidate ID, approved action/target, and canonical ordinal;
- execution ID: order ID and intended execution session;
- ledger event ID: execution/run boundary plus canonical event purpose;
- report ID: run ID, terminal state, and committed-state digest.

Database uniqueness protects run keys, request fingerprints, order IDs, execution IDs, and ledger event IDs. Equal identity with different content is corruption/conflict, not idempotency.

## 11. Runtime state machine

```text
DISCOVERED
  -> SKIPPED_NOT_SESSION
  -> SKIPPED_TOO_EARLY
  -> MISSED_DECISION_DEADLINE
  -> CLAIMED
       -> FAILED_BEFORE_DECISION
       -> DATA_INCOMPLETE
       -> SNAPSHOT_FROZEN
            -> CREDENTIAL_READY
                 -> FAILED_DECISION_PRE_SEND
                 -> SEND_INTENT_RECORDED
                      -> FAILED_DECISION_PRE_SEND
                      -> NEEDS_RECONCILIATION
                      -> DECISION_RECORDED
                      -> FAILED_AFTER_DECISION
                      -> ORDERS_PERSISTED
                           -> SUCCEEDED
```

Pending orders and execution obligations have separate durable lifecycles.

```text
PendingOrder:
  PENDING -> FINALIZED_FILLED | FINALIZED_REJECTED | FINALIZED_EXPIRED | FINALIZED_CANCELLED

ExecutionObligation:
  DISCOVERED -> READY -> PREPARED -> FINALIZED_FILLED
                                 -> FINALIZED_REJECTED
                    -> BLOCKED_DATA -> READY

  any nonterminal state -> TERMINATED_EXPIRED
                        -> TERMINATED_CANCELLED
```

Every local process creates an immutable run attempt record. The run's business identity remains stable across attempts.

`ExecutionObligation.BLOCKED_DATA` is not terminal and does not consume or replace `PendingOrder.PENDING`. It records the order ID, intended effective time, missing authority profile/version, first/last attempt time, retry eligibility, and error digest. An approved authority amendment may move the obligation back to `READY` only while the linked order remains `PENDING`, is unexpired, and chronology still permits it.

Retry updates only the obligation in one runtime SQLite transaction and leaves the order pending. Expiry or operator cancellation is legal from `DISCOVERED`, `READY`, `PREPARED`, or `BLOCKED_DATA`, subject to optimistic version and lease ownership checks. Expiry atomically moves the obligation to `TERMINATED_EXPIRED` and the linked order to `FINALIZED_EXPIRED`; operator cancellation atomically moves them to `TERMINATED_CANCELLED` and `FINALIZED_CANCELLED`. A fill/rejection atomically finalizes the obligation, finalizes the order, persists the typed execution result, and commits the matching ledger batch. Any identity/digest mismatch rolls back and halts the account.

Legal terminal pairs are exhaustive:

| Execution obligation | Pending order | Required execution result |
|---|---|---|
| `FINALIZED_FILLED` | `FINALIZED_FILLED` | typed fill payload and non-empty deterministic ledger batch |
| `FINALIZED_REJECTED` | `FINALIZED_REJECTED` | typed rejection reason and the policy-defined zero/non-fill ledger batch |
| `TERMINATED_EXPIRED` | `FINALIZED_EXPIRED` | expiry policy/version and occurrence time; no fill events |
| `TERMINATED_CANCELLED` | `FINALIZED_CANCELLED` | operator identity, reason, and occurrence time; no fill events |

No other terminal pairing is valid. The transaction checks the execution-result discriminator against both terminal states; a mismatch rolls back. `PREPARED` cannot be expired or cancelled while a live execution lease may still send or commit: the transition first proves the lease absent/expired and that no send intent or terminal result exists. An ambiguous external send remains a reconciliation state and is not converted to expiry or cancellation.

The chronology watermark is derived from the earliest unresolved obligation effective time. It advances only after the obligation and linked order reach a mutually valid terminal pair in the same transaction. A blocked obligation prevents later effective-time ledger events for that account, but does not block the other market/account. It may not be deleted or skipped merely to advance NAV.

A failure is never represented as HOLD. It creates no fabricated intent or order and does not mutate the portfolio except for separately committed prior-order execution work.

## 12. Claim, lock, and concurrency

A user-only advisory lock at:

`~/Library/Application Support/MyStockAgent/runtime/run-once.lock`

reduces duplicate processes but is not correctness authority. The runtime SQLite transaction is authoritative:

1. `BEGIN IMMEDIATE`;
2. insert or resolve deterministic run ID;
3. return a prior terminal result idempotently;
4. reject a live, unexpired lease;
5. create a recovery attempt for an expired lease;
6. reject the same run key with a different configuration digest.

The lock path must not follow symlinks and all runtime files use user-only permissions.

## 13. Crash recovery and invocation ambiguity

| Crash point | Recovery | Recall LLM? | Duplicate order? |
|---|---|---:|---:|
| before claim | discover and claim | no | no |
| after claim, before snapshot | new attempt resumes | no | no |
| after snapshot, before invocation | rebuild from frozen manifest | no | no |
| credential failure or proven pre-send failure | terminal pre-send failure | no automatic retry in the same attempt | no |
| after send intent and possible request send, before canonical decision | `NEEDS_RECONCILIATION` | **never automatically** | no |
| after decision, before orders | replay by fingerprint | no | no |
| during order insert | transaction rollback/retry | no | no |
| after orders, before success | verify exact order set and repair terminal state | no | no |
| during execution/ledger commit | recover by execution/event IDs | no | no |
| during report publication | rebuild and atomic rename | no | no |

Keychain access occurs before ambiguity is introduced. After the credential is read and validated, `CREDENTIAL_READY` is persisted. Immediately before the transport send boundary, `SEND_INTENT_RECORDED` is persisted. A credential/ACL/timeout failure or transport failure proven to occur before bytes can leave the process becomes `FAILED_DECISION_PRE_SEND`; only a crash or failure after send intent where non-send cannot be proven becomes `NEEDS_RECONCILIATION`.

`NEEDS_RECONCILIATION` has one v1 exit policy: an operator runs `stock-agent reconcile abandon --run-id ... --reason ...`. This appends an immutable reconciliation record and transitions the run to `ABANDONED_NO_ORDER`. It never imports an unverified provider result, never retries the same decision run, and never produces intents or orders. The command requires an exact run ID, displays only safe identities, and records operator account, UTC occurrence time, reason code, and prior request fingerprint. If the next open has passed, the same terminal policy applies; no historical order is fabricated.

After `DECISION_RECORDED`, recovery reads the existing journal decision and must not access Keychain or the network.

## 14. Sleep and catch-up

`StartCalendarInterval` is used because macOS coalesces missed calendar events and starts the job after wake. The runtime scans durable work; it never assumes one wake equals one session.

- Each market advances independently and chronologically.
- One wake processes at most one missing session per market.
- A pending order persisted before its intended open may be booked later only from admitted execution-open authority for that intended session.
- A missed close decision may be recovered only if the next explicit session open has not occurred.
- Once that open has passed, mark `MISSED_DECISION_DEADLINE`; do not call the LLM and do not backfill an order.
- Historical recovery queries use the intended PIT cutoff and frozen revision manifest; later knowledge cannot enter an older decision.

## 15. Keychain boundaries

Approved services:

- DeepSeek: `com.dewforest.my-stock-agent.deepseek`;
- Alpha Vantage: `com.dewforest.my-stock-agent.alpha-vantage`.

The account defaults to the current macOS user and is explicit in deployment configuration. The runtime never enumerates Keychain.

Alpha Vantage and DeepSeek have separate lifecycles.

An Alpha Vantage Keychain read occurs only for one exact symbol fetch after:

1. config/path permissions, kill switch, and calendar eligibility validate;
2. the deterministic market run is claimed;
3. the provider-day budget and symbol-attempt claim commit;
4. no prior successful fetch exists for that symbol/range;
5. the exact `DailyBarRequest` revalidates;
6. immediately before constructing/invoking the authenticated provider.

It does not wait for market coverage or a frozen manifest, because the fetch creates that coverage.

A DeepSeek Keychain read occurs only after:

1. config and permissions validate;
2. the scoped kill switch is off;
3. calendar eligibility and deterministic run claim succeed;
4. market coverage and frozen manifest succeed;
5. candidate/request exact revalidation succeeds;
6. no canonical decision exists;
7. no prior ambiguous send intent exists;
8. immediately before `CREDENTIAL_READY` and `SEND_INTENT_RECORDED` at the live invocation boundary.

The read value is passed as an opaque in-process dependency and discarded after invocation. It never appears in argv, plist, config, `.env`, reports, request logs, or repository files.

Zero-Keychain paths are tested separately. Alpha Vantage is not read for help, dry-run, replay, non-session, too-early wake, invalid config, exhausted budget, open circuit, or an already successful symbol fetch. DeepSeek is not read for those paths plus missed deadline, incomplete data, terminal duplicate wake, existing/ambiguous decision, and all post-decision recovery.

Every Keychain subprocess has a bounded timeout. Interactive enrollment is separate from runtime. The installed LaunchAgent must pass a noninteractive GUI-domain preflight before activation; timeout, ACL denial, or a prompt requirement fails safely and disables live activation. `-A` is forbidden.

### 15.1 Corporate actions

Raw unadjusted daily OHLCV is insufficient for a long-running account. Phase 0 must select and validate a corporate-action authority before automatic position holding is activated. Canonical events include split/bonus ratio, cash dividend, symbol change, merger/delisting disposition, ex/effective/available/ingested timestamps, source identities, and revision digest. Accepted actions become append-only ledger events and are processed before valuation or execution at the same effective instant.

Until that authority is validated, any position-bearing activation is blocked. A price-jump heuristic is not an authority and cannot silently repair quantity, cost basis, cash, NAV, or P&L.

The 2026-08-12 proof validated SEC and CN statutory/exchange disclosures as legal-fact authorities, but not a complete standardized market-treatment feed. Alpha Vantage and Eastmoney remain discovery/history cross-check sources. Nasdaq/NYSE/FINRA and authorized CN exchange/vendor products require entitlement and licensing validation. This keeps the corporate-action gate `PARTIAL` and position-bearing activation blocked.

## 16. Risk and decision atomicity

Each market sends one complete, canonical 10-candidate request to the bounded LLM. Missing, duplicate, reordered, malformed, or out-of-envelope selections reject the entire market decision. No successful prefix is admitted.

The accepted complete intent tuple is passed once to `RiskEngine.evaluate_many()` so holding slots, sector exposure, and daily cash budget are projected consistently.

Existing hard limits remain authoritative:

- single symbol target up to 15%;
- sector up to 30%;
- at most 10 positions;
- daily new-position budget up to 30% of day-start available cash;
- drawdown 15% blocks new BUY exposure;
- drawdown 20% triggers portfolio reduction assessment;
- missing sector rejects exposure-increasing BUY.

Technology and Financials each contain three universe members, so the 30% sector cap is expected to bind.

## 17. Reports and observability

Every run attempt produces structured state in the runtime DB. Terminal runs publish:

- immutable JSON for machines;
- Markdown for the user.

Reports contain run/config/universe/calendar identities, market/session, symbol coverage, source/revision digests, decision IDs, risk results, order/execution states, ledger/NAV/P&L in native currency, retry/recovery classification, and safe failure codes.

Reports exclude credentials, authorization headers, raw provider bodies, raw model bodies, traceback locals, and secret-bearing URLs. Publication uses write-to-temp, fsync, and atomic rename. A report can be rebuilt entirely from committed stores.

Initial notification is local report plus nonzero LaunchAgent exit/log status. External messaging is a later versioned capability.

## 18. LaunchAgent

The installer generates a user LaunchAgent plist with absolute paths and no secrets. It specifies:

- a stable label;
- `ProgramArguments` for the installed wrapper and config path;
- `StartCalendarInterval` wake times that cover CN and US close-ready windows;
- `ProcessType=Background`;
- bounded stdout/stderr file paths containing only safe runtime output;
- no `KeepAlive` restart loop;
- no shell interpolation;
- no environment secret;
- `Umask=0077` and an explicit absolute working directory.

The wrapper has a fixed PATH and invokes the project/runtime executable by absolute path. Every config, wrapper, plist, SQLite/DuckDB file, lock, log, report directory, temporary report, and final report is checked as the expected owner, regular file/directory, non-symlink, and no broader than the approved user-only mode before use. Logs are size-bounded/rotated and user-only.

Installation validates the plist, bootstraps it into the user GUI domain, performs a timeout-bounded noninteractive Keychain preflight, and provides status, manual run, pause, resume, and uninstall commands. Installation and Keychain enrollment are explicit user operations; pytest never modifies launchd or Keychain.

## 19. Failure policy

- Market-data failure: persist safe symbol/market code; retry only allowed transient classes.
- `THROTTLED`: stop remaining calls for that provider/day.
- Data incomplete: no LLM, no new orders.
- LLM transport/schema/identity failure: no intents, no orders, no automatic recall.
- Decision persistence failure: no executable intents.
- Risk rejection: persist normal audited rejection.
- Missing execution-open/session-state authority: no fill.
- Unaffordable/invalid order at open: terminal paper rejection.
- Ledger invariant or corruption failure: halt the affected account and activate its account-scoped kill switch until repaired.
- CN failure never blocks US and US failure never blocks CN.

Kill switches have explicit `GLOBAL`, `MARKET`, and `ACCOUNT` scopes in runtime SQLite. The most specific affected scope is used by default; only an operator may set or clear `GLOBAL`. A switch blocks new fetch/decision/order work for its scope but permits status, reports, reconciliation, and integrity repair. One account's corruption cannot silently escalate to the other market.

All budgets, leases, retries, grace periods, and deadlines are stored and compared as aware UTC instants. Alpha Vantage daily quota uses `America/New_York` natural-day boundaries `[00:00, next 00:00)`, then stores the derived UTC interval. Cross-year next-session resolution requires adjacent approved schedule versions for every exchange represented by the market profile. For the combined US profile, both NYSE and Nasdaq adjacent versions are mandatory; an NYSE multi-year page alone cannot authorize the transition. Absence or digest conflict yields calendar-not-covered and no order.

## 20. Acceptance tests

The implementation is not complete until tests prove:

1. non-session and too-early wakes do not read Keychain or call providers;
2. same deterministic run is idempotent across process restart;
3. expired lease creates one recovery attempt without a duplicate run;
4. 10/10 coverage is required and held-symbol missing price halts progression;
5. request budgets survive restart and throttling opens a durable circuit;
6. frozen snapshot rejects changed revisions under the same run;
7. one complete 10-symbol LLM response is required;
8. decision-recorded recovery uses replay and performs zero LLM/Keychain calls;
9. ambiguous post-send crash enters `NEEDS_RECONCILIATION` and never automatically recalls;
10. exact order set persists atomically with deterministic IDs;
11. pending orders and A-share acquisition lots survive restart;
12. next-open execution cannot use a close-published row as open authority;
13. CN missing suspension/limit state cannot fill;
14. duplicate execution/event IDs are idempotent only for byte-identical content;
15. CN and US accounts, failures, calendars, currencies, and reports remain isolated;
16. sleep catch-up respects the missed-decision deadline;
17. reports rebuild byte-identically from committed state and contain no secrets;
18. generated plist contains no secret and passes `plutil` validation;
19. full pytest, Ruff, `git diff --check`, and repository credential scan pass;
20. an explicit, user-authorized local dry run proves zero network/Keychain access before live installation.

## 21. Non-goals

Version 1 does not include:

- real brokerage connectivity or real orders;
- leverage, shorting, derivatives, or intraday strategies;
- dynamic LLM-selected universe;
- automatic universe replacement;
- FX conversion or combined global NAV;
- partial-universe LLM decisions;
- automatic retry of ambiguous LLM invocations;
- guessing CN suspension/price-limit state from OHLCV;
- external messaging, web UI, or mobile UI;
- profitability claims.

## 22. Delivery sequence

1. versioned universe and deployment profile; implement schedule models and local-generation tooling, but do not check in or label derived 2026 schedules `OFFICIAL` until source-artifact and license/install gates close;
2. runtime state models/store and deterministic claim/recovery;
3. durable ledger event and order/execution stores;
4. per-symbol fetch orchestration, budgets, and coverage gate;
5. frozen snapshot and bounded decision recovery;
6. batch risk and atomic pending-order persistence;
7. admitted open/session-state execution and ledger booking only after the execution authority verdict becomes `VALIDATED`;
8. reports and operational CLI;
9. Keychain sources and zero-access negative tests;
10. LaunchAgent generation/installer;
11. offline crash/restart matrix;
12. explicit user-authorized live dry run and then paper-runtime activation.

Each capability follows RED → GREEN → full verification. One broad specification review and one broad quality review are sufficient; important findings receive focused regression tests and narrow re-review.