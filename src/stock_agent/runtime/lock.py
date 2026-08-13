from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockHeldError(Exception):
    """Another local process already holds the advisory lock."""


class ProcessLock:
    """A user-only advisory file lock that excludes concurrent local processes.

    Uses ``fcntl.flock`` so the kernel releases the lock automatically when the
    owning process exits (stale exit cannot wedge the runtime), and never
    deletes the lock file (so it cannot destroy another process's authority).
    """

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        if path.exists() and path.is_symlink():
            raise ValueError("lock path must not be a symlink")
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ProcessLock:
        if not self.acquire():
            raise LockHeldError("another local process holds the lock")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
