#!/usr/bin/env python3
"""Install the paper-runtime LaunchAgent in the user GUI domain.

Idempotent and reversible. Only generates a plist from launchd.generate_plist
and drives launchctl with fixed no-shell argument vectors. Never stores secrets.

Usage:
    python scripts/install_paper_runtime_launchagent.py install \\
        --executable /path/to/stock-agent --config /path/to/config.yaml
    python scripts/install_paper_runtime_launchagent.py status
    python scripts/install_paper_runtime_launchagent.py uninstall
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from stock_agent.runtime.launchd import LABEL, generate_plist

_DOMAIN = "gui/$(id -u)"


def _reject_unsafe_path(path: Path, description: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{description} must be an absolute path: {path}")
    if path.is_symlink():
        raise ValueError(f"{description} must not be a symlink: {path}")


def _run(argv: list[str]) -> int:
    completed = subprocess.run(argv, capture_output=True, timeout=30, check=False)
    return completed.returncode


def _plist_path() -> Path:
    home = Path.home()
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(executable: Path, config_path: Path, work_dir: Path, log_dir: Path) -> int:
    _reject_unsafe_path(executable, "executable")
    _reject_unsafe_path(config_path, "config")
    if not executable.is_file():
        raise ValueError("executable must be a regular file")
    content = generate_plist(
        executable=executable,
        config_path=config_path,
        log_dir=log_dir,
        work_dir=work_dir,
        wake_entries=((9, 30), (16, 0)),
    )
    target = _plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return _run(["launchctl", "bootstrap", _DOMAIN, str(target)])


def status() -> int:
    return _run(["launchctl", "print", _DOMAIN + "/" + LABEL])


def uninstall() -> int:
    code = _run(["launchctl", "bootout", _DOMAIN + "/" + LABEL])
    path = _plist_path()
    if path.exists():
        path.unlink()
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_paper_runtime_launchagent")
    sub = parser.add_subparsers(dest="command", required=True)
    install_p = sub.add_parser("install")
    install_p.add_argument("--executable", required=True, type=Path)
    install_p.add_argument("--config", required=True, type=Path)
    install_p.add_argument("--work-dir", required=True, type=Path)
    install_p.add_argument("--log-dir", required=True, type=Path)
    sub.add_parser("status")
    sub.add_parser("uninstall")

    args = parser.parse_args(argv)
    if args.command == "install":
        return install(args.executable, args.config, args.work_dir, args.log_dir)
    if args.command == "status":
        return status()
    return uninstall()


if __name__ == "__main__":
    sys.exit(main())
