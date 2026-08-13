from __future__ import annotations

import plistlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

LABEL = "com.dewforest.my-stock-agent.paper-runtime"


def generate_plist(
    *,
    executable: Path,
    config_path: Path,
    log_dir: Path,
    work_dir: Path,
    wake_entries: tuple[tuple[int, int], ...],
) -> bytes:
    """Generate a secret-free user LaunchAgent plist.

    The plist only wakes ``run-once`` and owns no business semantics: no
    ``KeepAlive`` loop, no ``EnvironmentVariables``, absolute paths everywhere,
    a background process type, and a restrictive umask.
    """
    if not wake_entries:
        raise ValueError("wake_entries must not be empty")
    for entry in wake_entries:
        if len(entry) != 2 or not (0 <= entry[0] < 24 and 0 <= entry[1] < 60):
            raise ValueError("wake_entries must be (hour, minute) pairs")

    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable),
            "run-once",
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(work_dir),
        "ProcessType": "Background",
        "StartCalendarInterval": [
            {"Hour": hour, "Minute": minute} for hour, minute in wake_entries
        ],
        "Umask": 0o077,
        "StandardOutPath": str(log_dir / "paper-runtime.stdout.log"),
        "StandardErrorPath": str(log_dir / "paper-runtime.stderr.log"),
    }
    return plistlib.dumps(plist)


def load_plist(content: bytes) -> dict[str, Any]:
    return plistlib.loads(content)


def validate_with_plutil(content: bytes) -> None:
    """Validate plist syntax with the real ``plutil -lint`` in a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "agent.plist"
        path.write_bytes(content)
        completed = subprocess.run(
            ["plutil", "-lint", str(path)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("plist failed plutil -lint")
