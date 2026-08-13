from __future__ import annotations

from pathlib import Path

import pytest

from stock_agent.runtime.lock import LockHeldError, ProcessLock


def test_lock_excludes_concurrent_processes(tmp_path: Path) -> None:
    path = tmp_path / "paper-runtime.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_release_allows_reacquire_without_deleting_lock_file(tmp_path: Path) -> None:
    path = tmp_path / "paper-runtime.lock"
    lock = ProcessLock(path)
    assert lock.acquire() is True
    lock.release()
    # The lock file still exists, but the lock is released.
    assert path.exists()
    assert lock.acquire() is True
    lock.release()


def test_context_manager_raises_when_held(tmp_path: Path) -> None:
    path = tmp_path / "paper-runtime.lock"
    holder = ProcessLock(path)
    assert holder.acquire() is True
    with pytest.raises(LockHeldError):
        with ProcessLock(path):
            pass
    holder.release()


def test_symlink_lock_path_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.lock"
    real.write_text("")
    link = tmp_path / "link.lock"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        ProcessLock(link)
