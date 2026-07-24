"""v3.1.2 feature regressions under the current release."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
STORAGE = (ROOT / "app" / "storage.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
CURRENT_RELEASE_NOTE = ROOT / "docs" / "V3_2_0_EUR_SMART_AND_RECONNECT.md"
RELEASE_NOTE = ROOT / "docs" / "legacy" / "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md"
ARCHIVED_V311_NOTE = ROOT / "docs" / "legacy" / "V3_1_1_IBKR_ORDER_VALIDATION.md"


def test_v312_release_metadata_is_consistent() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.0" in GUI
    assert "This is synthetic v3.2.0 paper-trading example data." in GUI
    assert "**Current release: v3.2.0**" in README
    assert 'version = "3.2.0"' in PYPROJECT
    assert '$version = "3.2.0"' in BUILD
    assert "## v3.2.0" in CHANGELOG
    assert "## v3.1.2" in CHANGELOG
    assert "current v3.2.0 behavior" in DOCS_INDEX


def test_v312_current_and_archived_release_notes_are_in_the_correct_locations() -> None:
    assert CURRENT_RELEASE_NOTE.is_file()
    assert RELEASE_NOTE.is_file()
    assert ARCHIVED_V311_NOTE.is_file()
    assert not (ROOT / "docs" / "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md").exists()
    assert not (ROOT / "docs" / "V3_1_1_IBKR_ORDER_VALIDATION.md").exists()
    assert "V3_2_0_EUR_SMART_AND_RECONNECT.md" in README
    assert "V3_2_0_EUR_SMART_AND_RECONNECT.md" in DOCS_INDEX
    assert "V3_1_2_FILL_RECONCILIATION_AND_STAGE3_CLOSE.md" in LEGACY_INDEX
    assert "V3_1_1_IBKR_ORDER_VALIDATION.md" in LEGACY_INDEX


def test_v312_gui_documents_stage3_and_stage4_preclose_behavior() -> None:
    assert "Default OFF. In Stage 3" in GUI
    assert "commissions are ignored for that comparison" in GUI
    assert "In Stage 4, the app cancels the final SELL trailing-stop" in GUI
    assert "A market fill can be below the checked price or trailing stop" in GUI
    assert "Stage 3 requires the selected current price to be strictly above the average BUY price" in GUI


def test_v312_source_keeps_terminal_buy_and_exact_order_ownership_contracts() -> None:
    assert "awaiting_terminal_buy" in CONTROLLER
    assert "buy_remainder_cancel_requested" in CONTROLLER
    assert "get_cycle_for_order_ref" in CONTROLLER
    assert "known_order_refs" in CONTROLLER
    assert "return self.storage.get_latest_active_cycle()" not in CONTROLLER
    assert "synthetic_cumulative_placeholder" in STORAGE
    assert "buy_remainder_cancel_requested" in STORAGE


def test_v312_release_note_documents_safety_and_compatibility() -> None:
    text = RELEASE_NOTE.read_text(encoding="utf-8")
    assert "enters Stage 3 only when IBKR reports the original order terminal" in text
    assert "complete `OrderRef`" in text
    assert "Commissions are intentionally ignored" in text
    assert "does not guarantee a profitable fill" in text
    assert "buy_remainder_cancel_requested INTEGER NOT NULL DEFAULT 0" in text
    assert "Existing v3.1.1, v3.1.0, and v3.0.19 databases remain forward-compatible" in text
    assert "paper-account partial BUY/cancel race" in text
