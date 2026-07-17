from __future__ import annotations

from pathlib import Path


class _FakeKernel32:
    def __init__(self, exit_code: int = 259):
        self.exit_code = exit_code
        self.opened = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Win32 API name
        self.opened.append((access, inherit, pid))
        return 12345

    def GetExitCodeProcess(self, handle, out_code):  # noqa: N802 - Win32 API name
        out_code._obj.value = self.exit_code
        return True

    def CloseHandle(self, handle):  # noqa: N802 - Win32 API name
        self.closed.append(handle)
        return True


class _FakeWindll:
    def __init__(self, kernel32):
        self.kernel32 = kernel32


def test_windows_pid_check_does_not_send_ctrl_c_event(monkeypatch):
    from app import lockfile

    def forbidden_kill(pid, signal):
        raise AssertionError("Windows PID checks must not call os.kill(pid, 0)")

    kernel32 = _FakeKernel32(exit_code=259)
    monkeypatch.setattr(lockfile.os, "name", "nt", raising=False)
    monkeypatch.setattr(lockfile.os, "kill", forbidden_kill)
    monkeypatch.setattr(lockfile.ctypes, "windll", _FakeWindll(kernel32), raising=False)

    assert lockfile._pid_is_running(4321) is True
    assert kernel32.opened
    assert kernel32.closed == [12345]


def test_windows_pid_check_detects_exited_process(monkeypatch):
    from app import lockfile

    kernel32 = _FakeKernel32(exit_code=0)
    monkeypatch.setattr(lockfile.os, "name", "nt", raising=False)
    monkeypatch.setattr(lockfile.ctypes, "windll", _FakeWindll(kernel32), raising=False)

    assert lockfile._pid_is_running(4321) is False


def test_windows_build_reverted_to_v22_startprocess_only_for_pyinstaller():
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    run_tests = Path("scripts/run_tests.ps1").read_text(encoding="utf-8")
    assert "Start-Process" in build
    assert "Invoke-PyInstallerLogged" in build
    assert 'Invoke-Checked "Run pytest"' in build
    assert "build_pytest.log" not in build
    assert "Tee-Object -FilePath $LogPath" in build
    assert "Tee-Object -FilePath $LogPath" in run_tests


def test_lockfile_documents_windows_ctrl_c_regression():
    source = Path("app/lockfile.py").read_text(encoding="utf-8")
    assert "CTRL_C_EVENT" in source
    assert "Terminate batch job" in source
    assert "def _pid_is_running_windows" in source
    assert "os.kill(pid, 0)" in source
    assert 'if os.name == "nt"' in source
