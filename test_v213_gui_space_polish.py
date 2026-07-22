from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
ROOT_BUILD = Path("build_windows.bat").read_text(encoding="utf-8")


def test_v213_visible_scrollbars_are_not_globally_hidden():
    assert "QScrollBar:vertical" in GUI
    assert "width: 14px" in GUI
    assert "QScrollBar:horizontal" in GUI
    assert "height: 14px" in GUI
    assert "width: 0px;\n                background: transparent;\n                border: none;\n                margin: 0px;" not in GUI


def test_v213_tables_use_shared_space_polish_helpers():
    assert "def _polish_table_widget" in GUI
    assert "def _fit_table_height_to_rows" in GUI
    assert "def _cap_table_columns_for_horizontal_scroll" in GUI
    assert "_cap_table_columns_for_horizontal_scroll(self.history_table" in GUI
    assert "_polish_table_widget(self.recovery_compare_table" in GUI
    assert "_polish_table_widget(table, stretch_last=True" in GUI


def test_v213_zoomable_timeline_scrollbars_are_visible():
    assert "timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "timeline_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)" in GUI


def test_v213_root_build_windows_bat_delegates_to_scripts():
    assert 'powershell -ExecutionPolicy Bypass -File "%~dp0scripts\\build_windows.ps1" %*' in ROOT_BUILD
