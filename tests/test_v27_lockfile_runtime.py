from pathlib import Path

import pytest

from app.lockfile import _ACQUIRED_LOCK_PATHS, SingleInstanceError, SingleInstanceLock


def test_duplicate_in_process_lock_does_not_probe_pid(monkeypatch, tmp_path: Path):
    calls: list[int] = []

    def fake_pid_probe(pid: int) -> bool:
        calls.append(pid)
        return True

    monkeypatch.setattr("app.lockfile._pid_is_running", fake_pid_probe)
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(SingleInstanceError):
            second.acquire()
        assert calls == []
    finally:
        first.release()
        _ACQUIRED_LOCK_PATHS.clear()

    assert str(lock_path) not in _ACQUIRED_LOCK_PATHS
