# Real Market Data Ingestion — Normative Contract

Date: 2026-07-30
Status: Normative pre-implementation contract
Parent design: `docs/designs/2026-07-29-real-market-data-ingestion-design.md`

This document freezes the cross-module invariants that must exist before implementation. If the parent design or implementation plan conflicts with this contract, this contract wins and the conflicting text must be corrected before code is accepted.

## 1. Scope decisions

Task 10D implements one dense, raw-price, daily-bar path for each market:

- CN: Eastmoney, Shanghai `6xxxxx` and Shenzhen `0xxxxx`/`3xxxxx` only;
- US: Alpha Vantage, exact uppercase ASCII symbols matching `[A-Z][A-Z0-9]{0,9}` only;
- no symbol punctuation conversion, Beijing Exchange, corporate actions, adjusted prices, provider fallback, sparse-universe semantics, or general calendar service;
- no standard test may access the network or read a real API key;
- completion requires one contemporaneous live CN run and one contemporaneous live US run against the same relevant source tree.

Real-data results disclose both exact policies:

- `pit_knowledge_policy = "current-view-baseline/v1"`;
- `market_data_price_policy = "raw-unadjusted/no-corporate-actions/v1"`.

Both values enter the spec fingerprint. The result is chronological against a frozen current-view baseline; it is not vendor-vintage PIT and not total-return performance.

## 2. Authority model

There are four distinct authorities. They must not be collapsed.

### 2.1 Provider parsing authority

A provider adapter owns only native HTTP/schema interpretation. It returns normalized exact `FetchedDailyBar` values and no persistence or backtest objects.

### 2.2 Latest local observation authority

`PointInTimeStore.latest_observed_bar_revision(...)` is used only by ingestion.

Identity:

```
(provider_id, market, canonical_symbol, session_date)
```

Selection order:

```
ingested_at DESC, source_record_id DESC
```

It has no `available_at` cutoff. It answers “what payload from this provider did this process last observe for this event?”

A new changed observation for an existing provider/event stream requires:

```
new_ingested_at > latest_observed.ingested_at
```

Equality or rollback of the ingestion clock fails before any write.

### 2.3 Business PIT authority

`PointInTimeStore.latest_bar_revision_as_of(...)` is used by builders and backtests.

Eligibility and deterministic winner:

```
available_at <= as_of
ORDER BY available_at DESC,
         ingested_at DESC,
         source DESC,
         source_record_id DESC
```

This query does not mean “last locally observed”.

### 2.4 Frozen run authority

Before strategy, ledger, simulator, risk, or cache access, the runner resolves the complete matrix:

```
for each owning session S:
    for each historical session H <= S:
        for each symbol in the fixed universe:
            select revision for H as_of S.close_at
```

The matrix is immutable and symbol-major/session-minor within each owning session. The runner computes the resolved-data fingerprint before cache lookup. The session loop reads only this matrix and never re-queries the store.

Cache identity is:

```
(run_id, spec_fingerprint, resolved_data_fingerprint)
```

The same run ID with a different spec or resolved fingerprint is a stable conflict. A correction inserted after freeze cannot alter an in-flight run.

## 3. Baseline/correction state machine

For one provider/event stream, compare only normalized event payload:

```
market, canonical_symbol, session_date,
canonical open, high, low, close, volume
```

Do not compare availability, ingestion time, source IDs, provider record IDs, or Decimal trailing-zero representation.

| Previous local observation | New normalized payload | Action | revision kind | available_at |
|---|---|---|---|---|
| none | A | append | BASELINE | authoritative session close |
| A | A | no write | none | none |
| A | B | append | CORRECTION | new ingested_at |
| A → B | B | no write | none | none |
| A → B | A | append rollback | CORRECTION | new ingested_at |

For every appended row:

```
ingested_at >= available_at
```

For an existing stream, changed observations also require strict ingestion-clock increase.

A second provider attempting the same `(market, symbol, session_date)` is a persistence conflict in Task 10D. Lexical source precedence must not silently choose a provider.

## 4. Atomic persistence

