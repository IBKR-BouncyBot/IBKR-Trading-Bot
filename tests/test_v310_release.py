"""v3.1.0 close-before-RTH feature regressions under the current release."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD_SCRIPT = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
CURRENT_RELEASE_NOTE = ROOT / "docs" / "V3_1_1_IBKR_ORDER_VALIDATION.md"
V310_RELEASE_NOTE = ROOT / "docs" / "legacy" / "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md"


def test_current_release_metadata_is_consistent_and_v310_note_is_archived() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in GUI
    assert "This is synthetic v3.1.1 paper-trading example data." in GUI
    assert README.startswith("# BouncyBot - an IBKR Portable Trading Bot \n")
    assert "**Current release: v3.1.1**" in README
    assert 'version = "3.1.1"' in PYPROJECT
    assert '$version = "3.1.1"' in BUILD_SCRIPT
    assert "## v3.1.1" in CHANGELOG
    assert "current v3.1.1 behavior" in DOCS_INDEX
    assert CURRENT_RELEASE_NOTE.is_file()
    assert V310_RELEASE_NOTE.is_file()
    assert not (ROOT / "docs" / "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md").exists()


def test_v310_live_strategy_dashboard_scrolls_horizontally_only_when_needed() -> None:
    dashboard_start = GUI.index("def _build_dashboard")
    dashboard_block = GUI[dashboard_start : GUI.index("def _connection_group", dashboard_start)]

    assert "scroll.setWidgetResizable(True)" in dashboard_block
    assert "scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in dashboard_block
    assert "scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in dashboard_block
    assert "scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)" not in dashboard_block


def test_v310_gui_exposes_default_off_stage4_liquidation_controls_and_tooltips() -> None:
    assert 'QCheckBox("Cancel SELL trail and liquidate before close")' in GUI
    assert "self.cancel_sell_and_liquidate_before_close_check.setChecked(False)" in GUI
    assert "self.liquidate_before_close_spin.setRange(1, 240)" in GUI
    assert "self.liquidate_before_close_spin.setValue(5)" in GUI
    assert 'self.liquidate_before_close_spin.setSuffix(" min")' in GUI
    assert "Default OFF. Stage 4 only." in GUI
    assert "The market fill can be below the trailing stop and can realize a loss." in GUI
    assert "No outside-RTH replacement is submitted." in GUI


def test_v310_controller_keeps_cancel_confirm_replace_and_rth_only_market_boundaries() -> None:
    assert "_cancel_sell_and_liquidate_before_close_if_needed" in CONTROLLER
    assert "_is_stage4_trailing_sell_ref" in CONTROLLER
    assert '"order_type": "MKT"' in CONTROLLER
    assert '"tif": "DAY"' in CONTROLLER
    assert '"outside_rth": False' in CONTROLLER
    assert "outside_rth=False" in CONTROLLER
    assert "No second SELL was submitted" in CONTROLLER
    assert "No outside-RTH replacement order was submitted." in CONTROLLER


def test_v310_archived_documentation_preserves_scope_and_failure_behavior() -> None:
    release_note = V310_RELEASE_NOTE.read_text(encoding="utf-8")
    assert "Stage 4 (`SELL_TRAIL_ACTIVE`)" in release_note
    assert "waits until IBKR reports a terminal order state" in release_note
    assert "only the remaining app-owned quantity" in release_note
    assert "The market order prioritizes liquidation rather than price." in release_note
    assert "Stage-2 BUY trailing-stop behavior" in release_note
    assert "docs/legacy/V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md" in README
    assert "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md" in LEGACY_INDEX
