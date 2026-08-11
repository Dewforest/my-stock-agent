# Strategy A Bounded-LLM Vertical Slice Design

Date: 2026-08-06
Status: specification-reviewed; blockers resolved before implementation
Normative upstream reference: `ZhuLinsen/daily_stock_analysis@905c339d80ad2daa6fd2bab3bb10267b23c7ac1c`
Secondary current-source review: `ZhuLinsen/daily_stock_analysis@ed848da6f0fc1080e1a61a1799b9c7d510a3eaca`
Base repository commit: `b117731a39e6ce91c7fcd0b71b4571fa028df785`

## 1. Purpose and Authority Chain

Build the first strategy that turns point-in-time real daily bars into auditable simulated trades:

```
PIT market snapshot
  -> deterministic candidate envelopes
  -> provider-neutral LLM action selection inside each envelope
  -> immutable StrategyIntent values
  -> existing atomic risk engine
  -> existing next-open execution simulator
  -> existing append-only ledger and NAV
```

Authorities do not overlap:

- `PointInTimeStore` and `MarketSnapshot` own market facts and availability time.
- The deterministic builder owns eligibility, allowed actions, and action-to-target mappings.
- `LLMDecisionProvider` owns only an allowed action, confidence, thesis, and invalidation.
- The risk engine owns portfolio admissibility and may reject or reduce any intent.
- The execution simulator owns fill feasibility and price.
- The ledger owns cash, lots, positions, and P&L.

The LLM cannot create symbols, weights, orders, fills, risk approvals, or ledger events.

## 2. Upstream Adaptation Boundary

The frozen upstream source contributes concepts, not runtime dependencies:

1. classify market/trend state before selecting an action;
2. separate hard eligibility from soft judgment;
3. require price and volume confirmation for an offensive stance;
4. prefer neutral behavior when evidence is mixed or incomplete;
5. preserve data quality, reasons, risks, and invalidation conditions;
6. keep the final decision stage tool-free over frozen evidence;
7. canonicalize rich opinions into a strict trading action contract;
8. apply deterministic risk guardrails after model output.

`src/core/market_strategy.py` is a prompt blueprint, not an executable strategy. Upstream screening, specialist Agents, and market review are separate systems. We do not import their fetchers, pipeline, persistence, scheduler, LiteLLM routing, UI, notifications, broker code, report models, free-form parsers, or permissive schemas.

A current-source review recommended pure LLM reranking. This design deliberately allows bounded action selection because that is the user-approved boundary. The model still cannot define a target weight: the envelope maps each allowed action to exactly one target.

## 3. Provider-Neutral Model Boundary

The domain uses only:

- `LLMDecisionProvider`
- `LLMDecisionRequest`
- `LLMDecisionResponse`
- `LLMDecisionRecord`
- `LLMInvocationAttempt`
- `LLMRunAttestation`

Vendor names never appear in field names, strategy IDs, branching logic, or candidate semantics. Opaque provenance values such as `model_identity` may contain a provider/model identifier. Concrete OpenAI, DeepSeek, local, or other adapters are deployment choices outside the domain.

API keys are read only by a concrete live adapter at construction. Core code never reads environment variables. Secrets, authorization headers, transport objects, and raw secret-bearing payloads are forbidden from models, journal rows, evidence, logs, and exceptions.

## 4. Deterministic Strategy A Configuration

`StrategyAConfig` is exact, frozen, extra-forbidden, and versioned. Version 1 fields are:

- `config_version`: nonblank string;
- `short_window`: exact positive integer;
- `long_window`: exact positive integer, strictly greater than `short_window`;
- `volume_window`: exact positive integer;
- `volume_confirmation_threshold`: finite nonnegative Decimal;
- `offensive_target_weight`: finite Decimal in `(0, 1]`;
- `neutral_target_weight`: finite Decimal in `[0, offensive_target_weight]`;
- `model_identity_policy_id`: nonblank versioned string;
- `prompt_template_id`: nonblank versioned string;
- `prompt_template_digest`: `prompt-sha256:<64 lowercase hex>`.

Required history is derived, not configured:

```
required_history = max(long_window, volume_window + 1)
```

This removes contradictory minimum-history configurations.

The model-identity replay policy for version 1 is exact equality of the opaque `(model_identity, model_revision)` stored in the successful decision record against the identities allowed by the provider configured for replay. The policy ID is part of the request fingerprint. Vendor identity never affects deterministic features or regimes.

## 5. Deterministic Candidate Envelope

### 5.1 Input and time

The pure builder receives only exact revalidated `StrategyContext` and `StrategyAConfig`. It may not receive or call a store, network, wall clock, calendar, broker, risk engine, execution simulator, ledger, or callback.

