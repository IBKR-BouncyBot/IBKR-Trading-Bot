from pathlib import Path

from app.timeline_scaling import staged_axis_positions, true_time_axis_positions


def test_v210_markers_align_to_price_path_time_when_available():
    positions = staged_axis_positions([
        [
            {"time": 100.0, "price": 214.0},
            {"time": 110.0, "price": 215.0},
            {"time": 120.0, "price": 216.0},
        ],
        [
            {"label": "BUY", "time": 110.0, "position_hint": 0.34},
            {"label": "FINAL SELL", "time": 120.0, "position_hint": 0.90},
        ],
    ])

    assert round(positions[(1, 0)], 6) == round(positions[(0, 1)], 6)
    assert round(positions[(1, 1)], 6) == round(positions[(0, 2)], 6)


def test_v210_markers_align_to_price_path_price_when_time_is_missing():
    positions = staged_axis_positions([
        [
            {"time": 100.0, "price": 214.0},
            {"time": 110.0, "price": 215.0},
            {"time": 120.0, "price": 216.0},
        ],
        [
            {"label": "BUY", "price": 215.0, "position_hint": 0.34},
            {"label": "FINAL SELL", "price": 216.0, "position_hint": 0.90},
        ],
    ])

    assert round(positions[(1, 0)], 6) == round(positions[(0, 1)], 6)
    assert round(positions[(1, 1)], 6) == round(positions[(0, 2)], 6)


def test_v210_timeline_dialog_is_maximizable_scrollable_zoomable_and_crosshair_enabled():
    source = Path("app/gui.py").read_text(encoding="utf-8")

    assert "WindowMaximizeButtonHint" in source
    assert "setSizeGripEnabled(True)" in source
    assert "timeline_scroll = QScrollArea()" in source
    assert "Reset zoom" in source
    assert "def wheelEvent" in source
    assert "def mouseMoveEvent" in source
    assert "def _draw_hover_overlay" in source
    assert "Hover for crosshairs" in source
    assert "timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in source
    assert "self.text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in source
    assert "table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in source


def test_v211_timeline_uses_one_true_time_axis_for_all_timed_data():
    positions = true_time_axis_positions([
        [
            {"time": 100.0, "price": 214.0},
            {"time": 110.0, "price": 215.0},
            {"time": 120.0, "price": 216.0},
        ],
        [
            {"label": "BUY", "time": 115.0, "price": 215.5, "position_hint": 0.34},
            {"label": "FINAL SELL", "time": 120.0, "price": 216.0, "position_hint": 0.90},
        ],
        [{"label": "Transition", "time": 110.0}],
    ])

    assert positions[(0, 0)] < positions[(2, 0)] < positions[(1, 0)] < positions[(1, 1)]
    assert round(positions[(2, 0)], 6) == round(positions[(0, 1)], 6)
    assert round(positions[(1, 1)], 6) == round(positions[(0, 2)], 6)


def test_market_path_window_remains_authoritative_when_older_actions_exist():
    positions = true_time_axis_positions(
        [
            [
                {"time": 100.0, "price": 214.0},
                {"time": 110.0, "price": 215.0},
                {"time": 120.0, "price": 216.0},
            ],
            [
                {"label": "ANCHOR", "time": 10.0, "price": 218.0, "position_hint": 0.06},
                {"label": "DROP", "time": 110.0, "price": 215.0, "position_hint": 0.18},
                {"label": "FINAL SELL", "time": 120.0, "price": 216.0, "position_hint": 0.90},
            ],
        ],
        reference_window=(100.0, 120.0),
    )

    assert positions[(0, 0)] == 0.08
    assert round(positions[(1, 1)], 6) == round(positions[(0, 1)], 6)
    assert round(positions[(1, 2)], 6) == round(positions[(0, 2)], 6)
    assert positions[(1, 0)] == 0.08


def test_v210_live_command_bar_stays_parented_inside_live_tab_without_hide_show_bug():
    source = Path("app/gui.py").read_text(encoding="utf-8")

    assert "Windows layout bug" in source
    assert "_refresh_live_tab_layout" in source
    assert "outer.addWidget(self.command_bar, 0)" in source
    assert "self.shell_layout.addWidget(self.command_bar)" not in source



def test_v212_cycle_audit_draws_market_data_and_app_actions_as_separate_graphs():
    source = Path("app/gui.py").read_text(encoding="utf-8")

    assert "Market data graph - captured selected prices" in source
    assert "App actions graph - orders, fills, stages and guards" in source
    assert "Separate graphs: market data path and app actions share the same horizontal timestamp scale." in source
    assert "market_plot" in source
    assert "action_plot" in source
    assert "reference_window=self._path_time_window" in source
    assert "the plotted market-data window defines one shared horizontal timestamp scale" in source


def test_v212_stop_dialog_has_exit_only_path_when_no_strategy_is_running():
    source = Path("app/gui.py").read_text(encoding="utf-8")

    assert "self.exit_only_btn = QPushButton(\"Exit app\")" in source
    assert "def _choose_exit_only" in source
    assert "safe_to_exit=(not open_orders and not show_position_close and safe_no_running_strategy)" in source
    assert "No strategy cycle is running and no app-owned open TWS orders are visible" in source
