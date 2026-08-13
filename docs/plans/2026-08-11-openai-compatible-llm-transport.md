# OpenAI-Compatible LLM Transport Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. One capability-level specification review and one quality review; do not create per-microtask review loops.

**Goal:** Add a secret-safe, bounded OpenAI-compatible Chat Completions transport that feeds the existing recorded/replay provider and supports fixed OpenAI and DeepSeek endpoint profiles.

**Architecture:** Keep vendor names at adapter factories. A deterministic prompt encoder feeds a credential-owning stdlib HTTPS POST client; a strict provider-envelope parser returns the existing `RawLLMResponse`. Existing provider-neutral validation and journal atomicity remain authoritative.

**Tech stack:** Python 3.11 stdlib (`http.client`, `ssl`, `json`, `hashlib`), Pydantic contracts already in the project, pytest, Ruff.

---

### Task 1: Freeze prompt and profile contracts

**Files:**
- Create: `tests/strategies/test_openai_compatible.py`
- Create: `src/stock_agent/strategies/openai_compatible.py`

**RED:** Import missing prompt/profile symbols. Assert exact immutable OpenAI/DeepSeek host/path profiles, strict model/max-token values, computed prompt ID/digest, deterministic request JSON, Decimal/datetime encoding, exact message/body shape, no vendor secret fields, and prompt mismatch rejection before collaborators are touched.

**Run:** `uv run pytest tests/strategies/test_openai_compatible.py -q`

**GREEN:** Implement immutable exact dataclasses/constants and pure request/body encoding only. No HTTP yet.

**Verify:** focused test plus `uv run ruff check src/stock_agent/strategies/openai_compatible.py tests/strategies/test_openai_compatible.py`.

### Task 2: Add secret-safe bounded HTTPS POST

**Files:**
- Create: `tests/strategies/test_llm_http.py`
- Create: `src/stock_agent/strategies/llm_http.py`

**RED:** Assert environment token source behavior; token validation; exact host/path/body admissions; TLS-only POST headers; no redirect/compression/oversize response; timeout normalization; close-on-all-paths; no native cause/context; and canary absence from public exception graph and every `stock_agent.strategies.llm_http` traceback frame.

**Run:** `uv run pytest tests/strategies/test_llm_http.py -q`

**GREEN:** Implement `BearerTokenSource`, environment-backed source, safe error descriptor/public error, and stdlib bounded HTTPS client with an injected connection factory for deterministic tests.

**Verify:** focused test, existing data transport tests, Ruff.

### Task 3: Parse provider envelopes into the existing port

**Files:**
- Modify: `src/stock_agent/strategies/openai_compatible.py`
- Modify: `tests/strategies/test_openai_compatible.py`

**RED:** Add mutation cases for malformed bytes/JSON, duplicate keys, provider errors, zero/multiple choices, non-stop finish, wrong role, blank content, missing ID/model, model-supplied response ID, and exact extraction of provider ID/model.

**GREEN:** Implement `OpenAICompatibleChatTransport.invoke()` returning `RawLLMResponse`; set revision marker to `api-model-id:<returned-model>`; never retry.

**Verify:** focused tests and existing `tests/strategies/test_llm_provider.py`.

### Task 4: Prove producer-to-consumer record mode

**Files:**
- Modify: `tests/strategies/test_openai_compatible.py`

**RED/GREEN:** Drive a real `LLMDecisionRequest` through transport, `RecordedLLMDecisionProvider`, `LLMDecisionJournal`, and exact identity policy using a deterministic fake HTTP client. Assert one HTTP call, authoritative provider response ID, successful attempt/decision atomicity, and replay without transport.

**Verify:** `uv run pytest tests/strategies/test_openai_compatible.py tests/strategies/test_llm_provider.py tests/strategies/test_llm_journal.py -q`.

### Task 5: Add opt-in live smoke entrypoint

**Files:**
- Create: `scripts/verify_live_llm_transport.py`
- Create: `tests/scripts/test_verify_live_llm_transport.py`
- Modify: `.gitignore` only if a generated local artifact needs an explicit ignored path.

**RED:** Assert provider/model/env-var are explicit; API key cannot be supplied as a CLI option; dry construction does not read the environment; output is secret-free; missing credentials fail with a fixed message; no output artifact is written on failure.

**GREEN:** Build the provider profile, prompt-aligned Strategy A request, exact identity policy, temporary journal, and one record invocation. Emit only canonical nonsecret identifiers/status. Keep live execution outside pytest and require explicit `--execute`.

**Verify without key:** CLI help, construction tests, full secret scan. Do not execute the credential path yet.

### Task 6: Capability-level review and pre-key verification

**Specification review:** Check every design invariant, ownership boundary, failure row, no-retry rule, and provider-neutral domain constraint.

**Quality review:** Check stdlib HTTP correctness, resource closure, exception secrecy, test mutation strength, and false-green paths.

**Fresh gates:**

- `uv run pytest tests/strategies/test_llm_http.py tests/strategies/test_openai_compatible.py tests/scripts/test_verify_live_llm_transport.py -q`
- `uv run pytest -q`
- `uv run ruff check .`
- `git diff --check`
- tracked/untracked credential-shaped scan

Commit and push the feature branch only after all gates pass.

### Task 7: Real provider smoke test

**Prerequisite:** User explicitly chooses OpenAI or DeepSeek and supplies the key through a named environment variable. Never read browser cookies, Keychain, password stores, shell history, or unrelated environment variables.

**Run:** one explicit live invocation with a low bounded `max_tokens`, then verify the journaled attempt/decision and secret-free output. Do not retry automatically. If the provider returns a different model ID than the declared exact policy, report the observed safe model ID and revise configuration only with an explicit, honest identity policy.