The runner supplies a close snapshot. The request carries exact `decision_phase = POST_CLOSE`; phase is not inferred from current time. Version 1 does not attempt intraday decisions.

### 5.2 Bar windows and exact arithmetic

For each symbol, bars are chronological and come only from the frozen snapshot.

If history length is below `required_history`, data quality and regime are `INSUFFICIENT` and no feature is padded or fetched.

Otherwise:

- `short_sum` is the sum of the last `short_window` closes, including the latest bar.
- `long_sum` is the sum of the last `long_window` closes, including the latest bar.
- `prior_volume_sum` is the sum of exactly the `volume_window` volumes immediately preceding the latest bar; the latest volume is excluded.
- Positive trend means both:
  - `short_sum * long_window > long_sum * short_window`
  - `latest_close * long_window > long_sum`
- Negative trend means both corresponding strict `<` comparisons.
- Any equality or mixed trend comparison is neutral.
- Volume is confirmed when:
  - if `prior_volume_sum == 0`, `latest_volume > 0`;
  - otherwise `latest_volume * volume_window >= prior_volume_sum * volume_confirmation_threshold`.

No mean is materialized for regime classification, so recurring division and ambient rounding cannot change the regime.

Regime is:

- `INSUFFICIENT`: insufficient history;
- `OFFENSIVE`: positive trend and confirmed volume;
- `DEFENSIVE`: negative trend, regardless of volume;
- `NEUTRAL`: every other sufficient-history case.

All arithmetic executes in a private Decimal context sized from input coefficients/exponents. Invalid/extreme arithmetic yields one stable strategy-arithmetic error and emits no request.

### 5.3 Current-weight materialization

For comparison with configured caps, use exact cross multiplication:

```
market_value ? nav * configured_weight
```

If NAV is zero, current weight is zero. When a HOLD intent requires a materialized current weight, use a private context with precision sufficient for all input coefficient digits plus guard digits, `ROUND_FLOOR`, full supported exponent range, and canonical Decimal normalization. The same algorithm as the established fixture boundary is used; ambient context is neither consulted nor mutated.

### 5.4 Data quality and evidence

Version 1 data quality is:

- `COMPLETE`: every configured window exists;
- `INSUFFICIENT`: otherwise.

Every envelope carries explicit reason codes and sorted evidence IDs. Bar evidence reuses the established `bar-sha256` contract over:

```
[market, symbol, session_date, open, high, low, close, volume, available_at]
```

Only bars used by the long-close or prior-volume/latest-volume windows enter evidence; duplicates are removed and IDs are sorted.

Selected-revision source/ingestion provenance is not available through `StrategyContext` and therefore is not fabricated in candidate evidence. The real-data proof records revision provenance separately from the runner result.

Portfolio identity is:

```
portfolio-snapshot-sha256(
  [account_id, market, cash, nav, peak_nav, as_of,
   [[symbol, quantity, average_cost, market_value], ... symbol-sorted]]
)
```

All digests in this design use UTF-8 compact JSON arrays, fixed field order, `ensure_ascii=False`, canonical finite Decimal coefficient/exponent encoding, exact enum values, dates as ISO dates, and datetimes normalized to UTC with six fractional digits and trailing `Z`, via `stock_agent.audit`.

### 5.5 Complete action-to-target table

Configured weights are hard target ceilings. HOLD above a ceiling is not grandfathered.

`INSUFFICIENT`:

| Position | Allowed mapping |
|---|---|
| unheld | `HOLD -> 0` |
| held | `HOLD -> current_weight` |

Insufficient history preserves existing exposure but cannot create or increase it.

`OFFENSIVE` relative to `offensive_target_weight`:

| Current relation | Allowed mapping |
|---|---|
| unheld / zero | `HOLD -> 0`, `BUY -> offensive_target_weight` |
| `0 < current < cap` | `HOLD -> current_weight`, `BUY -> cap` |
| `current == cap` | `HOLD -> current_weight` |
| `current > cap` | `REDUCE -> cap` |

`NEUTRAL` relative to `neutral_target_weight`:

| Current relation | Allowed mapping |
|---|---|
| unheld / zero | `HOLD -> 0` |
| `0 < current <= cap` | `HOLD -> current_weight` |
| `current > cap > 0` | `REDUCE -> cap` |
| `current > cap == 0` | `SELL -> 0` |

`DEFENSIVE` relative to `neutral_target_weight`:

| Current relation | Allowed mapping |
|---|---|
| unheld / zero | `HOLD -> 0` |
| `0 < current <= cap` | `HOLD -> current_weight`, `SELL -> 0` |
| `current > cap > 0` | `REDUCE -> cap`, `SELL -> 0` |
| `current > cap == 0` | `SELL -> 0` |