Task 10D does not expose partial-success ingestion.

Add an atomic store operation that receives a fully materialized immutable batch of bar revisions. It must:

1. validate the complete batch before opening a transaction;
2. reject duplicate row identities and duplicate provider/event identities inside the batch;
3. begin one DuckDB transaction;
4. apply the existing idempotent/conflict semantics for every row;
5. commit only after all rows succeed;
6. roll back the whole transaction after any failure;
7. return exact appended/unchanged identities only after commit.

The existing single-row append behavior remains compatible and may delegate to the batch path.

Provider, parser, schedule, numeric, authority, clock, and persistence failures return no `IngestionReport` and leave zero new rows. No `PartialPersistenceError`, successful-prefix protocol, or recovery queue is part of Task 10D.

## 5. Canonical source identity

Provider adapters expose `provider_record_id`:

- native stable record ID when one exists;
- otherwise `provider-payload-sha256:<64 lowercase hex>` over the fixed normalized native row fields.

Persisted source is exact `provider_id`.

Persisted source record ID is:

```
market-data-source-record-sha256:<64 lowercase hex>
```

The tagged SHA-256 payload is a compact fixed array in this exact order:

```
[
  "source-record/v1",
  provider_id,
  market.value,
  canonical_domain_symbol,
  session_date.isoformat(),
  canonical_decimal(open),
  canonical_decimal(high),
  canonical_decimal(low),
  canonical_decimal(close),
  canonical_decimal(volume),
  provider_native_symbol,
  provider_record_id,
  revision_kind,                       # BASELINE | CORRECTION
  canonical_utc_datetime(available_at),
  "current-view-baseline/v1",
  "raw-unadjusted/no-corporate-actions/v1"
]
```

Canonical Decimal and datetime rules are the existing audit rules. Caller Decimal context cannot change validation, comparison, or hashes.

Rollback A after correction B receives a new correction availability instant and therefore a different persisted source record ID from baseline A.

## 6. Exact public models

All new public models are exact final frozen Pydantic models with strict validation, `extra="forbid"`, nested revalidation, stable subclass rejection, and resistance to:

- attribute assignment;
- `copy(include=...)`, `copy(exclude=...)`, and any explicit `copy(update=...)`;
- any explicit `model_copy(update=...)`, including `{}` and same-value updates;
- polluted or missing-field `model_construct` instances crossing a public boundary.

Consumers rebuild untrusted public model instances from exact field maps instead of trusting prior construction.

### 6.1 `DailyBarRequest`

Fields:

- exact `Market`;
- exact canonical symbol string;
- exact plain `date` start/end;
- exact price mode literal `RAW`.

Invariants:

- `start <= end`;
- CN and US symbol formats are the frozen subset in Section 1;
- no API key or transport option is part of the request.

### 6.2 `FetchedDailyBar`

Fields:

- market, canonical domain symbol, plain session date;
- exact finite Decimal OHLCV;
- provider ID, provider-native symbol, provider record ID.

Invariants:

- prices strictly positive;
- volume nonnegative;
- `low <= open, close <= high` and `low <= high`;
- effective scale at most 12;
- absolute value strictly less than `1E26`;
- no float, int, bool, or string coercion to Decimal.

### 6.3 `AppendedRevisionIdentity`

Fields:

- source;
- source record ID.

The tuple in reports is session-sorted and unique. Event details remain queryable from the store and are not duplicated into a second identity truth.

### 6.4 `IngestionReport`

Exact non-bool integer counts:

- requested;
- received;
- appended;
- unchanged.

Definitions and arithmetic:

```
requested = explicit supplied schedule sessions inside request range
0 < received <= requested
appended + unchanged == received
len(appended_revision_identities) == appended
```

A report exists only after successful atomic commit.

### 6.5 Errors

Keep a narrow exception hierarchy and freeze `MarketDataErrorCode` plus `retryable` and safe metadata. Exceptions contain no original exception object, URL query, headers, body, API key, or native payload.

Codes:

