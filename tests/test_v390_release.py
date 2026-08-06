"""v3.9.0 release metadata, compatibility, and packaging regressions."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
CURRENT_NOTE = ROOT / "docs" / "V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md"
ARCHIVED_V380_NOTE = ROOT / "docs" / "legacy" / "V3_8_0_BUY_PARTIAL_FILL_GRACE.md"
ARCHIVED_V380_REPORT = ROOT / "docs" / "legacy" / "V3_8_0_IMPLEMENTATION_TEST_REPORT.txt"
CURRENT_REPORT = ROOT / "IMPLEMENTATION_TEST_REPORT.txt"


def test_v390_release_metadata_is_consistent() -> None:
    assert 'APP_VERSION = "3.9.0"' in GUI
    assert "BouncyBot - IBKR Portable Trading Bot v3.9.0" in GUI
    assert "This is synthetic v3.9.0 paper-trading example data." in GUI
    assert "**Current release: v3.9.0**" in README
    assert 'version = "3.9.0"' in PYPROJECT
    assert '$version = "3.9.0"' in BUILD
    assert "## v3.9.0" in CHANGELOG
    assert "current repository version, v3.9.0" in SECURITY
    assert "current v3.9.0 behavior" in DOCS_INDEX


def test_v390_release_note_is_current_and_v380_material_is_archived() -> None:
    assert CURRENT_NOTE.is_file()
    assert ARCHIVED_V380_NOTE.is_file()
    assert ARCHIVED_V380_REPORT.is_file()
    assert not (ROOT / "docs" / ARCHIVED_V380_NOTE.name).exists()
    assert CURRENT_REPORT.is_file()
    assert "BouncyBot v3.9.0" in CURRENT_REPORT.read_text(encoding="utf-8")
    assert CURRENT_NOTE.name in README
    assert CURRENT_NOTE.name in DOCS_INDEX
    assert ARCHIVED_V380_NOTE.name in README
    assert ARCHIVED_V380_NOTE.name in LEGACY_INDEX
    assert ARCHIVED_V380_REPORT.name in LEGACY_INDEX


def test_v390_release_note_documents_scope_and_compatibility() -> None:
    text = CURRENT_NOTE.read_text(encoding="utf-8")
    assert "705 `Final SELL trigger not armed` warnings" in text
    assert "one persistence summary after the configured delay" in text
    assert "The state itself remains evaluated on every normal controller cadence" in text
    assert "Stable reason codes" in text or "stable reason codes" in text
    assert "Existing v3.8.0 databases" in text
    assert "No new SQLite object or persisted setting is introduced" in text
    assert "Events that remain unthrottled" in text


def test_v390_coalescing_and_live_status_are_present_without_guard_relaxation() -> None:
    assert "DIAGNOSTIC_FIRST_SUMMARY_SECONDS = 60.0" in CONTROLLER
    assert "DIAGNOSTIC_REPEAT_SUMMARY_SECONDS = 5.0 * 60.0" in CONTROLLER
    assert "NATIVE_ORDER_WAIT_SUMMARY_SECONDS = 15.0 * 60.0" in CONTROLLER
    assert "def _update_audit_condition" in CONTROLLER
    assert "def _clear_audit_condition" in CONTROLLER
    assert "def _stage3_sell_guard_reason_code" in CONTROLLER
    assert '"stage3_sell_quote_status"' in CONTROLLER
    assert "STAGE3_SELL_CONFIRMATIONS_REQUIRED = 2" in CONTROLLER
    assert "SELL_MARKET_DATA_REVALIDATION_BLOCKED" in CONTROLLER
    assert "Stage 3 SELL evidence" in GUI
    assert "suppressed audit rows" in GUI


def test_v390_adds_no_database_migration_or_persisted_diagnostic_setting() -> None:
    schema_doc = (ROOT / "docs" / "DATABASE_SCHEMA.md").read_text(encoding="utf-8")
    config_doc = (ROOT / "docs" / "CONFIGURATION_REFERENCE.md").read_text(encoding="utf-8")
    assert "v3.9.0 changes only in-memory diagnostic coalescing" in schema_doc
    assert "adds no SQLite object, migration, index, or persisted setting" in schema_doc
    assert "Audit diagnostic cadence (runtime only)" in config_doc
    assert "no GUI control and no persisted setting" in config_doc
