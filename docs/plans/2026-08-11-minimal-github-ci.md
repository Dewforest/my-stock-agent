# Minimal GitHub CI Implementation Plan

> **For Hermes:** Execute task-by-task with strict RED → GREEN → verification.

**Goal:** Add one bounded, secret-free GitHub Actions job that enforces the repository's existing pytest, Ruff, and whitespace gates.

**Architecture:** Treat the workflow as a versioned repository contract. A small pytest contract test protects the security and command invariants; GitHub Actions remains the runtime authority.

**Tech Stack:** GitHub Actions, Python 3.11, uv, pytest, Ruff, git.

---

### Task 1: Lock the CI contract with RED

**Files:**
- Create: `tests/ci/test_ci_workflow.py`
- Expected missing artifact: `.github/workflows/ci.yml`

1. Write a test requiring the exact workflow path and the approved triggers, read-only permissions, timeout, concurrency cancellation, full-history checkout, locked uv sync, pytest, Ruff, and PR-range diff-check.
2. Reject `${{ secrets.* }}` and commands for live providers or LLM transports.
3. Run `uv run pytest tests/ci/test_ci_workflow.py -q`.
4. Expected: FAIL because `.github/workflows/ci.yml` does not exist.

### Task 2: Add the minimal workflow

**Files:**
- Create: `.github/workflows/ci.yml`

1. Implement only the contract in the design document.
2. Run `uv run pytest tests/ci/test_ci_workflow.py -q`.
3. Expected: PASS.
4. Run `uv run pytest -q`, `uv run ruff check .`, and `git diff --check`.
5. Commit the design, plan, test, and workflow together with `ci: add pull request quality gates`.

### Task 3: Verify GitHub execution

1. Push `feature/phase-1-trading-core`.
2. Confirm PR #1 reports the CI job and reaches success.
3. Confirm stacked PR #2 receives the workflow from its updated base and reaches success; if GitHub does not emit a new synchronize event, refresh the PR branch without rewriting history.
4. Do not merge either PR.
