from __future__ import annotations

from pathlib import Path

from app.storage import BotStorage
from tests.test_controller_headless import _install_qt_stub

GUI = Path("app/gui.py").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")
ADAPTER = Path("app/ib_adapter.py").read_text(encoding="utf-8")


class AccountDisplayAdapter:
    def __init__(self, accounts):
        self.accounts = list(accounts)

    def managed_accounts(self):
        return list(self.accounts)


def test_top_status_account_uses_broker_display_account_without_changing_order_account(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = AccountDisplayAdapter(["DU1234567"])
    controller.connection.account = ""

    controller._refresh_display_accounts()
    controller.emit_snapshot(force=True)

    snapshot = controller.signals.snapshot_updated.emissions[-1][0][0]
    assert snapshot["display_account"] == "DU1234567"
    assert snapshot["broker_accounts"] == ["DU1234567"]
    assert snapshot["connection"]["account"] == ""


def test_configured_account_overrides_broker_display_account(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = AccountDisplayAdapter(["DU9999999"])
    controller.connection.account = "DU1234567"

    controller._refresh_display_accounts()
    controller.emit_snapshot(force=True)

    snapshot = controller.signals.snapshot_updated.emissions[-1][0][0]
    assert snapshot["display_account"] == "DU1234567"
    assert snapshot["broker_accounts"] == ["DU9999999"]


def test_account_display_is_gui_status_only():
    assert "def managed_accounts(self) -> list[str]" in ADAPTER
    assert "This is display-only metadata for the GUI status bar" in ADAPTER
    assert '"display_account": self._display_account_label()' in CONTROLLER
    assert '"broker_accounts": list(self._broker_display_accounts)' in CONTROLLER
    assert "ConnectionSettings.account is an optional routing override" in CONTROLLER
    assert "broker orders leave Order.account unset" in CONTROLLER


def test_command_bar_done_search_and_confirm_are_disabled_while_strategy_runs():
    assert 'if active_stage:' in GUI
    assert 'ticker_done = bool(has_selected_contract or cycle.get("ticker") or ticker_text)' in GUI
    assert 'price_done = bool(has_price or cycle.get("last_price"))' in GUI
    assert 'self.command_steps["ticker"].set_state(' in GUI
    assert 'self.command_steps["confirm"].set_state(' in GUI
    assert GUI.count('"Locked while strategy is running"') >= 2


def test_v221_version_strings():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.0" in GUI
    assert '# BouncyBot - an IBKR Portable Trading Bot ' in Path("README.md").read_text(encoding="utf-8")
    assert 'version = "3.1.0"' in Path("pyproject.toml").read_text(encoding="utf-8")
