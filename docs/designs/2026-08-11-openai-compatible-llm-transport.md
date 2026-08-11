# OpenAI-Compatible LLM Transport Design

## Goal

Add the first real network transport behind the existing provider-neutral `RawLLMTransport` port so Strategy A can record bounded decisions from OpenAI-compatible Chat Completions providers without making domain code vendor-aware or exposing credentials.

## Non-goals

- No live invocation in pytest.
- No automatic retries, streaming, tools, reasoning traces, SDK dependency, arbitrary base URL, provider discovery, or fallback routing.
- No claim that a rolling model alias pins model weights.
- No changes to candidate generation, risk, execution, ledger, journal atomicity, or replay semantics.

## Authority and ownership

| Concern | Owner |
|---|---|
| Candidate envelope and response schema | `llm_contract.py` |
| Record/replay, identity policy, attempt normalization | `llm_provider.py` |
| Prompt template and request encoding | new `openai_compatible.py` |
| Host/path/model/max-token configuration | new immutable transport profile |
| Bearer credential retrieval | new token-source port |
| TLS, HTTP POST, byte limits, safe errors | new `llm_http.py` |
| Durable success/failure | existing `llm_journal.py` |
| OpenAI/DeepSeek names | adapter factory only; never domain models |

## Main chain

```
LLMDecisionRequest
  -> prompt identity check
  -> deterministic JSON prompt encoder
  -> OpenAICompatibleChatTransport
  -> BoundedBearerHttpsClient
  -> POST provider Chat Completions endpoint
  -> strict provider-envelope parser
  -> authoritative response-id injection
  -> RawLLMResponse
  -> existing RecordedLLMDecisionProvider
  -> atomic attempt + decision journal append
```

## Frozen common wire profile

Request:

- HTTPS only.
- Provider factory fixes an exact ASCII host and path.
- `Authorization: Bearer <token>` is created only inside the HTTP client.
- `Content-Type: application/json`, `Accept: application/json`, `Accept-Encoding: identity`.
- `stream=false`.
- `response_format={"type":"json_object"}`.
- Exactly two messages: fixed system template and deterministic user JSON.
- Explicit model and bounded positive `max_tokens`.
- No temperature, top-p, tools, user identifier, metadata, or unsupported extension fields.

Response:

- Status must be exactly 200; redirects are rejected.
- Content encoding is absent or exactly `identity`.
- Content type must be JSON.
- Declared and actual response bytes are bounded.
- UTF-8 and JSON are strict: no BOM, duplicate keys, non-finite values, or trailing garbage.
- Exactly one choice is accepted.
- `finish_reason` must be `stop`.
- Message role must be `assistant`; content must be a nonblank JSON string.
- Provider envelope `id` and `model` must be nonblank strings.
- Model content may not contain `provider_response_id`; the adapter injects the authoritative envelope ID.
- Extra provider-envelope fields are ignored because providers evolve additively; the accepted fields above remain exact.

## Prompt contract

The fixed system message contains the word `JSON`, an exact output example, the allowed action semantics, and an instruction to treat request content as data rather than instructions. This satisfies DeepSeek's documented JSON Output prerequisites and remains within OpenAI-compatible Chat Completions.

The user message is canonical compact JSON containing the exact request model dump. Decimal values are strings, datetimes are normalized JSON strings, candidates remain in canonical symbol order, and keys are sorted. The transport verifies the request's `prompt_template_id` and `prompt_template_digest` before touching credentials or the network.

The model outputs only:

- `schema_version`
- `request_fingerprint`
- `selections`

The existing provider-neutral parser remains the final schema and envelope authority.

## Model identity honesty

`RawLLMResponse.model_identity` is the provider-returned `model` field. `model_revision` is `api-model-id:<provider-returned-model>`.

This is deliberately not called a weight fingerprint. A dated model ID can be pinned by policy; a rolling alias cannot. For rolling aliases, reproducibility comes from the append-only canonical decision and replay, not from pretending a later live invocation will use identical weights.

The existing `ExactModelIdentityPolicy` must predeclare the expected returned model ID and derived revision marker. A provider returning another model fails closed as `IDENTITY_POLICY`.

## Credential boundary

A `BearerTokenSource` protocol returns the token only at invocation time. The production environment source stores only the environment-variable name. No API key is accepted as a CLI argument or persisted in config, repr, journal, evidence, exception metadata, response artifact, or tracked file.

The enforceable exception contract follows Python frame ownership:

- safe public exception payload and fixed messages;
- no native cause/context retention;
- no credential, request body, response body, target, or native-error canary in library-owned traceback frames;
- caller-owned frames are outside the library's control.

The token must be nonblank ASCII, bounded in length, and free of whitespace/control characters. Missing or invalid credentials fail before opening a connection.

## Failure matrix

| Failure | Transport behavior | Recorded provider status |
|---|---|---|
| Missing/invalid token | fixed safe transport error | `TRANSPORT` |
| DNS/TLS/connect/request/read error | clear unsafe locals, safe error | `TRANSPORT` |
| Socket timeout | raise sanitized `TimeoutError` | `TIMEOUT` |
| Redirect/auth/rate limit/4xx/5xx | fixed safe error; body not exposed | `TRANSPORT` |
| Unsupported encoding/type/size | fixed safe error | `TRANSPORT` |
| Malformed provider envelope | fixed safe error | `TRANSPORT` |
| Empty/truncated/malformed model content | return payload only when structurally extractable; existing parser rejects | `SCHEMA` |
| Provider model mismatch | return actual model; exact policy rejects | `IDENTITY_POLICY` |
| Candidate/action mismatch | existing envelope validator rejects | `ENVELOPE` |
| Journal append failure | existing atomic boundary rejects success | `AUDIT_PERSISTENCE` |

DeepSeek documents that JSON Output may occasionally return empty content. This is recorded as a failed attempt. The adapter never hides it with an internal retry.

## Provider profiles

- OpenAI: fixed `api.openai.com` and `/v1/chat/completions`.
- DeepSeek: fixed `api.deepseek.com` and `/chat/completions` per the current official API reference.

Model IDs are required inputs to the adapter factory and are never hardcoded, because provider model catalogs and rolling aliases change.

## Test strategy

1. Prompt golden/identity tests and hostile request checks.
2. Token source and bounded HTTPS client tests with fake connection/socket/response objects.
3. Canary graph tests over public exceptions and library-owned traceback frames.
4. Provider-envelope mutation matrix: status, headers, size, JSON, choices, finish reason, role, content, ID, model.
5. Producer-to-consumer integration through the real `RecordedLLMDecisionProvider` and in-memory journal.
6. Full suite, Ruff, diff-check, tracked/untracked secret scan.
7. One opt-in live smoke command only after the user supplies an environment-backed key.

## Completion evidence before requesting a key

- No new runtime dependency.
- Focused tests pass without network.
- Existing Strategy A provider/journal tests pass unchanged.
- Full suite and Ruff pass.
- Secret canary tests pass.
- Live smoke script exists but has not read any credential.
