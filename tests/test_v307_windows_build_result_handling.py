from __future__ import annotations

from pathlib import Path

BUILD = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
ARCHIVE = Path("docs/legacy/README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
DOC = Path("docs/legacy/V3_0_7_WINDOWS_BUILD_EXIT_CODE_FIX.md").read_text(encoding="utf-8")


def _pyinstaller_helper() -> str:
    start = BUILD.index("function Invoke-PyInstallerLogged")
    end = BUILD.index("# Use the pyinstaller.exe entry point first", start)
    return BUILD[start:end]


def test_pyinstaller_log_output_is_not_returned_as_part_of_exit_code():
    helper = _pyinstaller_helper()
    display = "$combined | Tee-Object -FilePath $LogPath | Out-Host"
    result = "return [int]($process.ExitCode)"

    assert display in helper
    assert result in helper
    assert helper.index(display) < helper.index(result)
    assert "$combined | Tee-Object -FilePath $LogPath" not in helper.splitlines()


def test_real_pyinstaller_failures_and_missing_executable_still_fail():
    assert "if ($pyinstallerExitCode -ne 0)" in BUILD
    assert 'throw "PyInstaller failed with exit code $pyinstallerExitCode. See $buildLog"' in BUILD
    assert "if (!(Test-Path $exePath))" in BUILD
    assert 'throw "PyInstaller completed but $exePath was not created. See $buildLog"' in BUILD


def test_v307_version_and_documentation():
    assert "IBKR Portable Trading Bot v3.0.17" in GUI
    assert "# IBKR Portable Trading Bot v3.0.17" in README
    assert 'version = "3.0.17"' in PYPROJECT
    assert "v3.0.7 Windows build result handling" in ARCHIVE
    assert "v3.0.7 Windows build result handling" in DOC
