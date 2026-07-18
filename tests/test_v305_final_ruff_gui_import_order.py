from __future__ import annotations

from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
ARCHIVE = Path("docs/legacy/README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
DOC = Path("docs/legacy/V3_0_5_FINAL_RUFF_GUI_IMPORT_FIX.md").read_text(encoding="utf-8")


def test_v305_remaining_gui_imports_follow_ruff_order():
    assert GUI.index("    QTableWidget,\n") < GUI.index("    QTableWidgetItem,\n")
    assert GUI.index("    QTableWidgetItem,\n") < GUI.index("    QTabWidget,\n")
    assert GUI.index("    choose_timestamp_for_display,\n") < GUI.index("    clamp_fraction,\n")


def test_v305_version_and_patch_documentation_are_current():
    assert "IBKR Portable Trading Bot v3.0.18" in GUI
    assert "# IBKR Portable Trading Bot v3.0.18" in README
    assert 'version = "3.0.18"' in PYPROJECT
    assert "v3.0.5 final Ruff GUI import-order correction" in ARCHIVE
    assert "v3.0.5 final Ruff GUI import-order correction" in DOC
