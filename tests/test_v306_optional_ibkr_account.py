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
    # The optional GUI override no longer permits ambiguous execution routing:
    # v5.0 binds the cycle before transmitting any order.
    assert 'def _bind_cycle_account(' in CONTROLLER
    assert 'account=cycle.account,' in CONTROLLER
    assert 'account=(self.connection.account or cycle.account)' not in CONTROLLER


def test_blank_account_requires_one_unambiguous_managed_account():
    assert 'account_text = account or ("Auto (single managed account)" if connected and local_connected else "N/A")' in GUI
    assert "Optional; auto-select a single managed account" in GUI
    assert "Account is optional; blank requires one unambiguous managed account." in GUI


def test_v306_version_and_documentation():
    assert "BouncyBot - IBKR Portable Trading Bot v5.7.0" in GUI
    assert "# BouncyBot - an IBKR Trading Bot" in README
    assert 'version = "5.7.0"' in PYPROJECT
    assert "v3.0.6 optional IBKR account routing" in ARCHIVE
    assert "v3.0.6 optional IBKR account routing" in DOC
