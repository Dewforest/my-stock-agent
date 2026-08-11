# Minimal GitHub CI Design

## Goal

Turn the repository's existing local quality gates into a visible, secret-free GitHub pull-request check before either stacked PR is merged.

## Pressure-tested scope

The failure to prevent is simple: a branch can look green locally while GitHub has no independent check. The smallest adequate response is one workflow, one Python version, one job, and the exact gates already used by the project.

Rejected alternatives:

- A multi-version/OS matrix: unsupported complexity; the project declares Python 3.11 and currently targets one development environment.
- Reusable workflows and composite actions: abstraction before a second consumer exists.
- Coverage thresholds, packaging, release, deployment, or live-provider checks: separate capabilities with different failure and credential boundaries.

## Workflow contract

Create `.github/workflows/ci.yml` on `feature/phase-1-trading-core`.

Triggers:

- every pull request;
- pushes to `main`;
- manual dispatch.

Authority and resource bounds:

- top-level `contents: read` only;
- one job on `ubuntu-latest`;
- Python 3.11;
- 15-minute timeout;
- concurrency keyed by workflow and ref, with stale runs cancelled;
- no repository secrets, live providers, network market-data calls, or LLM calls.

Steps:

1. Check out full history so PR-range whitespace validation is meaningful.
2. Install `uv` with dependency caching.
3. Install exactly the locked dependency graph using `uv sync --locked --all-groups`.
4. Run `uv run pytest -q`.
5. Run `uv run ruff check .`.
6. Run `git diff --check` against the pull request base/head SHAs; on a push, check the pushed commit range.

## Failure semantics

Any command failure fails the single CI job. There are no retries or allow-failure branches. Dependency-lock drift fails at install time. The workflow never degrades to an unlocked resolution.

## Verification

A repository contract test reads the workflow as an artifact and rejects missing triggers, excessive permissions, absent bounds, unlocked installation, missing gates, shallow checkout, or secret references. After local RED/GREEN, the branch is pushed and GitHub's actual check run is the external acceptance test.
