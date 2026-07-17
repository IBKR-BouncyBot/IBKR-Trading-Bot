"""Regression coverage for the v3.0.12 reliability and audit-layout fixes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.models import ConnectionSettings, Stage, StrategySettings
from tests.support.qt_stubs import SignalStub, imported_gui_with_stubs


class _ControllerStub:
    def __init__(self) -> None:
        self.connection = ConnectionSettings()
        self.strategy = StrategySettings(ticker="AAPL")
        self.signals = SimpleNamespace(
            snapshot_updated=SignalStub(),
            history_updated=SignalStub(),
            connection_changed=SignalStub(),
            ticker_search_updated=SignalStub(),
        )

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            if name == "app_owned_unsold_position":
                return 0.0
            if name == "get_cycle_audit_details":
                return {}
            return None

        return call


def _blocked_running_snapshot() -> dict[str, Any]:
    strategy = StrategySettings(ticker="AAPL")
    connection = ConnectionSettings()
    return {
        "connected": True,
        "status": "Connected",
        "connection": asdict(connection),
        "strategy": asdict(strategy),
        "active_cycle": {
            "id": "cycle-1",
            "ticker": "AAPL",
            "stage": Stage.WAIT_INITIAL_DROP.value,
            "last_price": 100.0,
            "error_message": "BUY blocked: not enough ATR data is available yet.",
        },
        "price_snapshot": {
            "price": 100.0,
            "fields": {"last": 100.0},
            "api_data_state": "recent",
        },
        "broker_connectivity": {
            "local_connected": True,
            "upstream_connected": True,
            "state": "connected",
            "message": "ready",
        },
        "upstream_recovery_pending": False,
        "startup_resume_required": False,
    }


def test_input_lock_refresh_does_not_reenable_guard_blocked_start_button():
    with imported_gui_with_stubs(Path.cwd()) as gui:
        window = gui.MainWindow(_ControllerStub())
        snapshot = _blocked_running_snapshot()

        window._update_command_bar_states(snapshot)
        assert window.start_btn.isEnabled() is False
        assert window.command_steps["start"].state.text() == "BLOCKED"

        window._update_input_locks(Stage.WAIT_INITIAL_DROP.value)
        assert window.start_btn.isEnabled() is False
        assert window.command_steps["start"].state.text() == "BLOCKED"


def test_input_lock_method_no_longer_owns_workflow_button_enablement():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("    def _update_input_locks")
    end = source.index("    def ", start + 8)
    method = source[start:end]

    assert "self.start_btn.setEnabled(True)" not in method
    assert "self.stop_btn.setEnabled(True)" not in method
    assert "Workflow button state is owned exclusively" in method


def test_summary_tab_uses_six_column_no_scroll_table_and_expanding_graph():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("    def _summary_tab")
    end = source.index("    @staticmethod\n    def _key_value_table", start)
    section = source[start:end]

    assert "compact_timeline.setMinimumHeight(500)" in section
    assert "QSizePolicy.Expanding, QSizePolicy.Expanding" in section
    assert "_multi_pair_key_value_table(summary_items, pairs_per_row=3)" in section
    assert "horizontal_scroll=Qt.ScrollBarAlwaysOff" in section
    assert "vertical_scroll=Qt.ScrollBarAlwaysOff" in section
    assert "QHeaderView.ResizeToContents" in section
    assert "QHeaderView.Stretch" in section
    assert "return tab" in section
    assert "return cls._scrollable_tab(tab)" not in section


def test_timeline_tab_reserves_only_four_table_rows_and_expands_graph():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("    def _timeline_tab")
    end = source.index("    @staticmethod\n    def _scrollable_tab", start)
    section = source[start:end]

    assert "timeline.setMinimumHeight(500)" in section
    assert "timeline_scroll.setMinimumHeight(500)" in section
    assert "timeline_scroll.setMaximumHeight(16777215)" in section
    assert "layout.addWidget(timeline_scroll, 1)" in section
    assert section.count("max_visible_rows=4, expand_when_overflow=False") == 2
    assert "layout.addLayout(split, 0)" in section
    assert "timeline_scroll.setMaximumHeight(390)" not in section


def test_fixed_visible_row_mode_keeps_overflow_tables_compact():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("def _fit_table_height_to_rows")
    end = source.index("def _fit_table_height_to_all_rows", start)
    helper = source[start:end]

    assert "expand_when_overflow: bool = True" in helper
    assert "elif not expand_when_overflow:" in helper
    assert "table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in helper
    assert "table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)" in helper


def test_windows_release_zip_uses_market_replay_lab_naming_pattern():
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")

    assert '$appName = "IBKRTradingBot"' in build
    assert '$releaseName = "${appName}_${version}_Windows"' in build
    assert '$releaseZip = Join-Path $releaseDirectory "$releaseName.zip"' in build
    assert "Compress-Archive -Path $releaseRoot -DestinationPath $releaseZip" in build
