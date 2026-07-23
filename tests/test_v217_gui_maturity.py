from pathlib import Path

from app.timeline_scaling import parse_timeline_timestamp, preferred_timeline_timestamp, true_time_axis_positions

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v217_version_metadata_and_docs_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.1"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_v217_stop_dialog_offers_market_close_for_app_bought_unsold_position_without_tws_orders():
    assert "show_position_close_action" in GUI
    assert "SQLite shows {self.unsold_quantity:g} app-bought unsold share(s)" in GUI
    assert "show_position_close = bool(unsold_qty > 0)" in GUI
    assert "show_tws_order_actions=bool(open_orders)" in GUI
    assert "show_position_close_action=show_position_close" in GUI


def test_v217_recovery_market_close_button_is_enabled_only_with_unsold_quantity():
    assert "market_close_supported = open_qty > 0 and not no_recovery_action_needed" in GUI
    assert "can_market_close = market_close_supported and broker_refresh_current" in GUI
    assert 'can_market_close = permissions["can_market_close"]' in GUI
    assert "self.recovery_sell_market_btn.setEnabled(can_market_close)" in GUI
    assert "No app-bought unsold quantity is visible" in GUI


def test_v217_recovery_log_preserves_scroll_position_across_snapshot_updates():
    assert "def _set_recovery_details_text_preserve_scroll" in GUI
    assert "old_value = bar.value()" in GUI
    assert "ratio = old_value / float(old_max)" in GUI
    assert "target_value = int(round(ratio * new_max))" in GUI
    assert "QTimer.singleShot(0, lambda b=bar, v=target_value" in GUI
    assert "self._set_recovery_details_text_preserve_scroll" in GUI


def test_v217_recovery_does_not_mark_normal_strategy_wait_text_as_red_risk():
    assert "def _is_expected_strategy_wait_message" in GUI
    assert "waiting for a higher price" in GUI
    assert "no longer protects" in GUI
    assert "This is normal strategy wait/status text, not a recovery error" in GUI


def test_v217_summary_tab_now_prioritizes_graph_height_and_compact_details():
    assert "compact_timeline.setMinimumHeight(500)" in GUI
    assert "_multi_pair_key_value_table(summary_items, pairs_per_row=3)" in GUI
    assert "layout.addWidget(summary_table, 0)" in GUI


def test_v217_preferred_timeline_timestamp_uses_app_capture_aligned_time_before_ib_execution_time():
    capture_window = (
        parse_timeline_timestamp("2026-07-08T16:20:15+00:00"),
        parse_timeline_timestamp("2026-07-08T16:50:15+00:00"),
    )
    assert capture_window[0] is not None and capture_window[1] is not None
    chosen = preferred_timeline_timestamp(
        [
            "2026-07-08T16:35:14+00:00",  # app/cycle fill timestamp aligned with capture
            "2026-07-08 14:35:11+00:00",  # imported broker execution timestamp on different axis
        ],
        capture_window,  # type: ignore[arg-type]
        tolerance_seconds=3600,
    )
    assert chosen == parse_timeline_timestamp("2026-07-08T16:35:14+00:00")


def test_v217_true_time_axis_keeps_capture_path_and_actions_on_same_timescale_after_timestamp_choice():
    start = parse_timeline_timestamp("2026-07-08T16:20:15+00:00")
    buy = parse_timeline_timestamp("2026-07-08T16:35:14+00:00")
    sell = parse_timeline_timestamp("2026-07-08T16:48:53+00:00")
    end = parse_timeline_timestamp("2026-07-08T16:50:15+00:00")
    positions = true_time_axis_positions([
        [{"time": start, "price": 202.43}, {"time": buy, "price": 203.68}, {"time": sell, "price": 204.75}, {"time": end, "price": 204.84}],
        [{"label": "BUY", "time": buy, "price": 203.68}, {"label": "FINAL SELL", "time": sell, "price": 204.75}],
    ])
    assert round(positions[(1, 0)], 6) == round(positions[(0, 1)], 6)
    assert round(positions[(1, 1)], 6) == round(positions[(0, 2)], 6)


def test_v217_preferred_timeline_timestamp_suppresses_unmatched_broker_time_when_capture_window_exists():
    capture_window = (
        parse_timeline_timestamp("2026-07-08T16:20:15+00:00"),
        parse_timeline_timestamp("2026-07-08T16:50:15+00:00"),
    )
    chosen = preferred_timeline_timestamp(
        ["2026-07-08 14:35:11+00:00"],
        capture_window,  # type: ignore[arg-type]
        tolerance_seconds=1800,
    )
    assert chosen is None
