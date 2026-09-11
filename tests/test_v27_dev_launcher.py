from pathlib import Path


def test_run_dev_ps1_forces_real_windows_gui_clears_test_env_and_sets_fontdir():
    source = Path("scripts/run_dev.ps1").read_text(encoding="utf-8")
    assert '$env:QT_QPA_PLATFORM = "windows"' in source
    assert '$env:QT_QPA_FONTDIR = $windowsFonts' in source
    assert 'Join-Path $env:WINDIR "Fonts"' in source
    assert 'Remove-Item Env:\\IBKR_BOT_HEADLESS_SIGNALS' in source
    assert 'Remove-Item Env:\\PYTEST_DISABLE_PLUGIN_AUTOLOAD' in source
    assert 'Resolve-PythonLauncher' in source
    assert 'Restore-ProcessEnvironment' in source


def test_non_admin_run_dev_bat_launchers_exist_and_bypass_execution_policy_locally():
    root_bat = Path("run_dev.bat").read_text(encoding="utf-8")
    scripts_bat = Path("scripts/run_dev.bat").read_text(encoding="utf-8")
    for source in (root_bat, scripts_bat):
        assert "-ExecutionPolicy Bypass" in source
        assert "-NoProfile" in source
        assert "Set-ExecutionPolicy" not in source
        assert "QT_QPA_PLATFORM=windows" in source
        assert "IBKR_BOT_HEADLESS_SIGNALS=" in source
        assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=" in source
        assert "endlocal & exit /b %RUN_DEV_EXIT_CODE%" in source
    assert 'scripts\\run_dev.ps1' in root_bat
    assert 'run_dev.ps1' in scripts_bat


def test_run_tests_still_restores_headless_environment_but_build_uses_v22_method():
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    tests = Path("scripts/run_tests.ps1").read_text(encoding="utf-8")
    assert "Skipping full tests for faster, more reliable packaging." in build
    assert "[switch]$SkipTests" not in build
    assert '$env:QT_QPA_PLATFORM = "offscreen"' not in build
    assert 'IBKR_BOT_HEADLESS_SIGNALS' not in build
    assert '"QT_QPA_PLATFORM"' in tests
    assert '"QT_QPA_FONTDIR"' in tests
    assert '"IBKR_BOT_HEADLESS_SIGNALS"' in tests
    assert 'Restore-IbkrTestEnvironment' in tests


def test_lockfile_rejects_duplicate_in_process_before_pid_probe():
    source = Path("app/lockfile.py").read_text(encoding="utf-8")
    assert "_ACQUIRED_LOCK_PATHS" in source
    assert "def _lock_key" in source
    assert "if key in _ACQUIRED_LOCK_PATHS" in source
    assert "_ACQUIRED_LOCK_PATHS.add(key)" in source
    assert "_ACQUIRED_LOCK_PATHS.discard(key)" in source
