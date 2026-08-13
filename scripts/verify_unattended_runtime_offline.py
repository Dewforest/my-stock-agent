#!/usr/bin/env python3
"""Offline acceptance gate for the unattended dual-market paper runtime.

Runs the vertical-slice and crash-recovery suites plus the full test battery,
lint, and whitespace checks. Produces zero external effects: frozen fixtures,
fake Keychain sources, and injected clocks only.
"""

from __future__ import annotations

import subprocess
import sys


def _run(argv: list[str]) -> int:
    print(f"\n$ {' '.join(argv)}")
    completed = subprocess.run(argv, check=False)
    return completed.returncode


def main() -> int:
    steps = [
        [
            "uv",
            "run",
            "pytest",
            "tests/runtime/test_unattended_vertical.py",
            "tests/runtime/test_crash_matrix.py",
            "-q",
        ],
        ["uv", "run", "pytest", "-q"],
        ["uv", "run", "ruff", "check", "."],
        ["git", "diff", "--check"],
    ]
    for step in steps:
        code = _run(step)
        if code != 0:
            print(f"\nFAILED: {' '.join(step)}")
            return code
    print("\nAll offline acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
