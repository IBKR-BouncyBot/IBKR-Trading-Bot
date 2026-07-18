from __future__ import annotations

from pathlib import Path

CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")
GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
ARCHIVE = Path("docs/legacy/README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
DOC = Path("docs/legacy/V3_0_6_OPTIONAL_IBKR_ACCOUNT.md").read_text(encoding="utf-8")


def test_live_account_is_optional_in_controller_and_gui():
    assert "Live trading requires an explicit IBKR account" not in CONTROLLER
    assert "Live trading requires an explicit IBKR account" not in GUI
    assert 'and account:' in CONTROLLER
    assert 'account=(self.connection.account or cycle.account)' in CONTROLLER


def test_blank_account_is_described_as_ibkr_default():
    assert 'account_text = account or "IBKR default"' in GUI
    assert "Optional override; blank uses IBKR default" in GUI
    assert "Account is optional; blank leaves account selection to IBKR." in GUI


def test_v306_version_and_documentation():
    assert "IBKR Portable Trading Bot v3.0.18" in GUI
    assert "# IBKR Portable Trading Bot v3.0.18" in README
    assert 'version = "3.0.18"' in PYPROJECT
    assert "v3.0.6 optional IBKR account routing" in ARCHIVE
    assert "v3.0.6 optional IBKR account routing" in DOC
