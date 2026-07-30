# Real Market Data Ingestion Design

Date: 2026-07-29
Status: Approved for implementation planning
Scope: Phase 1 Task 10D — provider-backed CN/US daily bars, incremental ingestion, and real-data backtest proof

## 1. Goal

Add one auditable path from external daily-market-data providers into the existing point-in-time store and chronological backtester.

The phase is complete only when both markets have been exercised against live provider responses:

- CN: Eastmoney, no credential;
- US: Alpha Vantage, API key supplied only through `ALPHA_VANTAGE_API_KEY`;
- normalized bars are persisted in a temporary DuckDB store;
- a chronological backtest consumes those persisted bars end to end;
- selected revision provenance and resolved-data fingerprints are present in the result.

Parser fixtures alone do not satisfy the acceptance criterion.

## 2. Non-goals

This task does not add:

- fundamentals, corporate actions, adjusted prices, FX, benchmark data, or current A100/US100 constituent universes;
- provider failover, background queues, retry daemons, parallel ingestion, durable job state, or rate-limit orchestration;
- a general exchange-calendar service;
- credentials in repository files, model dumps, logs, snapshots, or exception messages;
- CLI commands beyond a minimal live-verification script if required for reproducible acceptance;
- historical vendor revision archives.

Those are separate work, not small extensions to this slice.

## 3. Provider decision

### 3.1 Selected providers

CN uses Eastmoney's public historical K-line endpoint with raw/unadjusted daily bars (`fqt=0`). The initial supported symbol mapping is deliberately narrow:

- Shanghai `6xxxxx` -> Eastmoney market `1`;
- Shenzhen `0xxxxx` and `3xxxxx` -> Eastmoney market `0`;
- unsupported formats, including Beijing Exchange symbols, fail explicitly.

US uses Alpha Vantage `TIME_SERIES_DAILY` with `outputsize=compact`. The API key is injected into the provider constructor by the composition root from `ALPHA_VANTAGE_API_KEY`. The key is never retained in a Pydantic model, provenance record, representation, or error string.

The compact endpoint intentionally limits the first slice to recent sessions. Arbitrary deep US history is not promised by this task.

### 3.2 Rejected alternatives

- Yahoo Finance: repeated HTTP 429 from the current execution environment.
- Stooq: browser proof-of-work, unsuitable for a small deterministic HTTP adapter.
- Tencent US K-line: observed anomalous sparse history.
- Nasdaq site API: HTTP/2 protocol failures in the current environment.
- One unofficial aggregator for both markets: fewer classes, but a single unstable failure domain and weaker audit semantics.

Provider adapters remain replaceable, but fallback routing is not implemented.

## 4. Historical-knowledge policy

Neither selected provider exposes historical revision vintages. A bar fetched today for an old session is the provider's current view, not proof of what the provider published at that old close.

This task therefore distinguishes two facts:

1. business availability: a daily OHLCV bar is usable after the authoritative session close;
2. local observation: this project first observed the payload at ingestion time.

For the initial historical baseline:

- `available_at` is the supplied authoritative session close;
- provenance policy is `current-view-baseline/v1`;
- the result must not claim vendor-vintage point-in-time correctness.

For a later fetch of the same event identity:

- an unchanged normalized payload is skipped;
- a changed payload is appended as a correction;
- correction `available_at` is the current ingestion instant;
- prior backtest session results remain unchanged.

This provides chronological no-look-ahead against a frozen baseline and honest local correction history. Full historical-vintage PIT requires a vendor revision archive and is explicitly out of scope.

`BacktestInputManifest.pit_knowledge_policy` must support and fingerprint the `current-view-baseline/v1` value. Existing fixture/backtest behavior keeps `business-available-at/v1`.

## 5. Components

### 5.1 Provider request

`DailyBarRequest` is an exact frozen model with:

- market;
- normalized provider symbol;
- inclusive plain-date start and end;
- raw/unadjusted price mode only.