```
invalid_request, invalid_range, unsupported_symbol,
dns, connect_timeout, read_timeout, connection_reset, tls,
redirect_disallowed, response_too_large, unsupported_encoding,
invalid_encoding, http_4xx, http_5xx,
auth, throttled, information, malformed_json, schema, identity,
numeric, duplicate, out_of_range, empty, compact_coverage,
schedule, clock, persistence, persistence_conflict,
missing_session_data, live_incomplete, evidence_write,
secret_policy, internal_contract
```

Retryable codes are exactly:

```
dns, connect_timeout, read_timeout, connection_reset,
http_5xx, throttled
```

All others are terminal until caller input, credentials, schedule, provider contract, or code changes.

## 7. Provider protocol postconditions

`HistoricalDailyBarProvider` exposes only:

```python
@property
def provider_id(self) -> str: ...

def fetch_daily_bars(
    self, request: DailyBarRequest,
) -> tuple[FetchedDailyBar, ...]: ...
```

The ingestor, not runtime `Protocol` checking, enforces:

1. provider ID is exact, stripped, nonblank and identical before/after fetch;
2. result is an exact nonempty tuple;
3. every item is exact and fully rebuilt;
4. market/symbol/range equal the request;
5. event identity is unique;
6. result order is ascending session date;
7. all schedule and numeric admission checks pass before persistence.

A list, generator, tuple subclass, bar subclass, polluted model, duplicate, wrong identity, out-of-range row, or provider-ID mutation fails before writes.

## 8. HTTP transport profile

Production transport uses `http.client.HTTPSConnection` with one exact positive `timeout_seconds` applied to connect and then to socket reads. Error metadata records which stage failed; Task 10D does not claim independently configured timeout values.

Frozen profile:

- HTTPS only;
- system CA and hostname verification;
- adapter-specific host allowlist;
- GET only;
- no redirect following; any 3xx is `redirect_disallowed`;
- only status 200 reaches JSON parsing;
- no compression request; only absent or `identity` content encoding accepted;
- maximum response body 1 MiB;
- declared oversize rejected before body read;
- unknown/chunked length read at most limit + 1;
- strict UTF-8; BOM and invalid UTF-8 rejected;
- no URL query, request headers, response body, or response headers in logs/errors.

Secret-safe exception construction:

1. catch transport exceptions;
2. inside the handler extract only a safe code and metadata;
3. leave the `except` block;
4. clear library-owned locals that contain target, response body, native payload, or native exception;
5. raise a newly constructed safe exception with no cause/context reference to the original;
6. test the exception object graph, args, attributes, repr, logs, provider repr/copy/pickle, verifier output, tracked diff, and every traceback frame owned by `stock_agent.data.providers` with canaries.

Caller-owned traceback frames are outside this guarantee: the caller necessarily held the argument before invoking the provider, and a library must not mutate another frame's locals. No provider-owned traceback frame may retain the canary. Python string memory zeroization is not promised.

`MarketDataError` domain fields (`code`, `retryable`, `metadata`, and `args`) are immutable after construction. Python interpreter-managed linkage fields (`__traceback__`, `__cause__`, `__context__`, and `__suppress_context__`) remain assignable so normal propagation, context managers, and traceback stripping work correctly; they are not domain payload.

## 9. Eastmoney wire contract

Request:

- method: `GET`;
- host: `push2his.eastmoney.com`;
- path: `/api/qt/stock/kline/get`;
- query generated only with `urllib.parse.urlencode` from fixed parameters;
- date format: `YYYYMMDD`.

Fixed parameters:

```
secid=<1.SYMBOL for Shanghai | 0.SYMBOL for Shenzhen>
beg=<YYYYMMDD>
end=<YYYYMMDD>
klt=101
fqt=0
fields1=f1,f2,f3,f4,f5,f6
fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
```

No caller-supplied extra query parameters.

Envelope requirements:

- top-level JSON object with unique keys;
- exact integer `rc == 0`;
- object `data`;
- exact six-character string `data.code == request symbol`;
- exact integer `data.market == mapped market`;
- array `data.klines`.

Each K-line is one comma-separated string with exactly 11 fields:

