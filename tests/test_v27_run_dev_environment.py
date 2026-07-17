from pathlib import Path


def test_run_dev_clears_headless_test_environment_and_forces_real_windows_qt():
    source = Path("scripts/run_dev.ps1").read_text(encoding="utf-8")
    assert '$env:QT_QPA_PLATFORM = "windows"' in source
    assert 'Remove-Item Env:\\IBKR_BOT_HEADLESS_SIGNALS' in source
    assert 'Remove-Item Env:\\PYTEST_DISABLE_PLUGIN_AUTOLOAD' in source
    assert 'Remove-Item Env:\\PYTHONDONTWRITEBYTECODE' in source
    assert 'QT_QPA_FONTDIR' in source
    assert '& $python main.py' in source


def test_run_dev_bat_launches_powershell_without_admin_policy_change():
    script_bat = Path("scripts/run_dev.bat").read_text(encoding="utf-8")
    root_bat = Path("run_dev.bat").read_text(encoding="utf-8")
    for source in (script_bat, root_bat):
        assert "powershell.exe" in source
        assert "-NoLogo -NoProfile" in source
        assert "-ExecutionPolicy Bypass" in source
        assert "set RUN_DEV_EXIT_CODE=%ERRORLEVEL%" in source
        assert "exit /b %RUN_DEV_EXIT_CODE%" in source
    assert 'run_dev.ps1' in script_bat
    assert 'scripts\\run_dev.ps1' in root_bat


def test_run_tests_restores_headless_environment_build_is_v22_legacy():
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    run_tests = Path("scripts/run_tests.ps1").read_text(encoding="utf-8")
    assert "Skipping full tests for faster, more reliable packaging." in build
    assert "IBKR_BOT_HEADLESS_SIGNALS" not in build
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in build
    assert "QT_QPA_FONTDIR" not in build
    for source in (run_tests,):
        assert 'QT_QPA_PLATFORM' in source
        assert 'IBKR_BOT_HEADLESS_SIGNALS' in source
        assert 'PYTEST_DISABLE_PLUGIN_AUTOLOAD' in source
        assert 'QT_QPA_FONTDIR' in source
        assert '[Environment]::GetEnvironmentVariable($name, "Process")' in source
        assert '[Environment]::SetEnvironmentVariable($name, $oldValue, "Process")' in source
        assert 'finally {' in source
    assert 'Restore-IbkrTestEnvironment' in run_tests


def test_v27_docs_mention_development_launcher_cleanup():
    readme = Path("README.md").read_text(encoding="utf-8")
    docs = Path("docs/legacy/V2_12_WINDOWS_RUNTIME_QT_CLEANUP.md").read_text(encoding="utf-8")
    assert "run_dev.bat" in readme
    assert "QT_QPA_PLATFORM" in docs
    assert "ExecutionPolicy Bypass" in docs
