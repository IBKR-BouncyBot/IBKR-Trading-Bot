"""v3.1.0 release metadata and close-before-RTH GUI/documentation regressions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD_SCRIPT = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
RELEASE_NOTE_PATH = ROOT / "docs" / "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md"


def test_v310_release_metadata_is_consistent_and_user_readme_heading_is_preserved() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.0" in GUI
    assert "This is synthetic v3.1.0 paper-trading example data." in GUI
    assert README.startswith("# BouncyBot - an IBKR Portable Trading Bot \n")
    assert "**Current release: v3.1.0**" in README
    assert 'version = "3.1.0"' in PYPROJECT
    assert '$version = "3.1.0"' in BUILD_SCRIPT
    assert "## v3.1.0" in CHANGELOG
    assert "current v3.1.0 behavior" in DOCS_INDEX
    assert RELEASE_NOTE_PATH.is_file()


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
    assert 'outside_rth=False' in CONTROLLER
    assert "No second SELL was submitted" in CONTROLLER
    assert "No outside-RTH replacement order was submitted." in CONTROLLER


def test_v310_current_documentation_describes_scope_and_failure_behavior() -> None:
    release_note = RELEASE_NOTE_PATH.read_text(encoding="utf-8")
    assert "Stage 4 (`SELL_TRAIL_ACTIVE`)" in release_note
    assert "waits until IBKR reports a terminal order state" in release_note
    assert "only the remaining app-owned quantity" in release_note
    assert "The market order prioritizes liquidation rather than price." in release_note
    assert "Stage-2 BUY trailing-stop behavior" in release_note
    assert "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md" in README
    assert "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md" in DOCS_INDEX
