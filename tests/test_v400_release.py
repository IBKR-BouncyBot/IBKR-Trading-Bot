"""Current release metadata and the narrow v4.0.0 compatibility boundary."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTE = "V4_0_0_ATR_SESSION_MEMORY_AND_ORDER_EDITING.md"


def test_current_metadata_and_windows_build_version():
    gui = (ROOT / "app/gui.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "4.0.0"' in gui
    assert 'version = "4.0.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '$version = "4.0.0"' in (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert "**Current release: v4.0.0**" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_current_note_and_archived_v390_material():
    assert (ROOT / "docs" / NOTE).is_file()
    assert not (ROOT / "docs/V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md").exists()
    assert (ROOT / "docs/legacy/V3_9_0_AUDIT_DIAGNOSTIC_COALESCING.md").is_file()
    assert (ROOT / "docs/legacy/V3_9_0_IMPLEMENTATION_TEST_REPORT.txt").is_file()
    assert NOTE in (ROOT / "README.md").read_text(encoding="utf-8")
    assert NOTE in (ROOT / "docs/README.md").read_text(encoding="utf-8")


def test_scope_and_upgrade_are_explicit():
    note = (ROOT / "docs" / NOTE).read_text(encoding="utf-8")
    assert "seven calendar days" in note
    assert "working Stage-2 BUY retains its original" in note
    assert "Existing v3.9.0 databases remain compatible" in note
    assert "Cancel** is the default/focused action" in note
    schema = (ROOT / "docs/DATABASE_SCHEMA.md").read_text(encoding="utf-8")
    assert "atr_rth_seed_v1:" in schema
    assert "next_order_risk_edits_v1" in schema


def test_existing_order_and_worker_guards_remain_explicit():
    controller = (ROOT / "app/controller.py").read_text(encoding="utf-8")
    assert "STAGE3_SELL_CONFIRMATIONS_REQUIRED = 2" in controller
    assert "SELL_MARKET_DATA_REVALIDATION_BLOCKED" in controller
    assert "PROTECTIVE_SELL_PARTIAL_TERMINAL" in controller
    assert "SELL_QUANTITY_MISMATCH" in controller
    assert "BUY_PARTIAL_FILL_GRACE_SECONDS = 3.0" in controller
