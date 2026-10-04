from __future__ import annotations

from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v220_version_metadata_updated():
    assert "BouncyBot - IBKR Portable Trading Bot v5.6.0" in GUI
    assert "# BouncyBot - an IBKR Trading Bot" in README
    assert 'version = "5.6.0"' in PYPROJECT


def test_rth_status_is_human_readable_with_hours_and_countdown():
    assert "def _rth_window_from_status" in GUI
    assert "liquid_hours" in GUI
    assert "closes in" in GUI
    assert "opens in" in GUI
    assert "Regular hours {hours_text}" in GUI
    assert "RTH open - closes in" in GUI


def test_live_strategy_graph_is_inside_price_panel_above_summary_cards():
    panel = GUI[GUI.index("class PricePanel"):GUI.index("class StopDialog")]
    progress_pos = panel.index("root.addWidget(self.progress_bar)")
    graph_pos = panel.index("root.addWidget(self.strategy_graph)")
    cards_pos = panel.index("root.addLayout(summary_grid)")
    assert progress_pos < graph_pos < cards_pos
    dashboard_pos = GUI.index("def _build_dashboard")
    configuration_pos = GUI.index("root.addLayout(top)", dashboard_pos)
    price_pos = GUI.index("self.price_panel = PricePanel()", dashboard_pos)
    price_insert_pos = GUI.index("root.addWidget(self.price_panel)", dashboard_pos)
    mid_pos = GUI.index("mid = QHBoxLayout()", price_pos)
    market_group_pos = GUI.index("def _market_state_group", mid_pos)
    assert price_pos < mid_pos < market_group_pos
    assert configuration_pos < price_insert_pos < mid_pos
    assert "self.strategy_graph = self.price_panel.strategy_graph" in GUI
    assert 'QGroupBox("Market and strategy graph")' not in GUI
    market_body = GUI[market_group_pos:GUI.index("def _order_state_group", market_group_pos)]
    assert "StrategyGraphWidget()" not in market_body


def test_stop_dialog_has_exit_resume_later_path_without_stop_action():
    assert "show_resume_later_exit_action" in GUI
    assert "Exit app and resume/recover later" in GUI
    assert "does not cancel TWS orders, does not sell shares" in GUI
    assert "click 4. Start strategy to resume monitoring/recovery" in GUI
    assert "self.exit_resume_later_btn.clicked.connect(self._choose_exit_only)" in GUI
