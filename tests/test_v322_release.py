"""v3.2.2 release metadata and GUI-layout regressions."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
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
CURRENT_NOTE = ROOT / "docs" / "V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md"
ARCHIVED_V321_NOTE = ROOT / "docs" / "legacy" / "V3_2_1_INCIDENT_GAP_CORRECTIONS.md"


def test_v322_release_metadata_is_consistent() -> None:
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.2" in GUI
    assert "This is synthetic v3.2.2 paper-trading example data." in GUI
    assert "**Current release: v3.2.2**" in README
    assert 'version = "3.2.2"' in PYPROJECT
    assert '$version = "3.2.2"' in BUILD
    assert "## v3.2.2" in CHANGELOG
    assert "current repository version, v3.2.2" in SECURITY
    assert "current v3.2.2 behavior" in DOCS_INDEX


def test_v322_release_note_is_current_and_v321_is_archived() -> None:
    assert CURRENT_NOTE.is_file()
    assert ARCHIVED_V321_NOTE.is_file()
    assert not (ROOT / "docs" / "V3_2_1_INCIDENT_GAP_CORRECTIONS.md").exists()
    assert "V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md" in README
    assert "V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md" in DOCS_INDEX
    assert "V3_2_1_INCIDENT_GAP_CORRECTIONS.md" in LEGACY_INDEX


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


def test_v322_release_note_documents_scope_and_compatibility() -> None:
    text = CURRENT_NOTE.read_text(encoding="utf-8")
    assert "price data monitor" in text.lower()
    assert "IBKR long name" in text
    assert "content width" in text
    assert "top edge of the tab" in text
    assert "adds no SQLite table, column, index, or data migration" in text
    assert "Existing v3.2.1" in text


def test_v322_gui_source_contains_requested_layout_changes() -> None:
    assert 'self.big_ticker.setObjectName("BigPrice")' in GUI
    assert 'self.instrument_info.setObjectName("PriceInstrumentInfo")' in GUI
    assert "_fit_table_width_to_columns(risk_table, minimum=300, maximum=480)" in GUI
    assert "split.addWidget(transition_table, 1, Qt.AlignTop)" in GUI
    assert "split.addWidget(risk_table, 0, Qt.AlignTop)" in GUI
    assert GUI.count("return self._top_aligned_table_tab(table)") == 3
