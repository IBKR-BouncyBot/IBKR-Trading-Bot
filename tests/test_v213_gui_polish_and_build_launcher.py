from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
ROOT_BUILD = Path("build_windows.bat").read_text(encoding="utf-8")
SCRIPT_BUILD = Path("scripts/build_windows.bat").read_text(encoding="utf-8")


def test_v213_root_windows_build_launcher_exists_and_calls_script():
    assert Path("build_windows.bat").exists()
    assert r"scripts\build_windows.ps1" in ROOT_BUILD
    assert "build_windows.ps1" in SCRIPT_BUILD


def test_v213_table_polish_scrollbars_and_fit_helpers_are_used():
    assert "def _polish_table_widget" in GUI
    assert "def _fit_table_height_to_rows" in GUI
    assert "table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in GUI
    assert "table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "_resize_table_columns_for_available_width" in GUI
    assert "self.recovery_details.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in GUI
    assert "self.event_log.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in GUI


def test_v213_flowchart_canvas_resyncs_on_resize_and_uses_visible_scrollbar():
    assert "def _sync_flowchart_canvas" in GUI
    assert "def resizeEvent(self, event)" in GUI
    assert "self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in GUI
    assert "self.flowchart.setMinimumSize(target_width, target_height)" in GUI


def test_v213_cycle_audit_tabs_and_market_capture_use_available_space():
    assert "tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)" in GUI
    assert "_fit_table_height_to_all_rows(summary_table" in GUI
    assert "preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)" in GUI
    assert "timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
