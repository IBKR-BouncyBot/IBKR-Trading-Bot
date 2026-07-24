from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def _recovery_block() -> str:
    return GUI[GUI.index("def _build_recovery") : GUI.index("def _recovery_export_bundle_clicked")]


def test_v3014_version_metadata_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.0" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.2.0"' in PYPROJECT
    assert Path("docs/legacy/V3_0_14_RECONCILIATION_HISTORY_SORTING.md").exists()


def test_advanced_reconciliation_row_contains_only_unique_actions():
    block = _recovery_block()
    assert "self.recovery_cancel_orders_btn" not in block
    assert 'self.recovery_sell_market_btn = QPushButton("Sell app-bought unsold position")' in block
    assert 'self.recovery_leave_orders_btn = QPushButton("Leave orders working")' in block
    assert "self.recovery_stop_after_btn" not in block
    assert "self.recovery_refresh_btn" not in block
    assert block.count('QPushButton("Stop after current cycle")') == 1
    assert block.count('QPushButton("Refresh from IBKR/TWS")') == 1
    assert block.count('QPushButton("Cancel visible app-owned orders")') == 1


def test_history_cycle_column_uses_numeric_display_role_for_sorting():
    assert 'if key == "cycle_number":' in GUI
    assert 'item.setData(Qt.DisplayRole, int(raw_value))' in GUI
    assert 'item.setData(Qt.UserRole, r)' in GUI
    assert "Qt sorts cycle 2 before cycle 10" in GUI
