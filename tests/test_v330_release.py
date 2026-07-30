"""v3.6.0 release metadata, compatibility, and documentation regressions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
ADAPTER = (ROOT / "app" / "ib_adapter.py").read_text(encoding="utf-8")
STRATEGY = (ROOT / "app" / "strategy.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
DOCS_INDEX = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
LEGACY_INDEX = (ROOT / "docs" / "legacy" / "README.md").read_text(encoding="utf-8")
CURRENT_NOTE = ROOT / "docs" / "V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md"
ARCHIVED_V330_NOTE = ROOT / "docs" / "legacy" / "V3_3_0_DARK_MODE_AUDIT_AND_WINDOWS_RELEASE.md"
ARCHIVED_V322_NOTE = ROOT / "docs" / "legacy" / "V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md"


def test_v330_release_metadata_is_consistent() -> None:
    assert 'APP_VERSION = "3.6.0"' in GUI
    assert "BouncyBot - IBKR Portable Trading Bot v3.6.0" in GUI
    assert "This is synthetic v3.6.0 paper-trading example data." in GUI
    assert "**Current release: v3.6.0**" in README
    assert 'version = "3.6.0"' in PYPROJECT
    assert '$version = "3.6.0"' in BUILD
    assert "## v3.6.0" in CHANGELOG
    assert "current repository version, v3.6.0" in SECURITY
    assert "current v3.6.0 behavior" in DOCS_INDEX


def test_v330_release_note_is_current_and_v322_is_archived() -> None:
    assert CURRENT_NOTE.is_file()
    assert ARCHIVED_V330_NOTE.is_file()
    assert ARCHIVED_V322_NOTE.is_file()
    assert not (ROOT / "docs" / ARCHIVED_V330_NOTE.name).exists()
    assert not (ROOT / "docs" / ARCHIVED_V322_NOTE.name).exists()
    assert CURRENT_NOTE.name in README
    assert CURRENT_NOTE.name in DOCS_INDEX
    assert ARCHIVED_V330_NOTE.name in README
    assert ARCHIVED_V330_NOTE.name in LEGACY_INDEX
    assert ARCHIVED_V322_NOTE.name in README
    assert ARCHIVED_V322_NOTE.name in LEGACY_INDEX


def test_v321_incident_gap_corrections_remain_present() -> None:
    assert "_PRIMARY_EXCHANGE_CONTINUOUS_SESSIONS" in ADAPTER
    assert '"LSE": ("Europe/London", dt.time(8, 0), dt.time(16, 30))' in ADAPTER
    assert '"LSEETF": ("Europe/London", dt.time(8, 0), dt.time(16, 30))' in ADAPTER
    assert "_apply_primary_exchange_continuous_session" in ADAPTER
    assert "_close_cached_rth_status_at_boundary" in ADAPTER
    assert "BUY_PREFLIGHT_AUDIT_THROTTLE_SECONDS = 60.0" in CONTROLLER
    assert "buy_preflight_block|" in CONTROLLER
    assert "rollback_preflight_blocked_order" in STRATEGY
    assert 'next_cycle.buy_status = "PreflightBlocked"' in STRATEGY


def test_v330_release_note_documents_scope_and_compatibility() -> None:
    text = ARCHIVED_V330_NOTE.read_text(encoding="utf-8")
    assert "Automatic and selectable Fusion themes" in text
    assert "About screen logo" in text
    assert "Cycle Audit Timeline tables" in text
    assert "Cycle Audit Market capture tab" in text
    assert "View > Light" in text
    assert "BouncyBot.lnk" in text
    assert "adds no SQLite table, column, index, data migration, or persisted setting" in text
    assert "Existing v3.2.2 portable databases" in text
    assert "must be assembled on Windows" in text


def test_v330_theme_integration_is_present_without_trading_layer_changes() -> None:
    assert "_system_prefers_dark" in MAIN
    assert "_install_system_theme_hook" in MAIN
    assert "colorSchemeChanged" in MAIN
    assert "def apply_system_theme" in GUI
    assert "def _dark_stylesheet" in GUI
    assert "DARK_MODE_APP_PROPERTY" in GUI
    assert 'self.menuBar().addMenu("View")' in GUI
    assert 'QAction("Light mode", self)' in GUI
    assert 'QAction("Dark mode", self)' in GUI
    assert "DARK_MODE_APP_PROPERTY" not in CONTROLLER
    assert "DARK_MODE_APP_PROPERTY" not in STRATEGY
