from pathlib import Path

from app.models import ConnectionSettings, StrategySettings, suggested_hard_risk_defaults
from app.storage import BotStorage

GUI = Path("app/gui.py").read_text(encoding="utf-8")
BUILD = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")


def test_v126_defaults_market_data_and_min_trade_price():
    assert ConnectionSettings().market_data_type == 0
    assert StrategySettings().min_trade_price == 0.0
    assert suggested_hard_risk_defaults(10_000)["min_trade_price"] == 0.0


def test_v126_history_clickthrough_and_example_row_are_ui_only():
    assert "CycleAuditDialog" in GUI
    assert "cellClicked.connect(self._history_row_clicked)" in GUI
    assert "__example" in GUI
    assert "It is never stored in SQLite" in GUI


def test_v127_recovery_tab_is_restored_as_rightmost_main_tab():
    assert "self.recovery_tabs = QTabWidget()" not in GUI
    assert "self.recovery_tabs.setTabPosition(QTabWidget.East)" not in GUI
    assert 'self.recovery_tabs.addTab(self.recovery_tab, "Reconciliation")' not in GUI
    assert 'self.tabs.addTab(self.recovery_tab, "Reconciliation")' in GUI


def test_v126_question_mark_badges_removed_from_visible_ui():
    assert 'label = QLabel("")' in GUI
    assert 'label.setVisible(False)' in GUI
    assert 'label = QLabel("?")' not in GUI


def test_v126_flowchart_uses_dynamic_width_to_avoid_clipped_right_side():
    assert "canvas_w = max(float(self.MIN_CANVAS_WIDTH), float(self.width()))" in GUI
    assert "self.scroll.setWidgetResizable(False)" in GUI
    assert "self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in GUI
    assert "self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in GUI
    assert "details_box = QRectF" in GUI


def test_v126_price_monitor_uses_shared_table_height_sizing_and_scrollbars():
    assert "def _fit_table_height_to_rows" in GUI
    assert "table.resizeRowsToContents()" in GUI
    assert "table.setVerticalScrollBarPolicy(vertical_scroll)" in GUI
    assert "QSizePolicy.Expanding, QSizePolicy.Fixed" in GUI


def test_v28_build_script_keeps_v22_checked_path_but_packages_reliably():
    assert "[switch]$RunTests" in BUILD
    assert "[switch]$SkipTests" not in BUILD
    assert "Skipping full tests for faster, more reliable packaging." in BUILD
    assert r"& $python scripts\run_all_simulations.py" in BUILD
    assert "& $python scripts\run_simulated_strategy.py" not in BUILD
    assert "Run CSV simulation fixtures" in BUILD
    assert r".venv\Scripts\pyinstaller.exe" in BUILD
    assert "--collect-submodules" not in BUILD
    assert "IBKRTradingBot.exe" in BUILD
    assert "PyInstaller completed but $exePath was not created" in BUILD


def test_storage_cycle_audit_details_returns_orders_executions_and_events(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strategy = StrategySettings(ticker="AAPL")
    from app.models import CycleState
    cycle = CycleState.new(strategy, cycle_number=1, account="DU1", last_price=100.0, reinvested_profit=0.0)
    storage.upsert_cycle(cycle)
    storage.add_order(cycle=cycle, action="BUY", order_type="TRAIL", order_id=1, perm_id=2, order_ref="IBKRBOT|AAPL|BUY", quantity=10, trailing_percent=1.0, initial_stop_price=101.0, status="Submitted")
    storage.add_execution(cycle=cycle, ticker="AAPL", side="BOT", shares=10, price=101.0, avg_price=101.0, order_ref="IBKRBOT|AAPL|BUY", execution_id="E1")
    storage.add_event("INFO", "example event", ticker="AAPL", cycle_id=cycle.id)
    storage.add_decision_event(event_type="TEST", message="decision event", cycle=cycle, stage_before="A", stage_after="B")
    details = storage.cycle_audit_details(cycle.id)
    assert details["cycle"]["id"] == cycle.id
    assert details["orders"][0]["order_ref"] == "IBKRBOT|AAPL|BUY"
    assert details["executions"][0]["execution_id"] == "E1"
    assert details["events"][0]["message"] == "example event"
    assert details["decision_events"][0]["event_type"] == "TEST"