| index | field |
|---:|---|
| 0 | session date |
| 1 | open |
| 2 | close |
| 3 | high |
| 4 | low |
| 5 | volume |
| 6 | turnover, ignored after structural admission |
| 7 | amplitude, ignored after structural admission |
| 8 | percentage change, ignored after structural admission |
| 9 | absolute change, ignored after structural admission |
| 10 | turnover rate, ignored after structural admission |

Every field must be present and nonblank. The six persisted fields are parsed exactly. Returned dates must all be inside the bounded request. Duplicate dates fail. Rows are sorted only after complete admission. Empty rows fail.

HTTP 401/403 map to `auth`, 429 to `throttled`, 5xx to retryable `http_5xx`, other 4xx to terminal `http_4xx`. Nonzero Eastmoney `rc` is terminal `information` unless a future documented code is added by design revision.

## 10. Alpha Vantage wire contract

Request:

- method: `GET`;
- host: `www.alphavantage.co`;
- path: `/query`;
- query generated only with `urlencode` from fixed parameters.

Fixed parameters:

```
function=TIME_SERIES_DAILY
symbol=<canonical US symbol>
outputsize=compact
datatype=json
apikey=<private constructor key>
```

No caller extras. The key is read once from `ALPHA_VANTAGE_API_KEY` by the live composition root; it never enters argv, Pydantic models, reports, evidence, source IDs, or logs.

Parser pipeline:

1. body-size and strict encoding admission;
2. JSON decoding with duplicate-key rejection at every object level and rejection of `NaN`/`Infinity`;
3. response-category detection;
4. full metadata/series/record validation;
5. full-series date uniqueness and numeric validation;
6. range coverage decision;
7. inclusive filtering;
8. ascending sort;
9. exact tuple construction.

Expected required keys:

```
"Meta Data"
"Time Series (Daily)"
"Meta Data"["2. Symbol"]
record["1. open"]
record["2. high"]
record["3. low"]
record["4. close"]
record["5. volume"]
```

Metadata symbol must exactly equal the request symbol. Unknown metadata keys may be ignored after duplicate-key/type admission; unknown daily-record fields fail because price schema is exact.

The complete compact series is validated before filtering. Let its bounds be `[earliest_returned, latest_returned]`:

- request start before earliest returned -> `compact_coverage`;
- request end after latest returned is allowed only for dates not listed as already-closed expected sessions;
- an expected closed session missing from the filtered result is a verifier/builder data-gap failure;
- filtered empty inside the returned coverage window -> `empty`.

Response categories:

- `Error Message` -> terminal `auth` only for a recognized credential pattern, otherwise terminal `invalid_request`;
- rate-limit `Note` or rate-limit `Information` -> retryable `throttled`;
- other `Information` -> terminal `information`;
- missing expected series without one of those categories -> `schema`.

## 11. JSON admission

All provider JSON uses a shared strict loader:

- input already passed body/encoding limits;
- duplicate object keys rejected at every nesting level;
- `NaN`, `Infinity`, and `-Infinity` rejected;
- trailing non-whitespace data rejected;
- no domain numeric field travels through binary float;
- raw bodies and parsed native dictionaries do not cross adapter boundaries.

Any malformed row aborts the complete provider call. No valid prefix is returned.

## 12. Session schedule and dense data

The explicit `TradingCalendar` plus an immutable bounded schedule is the sole holiday/open/close authority.

Every schedule row contains:

- plain session date;
- exact aware open and close datetimes;
- IANA zone name;
- provenance text and generation/retrieval date.

Invariants:

- local date of open and close equals the row session date;
- open < close;
- previous close < next open;
- schedule dates equal the requested calendar slice exactly;
- DST offset comes from `ZoneInfo`;
- early close is explicit per date, never inferred from a fixed clock;
- every selected session is closed at ingestion time.

Task 10D supports only a dense grid:

```
fixed universe × every explicit calendar session
```

Every symbol/session must have a persisted close revision. Holiday dates are excluded only by the calendar. Suspension, provider gap, and missing row are unsupported and fail. No forward-fill, zero-fill, symbol removal, session removal, or silent range shortening.

