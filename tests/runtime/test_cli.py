from __future__ import annotations

import contextlib
import io

from typer.testing import CliRunner

from stock_agent.runtime.cli import (
    EXIT_INTERNAL_CORRUPTION,
    EXIT_KILL_SWITCH,
    EXIT_NO_WORK,
    EXIT_RECONCILIATION,
    EXIT_RETRYABLE_FAILURE,
    build_app,
    emit_envelope,
)

runner = CliRunner()


def test_help_is_available_and_lists_commands() -> None:
    result = runner.invoke(build_app(), ["--help"])
    assert result.exit_code == 0
    for command in ("run-once", "status", "report", "dry-run", "pause", "resume"):
        assert command in result.output


def test_status_and_dry_run_are_zero_work_by_default() -> None:
    assert runner.invoke(build_app(), ["status"]).exit_code == EXIT_NO_WORK
    assert runner.invoke(build_app(), ["dry-run"]).exit_code == EXIT_NO_WORK


def test_exit_codes_distinguish_outcomes() -> None:
    app = build_app(
        {
            "status": lambda: EXIT_RETRYABLE_FAILURE,
            "report": lambda: EXIT_RECONCILIATION,
            "pause": lambda: EXIT_KILL_SWITCH,
            "resume": lambda: EXIT_INTERNAL_CORRUPTION,
        }
    )
    assert runner.invoke(app, ["status"]).exit_code == EXIT_RETRYABLE_FAILURE
    assert runner.invoke(app, ["report"]).exit_code == EXIT_RECONCILIATION
    assert runner.invoke(app, ["pause"]).exit_code == EXIT_KILL_SWITCH
    assert runner.invoke(app, ["resume"]).exit_code == EXIT_INTERNAL_CORRUPTION


def test_envelope_is_bounded_and_sorted() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_envelope("ok", market="US")
    assert buf.getvalue().strip() == '{"market": "US", "outcome": "ok"}'


def test_cli_module_has_no_keychain_import() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "stock_agent" / "runtime" / "cli.py"
    ).read_text(encoding="utf-8")
    assert "keychain" not in source
