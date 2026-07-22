from __future__ import annotations

from pathlib import Path

WINDOWS_LAUNCHERS = [
    Path("build_windows.bat"),
    Path("run_dev.bat"),
    Path("run_all_tests.bat"),
    Path("scripts/build_windows.bat"),
    Path("scripts/run_dev.bat"),
]

POWERSHELL_SCRIPTS = [
    Path("scripts/build_windows.ps1"),
    Path("scripts/build_windows_checked.ps1"),
    Path("scripts/run_dev.ps1"),
    Path("scripts/run_tests.ps1"),
]


def test_root_run_all_tests_bat_runs_complete_local_validation_suite():
    source = Path("run_all_tests.bat").read_text(encoding="utf-8")
    assert Path("run_all_tests.bat").exists()
    assert r"scripts\run_tests.ps1" in source
    assert r"scripts\run_quality_checks.py" in source
    assert "set IBKR_BOT_NO_PAUSE=1" in source
    assert "pause" in source.lower()
    assert "endlocal & exit /b %TEST_EXIT_CODE%" in source
    assert ":quality_failed" in source
    assert 'if not "%TEST_EXIT_CODE%"=="0" goto quality_failed' in source


def test_all_windows_batch_launchers_pause_and_preserve_exit_code():
    for path in WINDOWS_LAUNCHERS:
        source = path.read_text(encoding="utf-8")
        assert "pause" in source.lower(), path
        assert "exit /b %" in source, path
        assert "setlocal" in source.lower(), path


def test_powershell_launchers_pause_when_run_directly_but_can_be_suppressed_by_batch_wrappers():
    for path in POWERSHELL_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        assert "IBKR_BOT_NO_PAUSE" in source, path
        assert "function Wait-IbkrScriptPause" in source, path
        assert 'Read-Host "Press Enter to exit"' in source, path
        assert source.rstrip().endswith("Wait-IbkrScriptPause"), path
