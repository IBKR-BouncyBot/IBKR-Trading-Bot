from __future__ import annotations

from app import lockfile


def test_windows_pid_probe_does_not_call_os_kill(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_windows_probe(pid: int) -> bool:
        calls.append(("windows", pid))
        return True

    def forbidden_kill(pid: int, signal_value: int) -> None:  # pragma: no cover - failure path
        raise AssertionError("os.kill must not be used for Windows PID probes")

    monkeypatch.setattr(lockfile.os, "name", "nt", raising=False)
    monkeypatch.setattr(lockfile, "_pid_is_running_windows", fake_windows_probe)
    monkeypatch.setattr(lockfile.os, "kill", forbidden_kill)

    assert lockfile._pid_is_running(12345) is True
    assert calls == [("windows", 12345)]


def test_windows_pid_probe_uses_process_exit_code_without_signalling():
    class FakeKernel32:
        def __init__(self, exit_code: int):
            self.exit_code = exit_code
            self.opened: list[tuple[int, bool, int]] = []
            self.closed: list[int] = []

        def OpenProcess(self, access: int, inherit: bool, pid: int):
            self.opened.append((access, inherit, pid))
            return 77

        def GetExitCodeProcess(self, handle: int, pointer) -> bool:
            pointer._obj.value = self.exit_code
            return True

        def CloseHandle(self, handle: int) -> bool:
            self.closed.append(handle)
            return True

    running = FakeKernel32(exit_code=259)
    stopped = FakeKernel32(exit_code=0)

    assert lockfile._pid_is_running_windows(456, kernel32=running, get_last_error=lambda: 0) is True
    assert lockfile._pid_is_running_windows(456, kernel32=stopped, get_last_error=lambda: 0) is False
    assert running.opened and running.closed == [77]
    assert stopped.opened and stopped.closed == [77]
