import re
from collections.abc import Callable
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"


def _assert_ci_workflow_contract(workflow: str) -> None:
    expected_header = """name: CI

on:
  pull_request:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read

"""
    assert workflow.startswith(expected_header)

    required_fragments = (
        "cancel-in-progress: true",
        "runs-on: ubuntu-latest",
        "timeout-minutes: 15",
        "python-version: '3.11'",
        "fetch-depth: 0",
        "enable-cache: true",
        "uv sync --locked --all-groups",
        "uv run pytest -q",
        "uv run ruff check .",
        "git diff --check",
        "github.event.pull_request.base.sha",
        "github.event.pull_request.head.sha",
        "github.event.before",
        "github.sha",
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    )
    for fragment in required_fragments:
        assert fragment in workflow

    assert "pull_request_target" not in workflow
    assert "schedule:" not in workflow
    assert "continue-on-error" not in workflow
    assert re.search(r"\$\{\{\s*secrets(?:\.|\[)", workflow, re.IGNORECASE) is None
    assert re.search(r"(?im)^\s*(?:[\w-]+:\s*write|write-all)\s*$", workflow) is None


def test_ci_workflow_is_bounded_and_secret_free() -> None:
    _assert_ci_workflow_contract(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.replace("  contents: read", "  contents: read\n  issues: write"),
        lambda value: value + "\n# ${{ secrets['TOKEN'] }}\n",
        lambda value: value.replace(
            "  workflow_dispatch:",
            "  schedule:\n    - cron: '0 0 * * *'\n  workflow_dispatch:",
        ),
    ),
)
def test_ci_contract_rejects_unauthorized_mutations(
    mutation: Callable[[str], str],
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_ci_workflow_contract(mutation(workflow))
