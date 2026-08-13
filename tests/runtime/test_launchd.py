from __future__ import annotations

from pathlib import Path

from stock_agent.runtime.launchd import (
    LABEL,
    generate_plist,
    load_plist,
    validate_with_plutil,
)

EXECUTABLE = Path("/usr/local/bin/stock-agent")
CONFIG = Path("/Users/test/.hermes/my-stock-agent/config.yaml")
LOG_DIR = Path("/Users/test/.hermes/my-stock-agent/logs")
WORK_DIR = Path("/Users/test/.hermes/my-stock-agent")


def make_plist() -> bytes:
    return generate_plist(
        executable=EXECUTABLE,
        config_path=CONFIG,
        log_dir=LOG_DIR,
        work_dir=WORK_DIR,
        wake_entries=((9, 30), (16, 0)),
    )


def test_plist_has_absolute_paths_and_no_secrets() -> None:
    plist = load_plist(make_plist())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"][0] == str(EXECUTABLE)
    assert plist["WorkingDirectory"] == str(WORK_DIR)
    assert plist["StandardOutPath"].startswith("/")
    assert plist["StandardErrorPath"].startswith("/")
    assert "EnvironmentVariables" not in plist
    assert "Credential" not in plist


def test_calendar_interval_background_and_no_keepalive() -> None:
    plist = load_plist(make_plist())
    assert plist["ProcessType"] == "Background"
    assert "KeepAlive" not in plist
    intervals = plist["StartCalendarInterval"]
    assert {"Hour": 9, "Minute": 30} in intervals
    assert {"Hour": 16, "Minute": 0} in intervals


def test_umask_and_working_directory() -> None:
    plist = load_plist(make_plist())
    assert plist["Umask"] == 0o077
    assert plist["WorkingDirectory"] == str(WORK_DIR)


def test_multiple_wake_entries_are_preserved() -> None:
    content = generate_plist(
        executable=EXECUTABLE,
        config_path=CONFIG,
        log_dir=LOG_DIR,
        work_dir=WORK_DIR,
        wake_entries=((9, 30), (11, 30), (13, 0), (15, 0)),
    )
    intervals = load_plist(content)["StartCalendarInterval"]
    assert len(intervals) == 4


def test_empty_wake_entries_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        generate_plist(
            executable=EXECUTABLE,
            config_path=CONFIG,
            log_dir=LOG_DIR,
            work_dir=WORK_DIR,
            wake_entries=(),
        )


def test_plutil_lint_passes() -> None:
    validate_with_plutil(make_plist())
