from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v219_version_package_metadata_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.0" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.2.0"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_idle_and_stopped_graph_only_show_current_price_level():
    graph_block = GUI[GUI.index("class StrategyGraphWidget") : GUI.index("class FlowchartPanel")]
    assert 'if not stage or stage in {Stage.STOPPED.value, Stage.IDLE.value}:' in graph_block
    assert 'add("Current price", current_price, "#111827", "last usable app price")' in graph_block
    assert "avoid\n            # drawing projected levels from stale inputs or previous cycles" in graph_block
    assert "Projected anchor" not in graph_block
    assert "Projected BUY stop" not in graph_block


def test_recovery_action_buttons_disable_when_snapshot_is_safe():
    assert "def _recovery_action_permissions(" in GUI
    assert "no_recovery_action_needed = (" in GUI
    assert 'action_state in {"success", "inactive", "active"}' in GUI
    assert "self.recovery_resume_btn.setEnabled(can_resume)" in GUI
    assert "self.recovery_stop_cycle_btn.setEnabled(can_stop_cycle)" in GUI
    assert "self.recovery_mark_manual_btn.setEnabled(can_mark_manual)" in GUI
    assert "No recovery action is available because the current snapshot is safe." in GUI
    assert "Refresh from IBKR/TWS remains available" in GUI


def test_recovery_advanced_buttons_use_current_refresh_gating_without_duplicate_cancel():
    assert "self.recovery_cancel_orders_btn" not in GUI
    assert "self.recovery_leave_orders_btn.setEnabled(can_leave_orders)" in GUI
    assert 'elif permissions["cancel_supported"] and not refresh_current:' in GUI
    assert "self.recovery_stop_after_btn" not in GUI
    assert "self.recovery_refresh_btn" not in GUI


def test_general_tables_use_content_based_column_sizing_without_forced_stretch():
    helper_start = GUI.index("def _auto_size_table_columns(")
    helper_end = GUI.index("def _resize_table_columns_for_available_width", helper_start)
    helper = GUI[helper_start:helper_end]

    assert "header.setStretchLastSection(False)" in helper
    assert "header.setSectionResizeMode(col, QHeaderView.Interactive)" in helper
    assert "table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in helper
    assert "QHeaderView.Stretch" not in helper
    assert "_auto_size_table_columns(self.recovery_compare_table" in GUI