A REDUCE target is always strictly below current weight. Every allowed action maps to exactly one target; the LLM never transmits a weight.

### 5.6 Candidate ordering and digest

Candidates are symbol-sorted. Candidate ID is tagged `strategy-a-candidate-sha256` over:

```
[schema_version, strategy_id, config_version, market, symbol, as_of,
 decision_phase, regime, data_quality,
 short_window, long_window, volume_window, volume_threshold,
 short_sum, long_sum, latest_close, prior_volume_sum, latest_volume,
 portfolio_snapshot_id,
 [[action, mapped_target_weight], ... action-enum order],
 reason_codes_sorted, evidence_ids_sorted]
```

Insertion order cannot affect the digest.

## 6. Request and Response Contract

### 6.1 Request

One batch request is built per close session with exact fields:

- `schema_version`;
- `strategy_id` and `config_version`;
- market and UTC `as_of`;
- `decision_phase = POST_CLOSE`;
- `model_identity_policy_id`;
- `prompt_template_id` and digest;
- sorted exact candidate envelopes;
- `request_fingerprint`.

The request fingerprint tag is `llm-decision-request-sha256` over all preceding canonical fields and candidate IDs. It excludes invocation mode, attempt timestamps, provider response IDs, and transport metadata so record and replay resolve the same decision.

### 6.2 Response

The exact top-level response fields are:

- `schema_version`;
- echoed `request_fingerprint`;
- exact symbol-sorted `selections`;
- optional opaque `provider_response_id`.

Each selection contains exactly:

- symbol;
- selected action;
- confidence integer `0..100`;
- nonblank thesis;
- nonblank invalidation.

Target/weight/quantity/order fields and all unknown fields are forbidden. Extra, missing, duplicate, or reordered candidates; unsupported actions; fingerprint mismatch; malformed JSON; or partial coverage rejects the whole batch.

Response digest tag is `llm-decision-response-sha256` over the exact canonical response fields, excluding only `provider_response_id` because it is transport provenance rather than decision semantics.

## 7. Canonical Decisions, Attempts, and Replay

### 7.1 Stable successful decision

A canonical `LLMDecisionRecord` exists only for a fully validated successful record-mode decision. Its stable ID is:

```
llm-decision-sha256([request_fingerprint, response_digest])
```

It contains the request/response digests, validated selections, prompt/config provenance, opaque model identity/revision, provider response ID when present, and the successful record-mode start/end timestamps. These timestamps are provenance values but do not enter the record ID.

Exactly one successful decision may exist per request fingerprint:

- byte-identical reappend is idempotent;
- a different success for the same request fingerprint is a conflict;
- a different record ID that claims the same request fingerprint is also a conflict.

Only this stable decision ID enters `StrategyIntent.evidence_ids`.

### 7.2 Invocation attempts

Every record-mode invocation attempt has a separate `LLMInvocationAttempt`, including timeout, transport failure, malformed/no response, validation failure, success, or journal failure attempt. Attempt ID is supplied by the invocation boundary and must be unique; it is not derived from current time.

Attempt records contain normalized status/error, request fingerprint, optional response digest/provider response ID, and injected start/end timestamps. They never enter intents or `BacktestResult`.

A failure to persist an attempt is necessarily unattested; it is reported as `AUDIT_PERSISTENCE` and emits zero intents. The design does not claim the failed audit write was persisted.

### 7.3 Durable journal

`DuckDBLLMDecisionJournal` is a separate append-only audit store, not part of `PointInTimeStore`. One transaction atomically:

1. validates absence/idempotency/conflict for the request fingerprint and decision ID;
2. appends the terminal attempt row;
3. appends the successful canonical decision when applicable;
4. commits both or neither.

Failed attempts append only their attempt row atomically. Read methods return exact immutable domain values. Close/reopen must preserve records.

### 7.4 Replay

Replay performs exact lookup by request fingerprint and exact model-identity policy. It returns stored selections and the original stable decision ID. It does not create a replacement decision record and does not call a live transport.

Optional `LLMRunAttestation` records may state that an externally supplied execution label consumed decision IDs in `REPLAY` or `RECORD` mode. Attestations are outside `BacktestResult`, do not enter intent evidence, and cannot change trading-result bytes.

Therefore:

- immutable trading-result content is byte-identical between record and replay;
- invocation/access history can differ without pretending to be part of the trading result.

## 8. Atomic Failure Boundary and Intent Conversion

Atomicity applies to the Strategy A decision batch:

