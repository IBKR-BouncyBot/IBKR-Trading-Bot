from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v218_version_and_package_metadata_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.0" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.0"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_recovery_safe_local_stop_is_not_classified_as_red_manual_review():
    assert "def _is_expected_operator_stop_message" in GUI
    assert "Strategy was stopped locally by the operator before any app-owned broker order or position needed recovery." in GUI
    assert "No recovery action is needed. You can start a new cycle when the normal guards allow trading." in GUI
    assert "No recovery action is required. Start a new strategy only when intended." in GUI


def test_recovery_stopped_local_order_or_position_remainders_are_caution_not_risk():
    stopped_block = GUI[GUI.index("elif stage == Stage.STOPPED.value:") : GUI.index("elif cycle.get(\"error_message\"):")]
    assert "This is a caution state, not an automatic error." in stopped_block
    assert "action_state = \"waiting\"" in stopped_block
    assert "action_state = \"risk\"" in stopped_block  # still reserved for true recovery flags/errors
    assert "The local cycle is stopped, and TWS still reports app-owned open order(s)." in stopped_block
    assert "The local cycle is stopped, and SQLite still shows {open_qty:g} app-bought unsold share(s)." in stopped_block


def test_recovery_completed_and_stopped_safe_states_are_success_not_inactive_or_risk():
    assert "elif stage == Stage.STOPPED.value:" in GUI
    assert "elif stage == Stage.CYCLE_COMPLETE.value:" in GUI
    assert "Stopped cycle is locally safe: no app-owned order or unsold app position is visible." in GUI
    assert "Completed cycle is locally safe: no app-owned order or unsold app position is visible." in GUI
    assert 'action_state = "success"' in GUI


def test_cycle_audit_timeline_default_size_fits_before_zoom_scrollbars():
    assert "self.setMinimumHeight(300 if self.compact else 320)" in GUI
    assert "self._base_canvas_width = 720 if self.compact else 940" in GUI
    assert "timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "timeline_scroll.setMinimumHeight(500)" in GUI
    assert "timeline_scroll.setMaximumHeight(16777215)" in GUI


def test_market_capture_summary_table_shows_all_rows_without_vertical_scrollbar():
    assert "def _fit_table_height_to_all_rows" in GUI
    assert "table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)" in GUI
    assert "_fit_table_height_to_all_rows(summary_table, min_height=420, max_height=760)" in GUI
    assert "show every field without" in GUI
