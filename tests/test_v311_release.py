"""v3.1.1 release metadata, documentation, and broker-boundary regressions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = (ROOT / "app" / "ib_adapter.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
RELEASE_NOTE = ROOT / "docs" / "V3_1_1_IBKR_ORDER_VALIDATION.md"
ARCHIVED_V310_NOTE = ROOT / "docs" / "legacy" / "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md"


def test_v311_release_metadata_is_consistent() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in GUI
    assert "This is synthetic v3.1.1 paper-trading example data." in GUI
    assert "**Current release: v3.1.1**" in README
    assert 'version = "3.1.1"' in PYPROJECT
    assert '$version = "3.1.1"' in BUILD
    assert "## v3.1.1" in CHANGELOG
    assert "current v3.1.1 behavior" in DOCS_INDEX


def test_v311_current_and_archived_release_notes_are_in_the_correct_locations() -> None:
    assert RELEASE_NOTE.is_file()
    assert ARCHIVED_V310_NOTE.is_file()
    assert not (ROOT / "docs" / "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md").exists()
    assert "V3_1_1_IBKR_ORDER_VALIDATION.md" in README
    assert "V3_1_1_IBKR_ORDER_VALIDATION.md" in DOCS_INDEX
    assert "V3_1_0_CLOSE_BEFORE_RTH_LIQUIDATION.md" in LEGACY_INDEX


def test_v311_adapter_source_keeps_market_rule_and_strict_what_if_contracts() -> None:
    assert "validExchanges" in ADAPTER
    assert "marketRuleIds" in ADAPTER
    assert "reqMarketRule" in ADAPTER
    assert 'source="market_rule"' in ADAPTER
    assert 'transmit=True' in ADAPTER
    assert 'whatIf=True' in ADAPTER
    assert 'getattr(self.ib, "whatIfOrder", None)' in ADAPTER
    assert '"validationerror"' in ADAPTER
    assert "No usable margin or equity impact" in ADAPTER


def test_v311_adapter_and_controller_keep_order_error_audit_and_circuit_breaker() -> None:
    assert '"event_type": "ORDER_ERROR"' in ADAPTER
    assert "advanced_reject_json" in ADAPTER
    assert "_ORDER_ERROR_CACHE_TTL_SECONDS = 30.0" in ADAPTER
    assert "_ORDER_ERROR_CACHE_MAX_ITEMS = 256" in ADAPTER
    assert 'event_type="BROKER_ORDER_ERROR"' in CONTROLLER
    assert "no replacement or automatic fresh-cycle retry will be sent" in CONTROLLER
    assert 'status in {"Inactive", "Rejected"}' in CONTROLLER


def test_v311_release_note_documents_compatibility_and_live_validation_boundary() -> None:
    text = RELEASE_NOTE.read_text(encoding="utf-8")
    assert "There is no SQLite schema change in v3.1.1." in text
    assert "Existing v3.1.0 and v3.0.19 databases remain forward-compatible." in text
    assert "live TWS or IB Gateway paper-account exercise" in text
    assert "Normal confirmed cancellations remain separate." in text
