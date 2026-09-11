from pathlib import Path


def test_windows_build_reverted_to_v22_legacy_method_without_headless_test_gate():
    source = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$RunTests" in source
    assert "[switch]$SkipTests" not in source
    assert "Skipping full tests for faster, more reliable packaging." in source
    assert 'Invoke-Checked "Run pytest"' in source
    assert 'Invoke-Checked "Run CSV simulation fixtures"' in source
    assert 'build_pytest.log' not in source
    assert '$env:IBKR_BOT_HEADLESS_SIGNALS = "1"' not in source
    assert '& $FilePath @Arguments 2>&1 | Tee-Object -FilePath $LogPath' not in source


def test_windows_bat_uses_v22_simple_powershell_launch():
    source = Path("scripts/build_windows.bat").read_text(encoding="utf-8")
    assert 'powershell -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*' in source
    assert "set IBKR_BOT_NO_PAUSE=1" in source
    assert "pause" in source.lower()
    assert "endlocal & exit /b %BUILD_EXIT_CODE%" in source


def test_controller_has_headless_signal_mode_for_tests_outside_legacy_build():
    source = Path("app/controller.py").read_text(encoding="utf-8")
    assert 'IBKR_BOT_HEADLESS_SIGNALS' in source
    assert 'class _HeadlessSignalInstance' in source
    assert 'from PySide6.QtCore import QObject, Signal' in source


def test_timeline_widget_expands_and_uses_dynamic_axis_width():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "self.setMinimumHeight(300 if self.compact else 320)" in source
    assert "QSizePolicy.Fixed if self.compact else QSizePolicy.Preferred" in source
    assert "axis_width =" in source
    assert "def axis_width_for" in source
    assert "_cycle_capture_time_window" in source


def test_run_tests_ps1_uses_direct_logged_native_invocation():
    source = Path("scripts/run_tests.ps1").read_text(encoding="utf-8")
    assert "& $FilePath @Arguments 2>&1 | Tee-Object -FilePath $LogPath" in source
    assert "run_tests_pytest.log" in source
    assert "run_tests_simulations.log" in source


def test_timeline_scaling_uses_tight_marker_focus_window():
    source = Path("app/timeline_scaling.py").read_text(encoding="utf-8")
    assert "def marker_centered_price_window" in source
    assert "v2.24 deliberately retains a tighter marker corridor" in source
    assert "Downsample after filtering" in Path("app/gui.py").read_text(encoding="utf-8")


def test_windows_lockfile_avoids_os_kill_zero_probe():
    source = Path("app/lockfile.py").read_text(encoding="utf-8")
    assert "def _pid_is_running_windows" in source
    assert "OpenProcess" in source
    assert "console control event" in source
    assert 'if os.name == "nt":' in source