Rolling overlap uses explicit calendar sessions only.

## 13. Open-frame provenance and no look-ahead

Each `BacktestSession` includes symbol-sorted `open_frame_sources`. Every item contains:

- market;
- symbol;
- session date;
- source;
- source record ID;
- revision available_at;
- revision ingested_at.

For each symbol/session, the builder:

1. calls only `latest_bar_revision_as_of(..., as_of=session.close_at)`;
2. derives synthetic open OHLC from that exact revision's `bar.open`;
3. sets synthetic volume to exact Decimal zero;
4. sets synthetic `available_at = open_at`;
5. records that same selected row in `open_frame_sources`.

The builder never calls latest-now or the latest-observed query. Open provenance enters the spec fingerprint. The runner validates that the own-session revision in its frozen matrix exactly matches the open-frame source and open value.

The strategy never receives open frames, open provenance, provider, store, or transport authority.

## 14. Fixture provenance

Every provider fixture has a same-name metadata JSON containing:

- provider ID;
- retrieved UTC timestamp;
- secret-free request parameters;
- source type: `live-redacted` or `documentation-derived`;
- exact transformations/redactions;
- SHA-256 of the fixture bytes.

Fixtures are parser evidence only. They do not satisfy live acceptance.

## 15. Live verification and completion

Standard pytest:

- uses injected transports only;
- never reads `ALPHA_VANTAGE_API_KEY`;
- never opens a socket;
- treats any attempted network/environment access as test failure.

Live verifier:

- key comes only from the environment;
- top-level handles classified safe errors and prints code plus safe metadata;
- unknown errors print only exception type and run ID, without repr or traceback;
- nonzero exit on any provider, schedule, ingestion, builder, runner, or evidence failure.

Each live run requires:

- one explicit bounded schedule with provenance;
- at least five closed dense sessions;
- full expected-session coverage;
- successful atomic ingestion into temporary DuckDB;
- nonempty selected revisions and open-frame provenance;
- both policy literals;
- nonempty spec and resolved-data fingerprints;
- two independent assemblies from the same normalized tuple producing equal fingerprints;
- no adjusted/total-return claim.

Evidence records:

- UTC run time;
- current git commit;
- SHA-256 of provider/parser/ingestor/builder/verifier source files;
- market, symbol, request and schedule provenance;
- provider ID, coverage, counts, policies, fingerprints and safe result summary.

Different live fetches need not have equal fingerprints. Evidence remains current only while the recorded relevant-source digest equals the current relevant-source digest. Evidence-only or unrelated documentation commits do not stale it.

Task 10D is complete only when:

1. all deterministic tests and full repository gates pass;
2. secret canary passes across repr, copy/pickle, exception graph, traceback, logs, verifier output, evidence and tracked diff;
3. CN and US each have one successful live evidence record against the same relevant-source digest;
4. both evidence records correspond to the final provider/parser/ingestor/builder/verifier code;
5. worktree is clean and the feature branch is pushed.

Missing key, network/TLS failure, throttling, compact-window shortage, data gap, or stale evidence leaves Task 10D `in_progress`.

## 16. Acceptance invariants

The implementation test suite must directly prove:

1. baseline A, repeat A, correction B, repeat B, rollback A;
2. strict per-stream ingestion clock and different-provider conflict;
3. canonical source-ID golden vectors and Decimal-context independence;
4. exact immutable/copy/subclass/model-construction boundaries;
5. provider-ID mutation, wrong container/type/identity/order/duplicate/range failures before writes;
6. atomic rollback on a failure at every batch position;
7. complete report arithmetic and identity order;
8. provider wire/schema/error/coverage rules;
9. secret canary across the full exception and output graph;
10. dense multi-symbol/session success and every missing-grid failure;
11. close-after correction excluded from historical open provenance;
12. frozen runner matrix unaffected by mid-run store writes;
13. cache conflict on changed resolved data;
14. raw-price policy fingerprint divergence;
15. live evidence freshness based on relevant-source digests.