- no partial successful response;
- no partial decision/attempt journal append;
- no executable intents unless the successful canonical decision is committed;
- any timeout, transport, schema, envelope, identity-policy, conflict, or audit failure emits zero intents through one stable strategy-decision error.

This does not roll back the runner's already committed close valuation mark. That mark belongs to the runner/ledger authority and precedes strategy evaluation.

After a validated decision is committed or replayed, conversion to intents is total:

- target comes only from the envelope action mapping;
- identity/as-of come from the request;
- thesis/invalidation/confidence come from the validated selection;
- evidence is sorted market evidence plus portfolio snapshot ID, candidate ID, and stable decision ID;
- output is symbol-sorted.

The existing risk engine then evaluates the full tuple atomically. Strategy A cannot inspect or override the result.

## 9. Runner and Result Contract

The existing `Strategy.evaluate(context) -> tuple[StrategyIntent, ...]` boundary remains unchanged. Strategy A owns its provider and journal. The runner remains vendor-blind.

`BacktestResult` contains immutable trading content only. It is not modified to carry invocation mode. A separate run attestation/report records `record|replay`, execution label, and consumed decision IDs.

A fresh runner in replay mode over the same spec, market revisions, config, prompt, identity policy, and journal must return a byte-identical `BacktestResult`. Record mode is not called reproducible until an independent replay proves equality.

## 10. Real-Data Proof

Tests never use live network access. The proof workflow is:

1. fetch a frozen provider schedule outside pytest;
2. persist normalized provider-derived rows and provenance as a committed fixture;
3. pin symbol, market, session dates, config, initial cash, current-view policy, recorded selection, and expected nonfinal offensive session;
4. ensure at least `required_history + 1` sessions so the decision can execute next open;
5. run record mode with the declared recorded-fixture model identity;
6. close/reopen the decision journal;
7. run a fresh replay and require byte-identical intents and `BacktestResult`;
8. independently verify an order, next-open terminal fill, lot/position, and NAV change.

The proof is labeled exactly:

- PIT policy: `current-view-baseline/v1` business-availability PIT;
- price policy: raw unadjusted;
- LLM mode: `recorded-fixture`, not a live model call.

It does not claim strict historical ingestion-time reconstruction, adjusted total returns, model quality, or profitability. Live provider refresh is a separate opt-in verification step; fixture tests remain reproducible offline.

## 11. First Slice Scope

Included:

1. immutable config, feature, envelope, request, response, decision, attempt, and attestation models;
2. deterministic bars-only builder;
3. provider-neutral protocol;
4. recorded/replay provider;
5. durable append-only DuckDB decision journal;
6. bounded Strategy A adapter;
7. integration through existing risk/execution/ledger runner;
8. committed provider-derived fixture and honest real-data proof;
9. evidence of submitted order, next-open fill, position, and NAV change.

Deferred:

- concrete vendor HTTP adapters;
- news, events, fundamentals, corporate actions, benchmark regime, sector breadth, and FX;
- live scheduler;
- A/B/C/D/F aggregation;
- UI and notifications;
- profitability claims.

## 12. Rejected Alternatives

- **LLM chooses arbitrary symbols/weights:** violates deterministic authority.
- **Vendor-specific strategy class:** deployment detail leaks into domain identity.
- **Live model call during replay:** destroys reproducibility.
- **New replay decision record:** changes evidence IDs and falsifies byte equality.
- **Parse buy/sell prose:** negation/localization creates ambiguous authority.
- **Permissive schema or partial coverage:** permits silent mixed semantics.
- **Copy upstream runtime:** duplicates and weakens verified local authorities.
- **In-process-only journal:** cannot satisfy audit-before-intent across restart.

## 13. Verification Gates

Completion requires tests proving:

- strict immutable exact models with no subclass/polluted-instance bypass;
- total config/formula/equality/zero-volume semantics;
- no look-ahead and hostile-context-safe Decimal behavior;
- deterministic candidate ordering and every canonical digest from independent literal bytes;
- complete action table with no REDUCE that increases or preserves exposure;
- strict top-level response fingerprint and forbidden weight fields;
- extra/missing/duplicate/partial/malformed output fails atomically;
- decision uniqueness, attempt audit, idempotency, conflicts, transactions, and close/reopen durability;
- replay makes no transport call and reuses the original decision ID;
- record then replay yields byte-identical intents and `BacktestResult` while attestations differ externally;
- failed audit append emits no intents and does not claim rollback of close valuation;
- existing fixture strategy and Phase 1 suite remain unchanged;
- pinned provider-derived data produces an auditable order, next-open fill, position, and NAV change;
- evidence names the exact PIT/price/recorded-fixture limitations;
- Ruff, full pytest, diff checks, secret scan, one quality review, and any narrow closure review pass.
