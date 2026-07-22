from pathlib import Path

from app.timeline_scaling import staged_axis_positions


def test_staged_axis_positions_keep_markers_in_cycle_order_away_from_left_axis():
    positions = staged_axis_positions([
        [{"price": 205, "order": idx} for idx in range(5)],
        [
            {"label": "ANCHOR", "position_hint": 0.05},
            {"label": "DROP", "position_hint": 0.18},
            {"label": "BUY", "position_hint": 0.34},
            {"label": "FINAL SELL", "position_hint": 0.90},
        ],
    ])

    assert 0.03 <= positions[(1, 0)] <= 0.07
    assert positions[(1, 0)] < positions[(1, 1)] < positions[(1, 2)] < positions[(1, 3)]
    assert 0.07 <= positions[(0, 0)] <= 0.09
    assert positions[(0, 4)] >= 0.94


def test_cycle_timeline_uses_true_timescale_and_clamped_marker_labels():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "true_time_axis_positions" in source
    assert "Separate graphs: market data path and app actions share the same horizontal timestamp scale." in source
    assert "Market data graph - captured selected prices" in source
    assert "def _draw_marker_label" in source
    assert "label_x = max(plot.left() + 6" in source
    assert "def _draw_hover_overlay" in source
    assert "Ctrl+mouse wheel zooms" in source


def test_successful_imported_cycles_do_not_paint_generic_error_text_as_risk_blocks():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    helper = source[source.index("def _is_audit_risk_block_event"):source.index("def _compact_text")]
    block = source[source.index("def _build_risk_blocks"):source.index("def _all_prices")]
    assert "Successful imported cycles can contain transient diagnostic text" in helper
    assert "_is_audit_risk_block_event(event)" in block
    assert "error stop" not in helper
    assert "blocked by" in helper


def test_bottom_command_bar_is_live_strategy_only():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "self.tabs.currentChanged.connect(self._on_tab_changed)" in source
    assert "outer.addWidget(self.command_bar, 0)" in source
    assert "parented inside the Live strategy" in source
    assert "return below the visible window" in source
