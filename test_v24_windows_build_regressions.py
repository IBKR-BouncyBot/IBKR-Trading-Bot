from pathlib import Path


def test_headless_qt_stub_reloads_controller_before_import():
    source = Path("tests/test_controller_headless.py").read_text(encoding="utf-8")
    assert 'monkeypatch.delitem(sys.modules, "app.controller", raising=False)' in source
    assert "qtcore.QByteArray = bytes" in source


def test_static_gui_source_reads_are_utf8_explicit():
    source = Path("tests/test_gui_static_no_wheel.py").read_text(encoding="utf-8")
    assert 'read_text(encoding="utf-8")' in source
    assert "read_text()" not in source


def test_controller_uses_object_signals_for_python_container_payloads():
    source = Path("app/controller.py").read_text(encoding="utf-8")
    assert "snapshot_updated = Signal(object)" in source
    assert "history_updated = Signal(object)" in source
    assert "contract_search_updated = Signal(object)" in source
    assert "ticker_search_updated = Signal(object)" in source
    assert "snapshot_updated = Signal(dict)" not in source


def test_windows_build_script_reverted_to_v22_optional_test_gate():
    ps1 = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$RunTests" in ps1
    assert "[switch]$CleanVenv" in ps1
    assert "[switch]$SkipTests" not in ps1
    assert "Skipping full tests for faster, more reliable packaging." in ps1
    assert 'Invoke-Checked "Run pytest"' in ps1
    assert 'Invoke-Checked "Run CSV simulation fixtures"' in ps1
    assert "build_pytest.log" not in ps1
    assert '$env:QT_QPA_PLATFORM = "offscreen"' not in ps1
    assert '$env:PYTHONUNBUFFERED = "1"' not in ps1


def test_build_windows_bat_matches_v22_legacy_launcher():
    bat = Path("scripts/build_windows.bat").read_text(encoding="utf-8")
    assert 'powershell -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*' in bat
    assert "set IBKR_BOT_NO_PAUSE=1" in bat
    assert "pause" in bat.lower()
    assert "endlocal & exit /b %BUILD_EXIT_CODE%" in bat