It rejects inverted ranges, unsupported markets, subclasses, copy projections, and polluted nested values.

### 5.2 Normalized provider bar

`FetchedDailyBar` is an exact frozen model with:

- market and normalized domain symbol;
- plain session date;
- exact finite Decimal open, high, low, close, and volume;
- provider ID;
- provider-native symbol;
- deterministic raw-record identity or canonical payload digest.

It enforces the existing `Bar` price/volume invariants and the store's `DECIMAL(38, 12)` representability boundary before any write.

Provider parsing never routes values through float.

### 5.3 Provider protocol

`HistoricalDailyBarProvider` exposes:

- immutable `provider_id`;
- `fetch_daily_bars(request) -> tuple[FetchedDailyBar, ...]`.

The returned tuple is exact, date-sorted, unique by event identity, inside the requested range, and belongs to the request market/symbol.

The protocol does not expose HTTP clients or provider-native dictionaries to consumers.

### 5.4 HTTP transport

A minimal injected transport accepts a URL plus non-secret headers and returns response bytes and status metadata. Production uses Python standard-library HTTP facilities; tests use deterministic in-memory responses. Transport exceptions are converted before they can expose a query string containing a key.

The transport has explicit connect/read timeout configuration. This task does not retry automatically. Ambiguous network failure is surfaced so callers decide whether to retry.

### 5.5 Incremental ingestor

`IncrementalBarIngestor` receives:

- one provider;
- `PointInTimeStore`;
- an exact map from session date to authoritative aware open/close instants;
- an ingestion clock value supplied by the caller.

For each normalized provider bar it:

1. verifies a matching session close exists;
2. builds the domain close `Bar`;
3. queries the latest locally visible revision for the event identity at ingestion time;
4. skips an unchanged normalized payload;
5. assigns baseline or correction availability according to Section 4;
6. derives a deterministic source record ID from provider ID, market, symbol, session, canonical OHLCV, and availability policy;
7. appends through the existing store API.

The method returns an immutable `IngestionReport` containing requested, received, appended, and unchanged counts plus appended revision identities. Any rejected row aborts the batch before writes; the report does not imply partial-error success.

Writes may occur one bar at a time because the current store has no batch transaction API. To avoid misleading atomicity, parsing and full-batch validation complete before the first write. Database errors after writing begins are surfaced with the exact successful prefix represented in store provenance; rollback semantics are not claimed.

### 5.6 Rolling overlap

The caller supplies the requested date range. A small helper can derive a rolling overlap from explicit calendar sessions, but the ingestor does not invent calendars from one symbol's bars.

Recommended operation:

- initial load: explicit recent range;
- incremental load: last five known calendar sessions through the requested end;
- unchanged overlap rows are skipped;
- changed overlap rows become corrections.

A missing provider row is not interpreted as deletion, holiday, or suspension.

### 5.7 Backtest-spec assembly

A focused real-data spec builder receives:

- normalized persisted close revisions;
- explicit `TradingCalendar` sessions;
- authoritative aware open/close instants;
- instruments, account, cash, strategy config, and run ID.

For each session it creates:

- a close bar from the persisted full OHLCV revision;
- an execution-only open frame with `open == high == low == close == daily open`, volume zero, and `available_at == open_at`;
- no strategy access to that open frame.

The synthetic open frame is an execution input, not a claim that the full daily bar was known at the open.

The builder rejects missing sessions instead of silently shortening the backtest.

## 6. Provider-specific parsing

### 6.1 Eastmoney

The adapter requests fixed field arrays and validates:

- top-level return code indicates success;
- `data` exists and identity matches the requested security;
- each K-line has the exact documented field count;
- date and OHLCV fields parse exactly;
- rows are sorted, unique, and bounded by the request;
- empty data is an explicit provider-data error.

Turnover and percentage fields may be present but are not persisted in the Phase 1 bar model.

### 6.2 Alpha Vantage

The adapter validates:

- neither `Error Message`, `Information`, nor `Note` replaces the expected time-series object;
- the metadata symbol matches the request after documented normalization;
- each daily record contains all five OHLCV fields;
- dates and Decimal strings parse exactly;
- rows are sorted ascending and filtered to the inclusive requested range;
- an empty filtered result is an explicit provider-data error.

Rate-limit and invalid-key payloads are provider errors, not empty market data.

## 7. Error boundaries

Stable domain-facing errors distinguish:

- transport failure or non-success HTTP status;
- provider throttling/authentication/information response;
- malformed schema or identity mismatch;
- unsupported symbol mapping;
- missing session schedule;
- numeric admission failure;
- empty requested range result;
- persistence conflict.

No exception includes API keys or complete raw payloads. A bounded response excerpt may be retained only when proven not to contain credentials; the default is metadata-only diagnostics.

No fallback emits zero prices, zero volume rows, fabricated holidays, or fixture bars.

## 8. Determinism and provenance

Canonical hashing uses the existing audit utility:

- compact fixed-field arrays;
- enum values;
- canonical Decimal coefficient/exponent strings;
- UTC six-microsecond datetimes;
- UTF-8 and tagged SHA-256.

Source is the stable provider ID. Source record IDs include enough event and payload identity to make unchanged retries idempotent and changed payloads distinct.

Provider response order never determines store or report order.

## 9. Security

- `ALPHA_VANTAGE_API_KEY` is read only by the live composition root.
- Tests pass a fake key directly to an injected test transport and assert it is absent from repr, exceptions, reports, and model dumps.
- No browser cookies, Keychain, `.env` files, shell history printing, or credential discovery commands are used.
- Live-verification output prints provider ID, symbol, date range, counts, fingerprints, and backtest summary only.

## 10. Testing

### 10.1 Model and parser tests

Use small redacted response fragments captured from real provider shapes to test:

- exact successful parsing;
- unsorted input normalization;
- duplicate/date/identity/schema rejection;
- Decimal boundaries and no-float admission;
- empty, throttled, invalid-key, and malformed responses;
- exact immutable/copy/subclass resistance;
- API-key redaction.

### 10.2 Ingestion tests

With a real temporary DuckDB store:

- initial baseline availability equals supplied close;
- identical overlap fetch is unchanged and appends nothing;
- changed overlap payload appends a correction at ingestion time;
- correction is invisible before ingestion and visible at/after ingestion;
- provenance and source record IDs are exact;
- parse failure writes nothing;
- close schedule gaps fail before writes.

### 10.3 Builder and runner tests

- persisted closes become cumulative PIT snapshots;
- open frames expose only the daily open value;
- missing calendar/session data fails;
- manifest records `current-view-baseline/v1`;
- fingerprints and independent equivalent results are deterministic.

### 10.4 Mandatory live acceptance

CN live acceptance:

- fetch a recent explicit range for `600000`;
- ingest into temporary DuckDB;
- run at least five sessions through `ChronologicalBacktestRunner`;
- verify nonempty selected revisions and stable provenance/fingerprint.

US live acceptance:

- require `ALPHA_VANTAGE_API_KEY` without printing it;
- fetch a recent explicit range for one supported US symbol;
- ingest into temporary DuckDB;
- run at least five sessions through the same runner;
- verify nonempty selected revisions and stable provenance/fingerprint.

A skipped live test is not a completed Task 10D. If the API key or network is unavailable, report the blocker and leave the task in progress.

## 11. Delivery sequence

1. Define exact provider and ingestion contracts.
2. Implement Eastmoney parser/adapter.
3. Implement Alpha Vantage parser/adapter with secret redaction.
4. Implement incremental baseline/correction ingestion.
5. Extend manifest knowledge-policy contract.
6. Implement focused real-data spec assembly.
7. Run CN live end to end.
8. Run US live end to end with caller-provided environment key.
9. Perform specification and code-quality review, then push the feature branch.

No step may replace a failed live path with fabricated output.
