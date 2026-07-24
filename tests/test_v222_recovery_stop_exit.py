from __future__ import annotations

from pathlib import Path

from app.models import Stage, StopAction, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub

GUI = Path("app/gui.py").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def _settings() -> StrategySettings:
    return StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
    )


def test_v222_version_metadata_is_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.2" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.2"' in PYPROJECT


def test_stop_and_exit_waits_until_local_stopped_state_is_persisted(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    cycle = StrategyEngine.start_cycle(_settings(), 1, "DU1234567", 100.0, 0.0)
    cycle.stage = Stage.WAIT_INITIAL_DROP
    storage.upsert_cycle(cycle)

    controller = controller_module.TradingController(storage=storage)
    controller.active_cycle = cycle
    controller.start_thread()
    try:
        assert controller.request_stop_and_wait(StopAction.STOP_NOW_NO_BROKER_ACTION, timeout=3.0) is True
        stored = storage.get_cycle(cycle.id)
        assert stored is not None
        assert stored.stage == Stage.STOPPED
        assert storage.get_latest_active_cycle("AAPL") is None
    finally:
        controller.shutdown()


def test_stop_and_exit_gui_uses_confirmed_worker_stop_before_close():
    assert "def request_stop_and_wait" in CONTROLLER
    assert "Stop strategy and exit app" in GUI
    assert "wait_for_local_state = bool(dialog.exit_app_after_action and dialog.selected_action == StopAction.STOP_NOW_NO_BROKER_ACTION)" in GUI
    assert "self._request_stop_action(dialog.selected_action, wait_for_local_state=wait_for_local_state)" in GUI
    assert "previous active cycle and keep ticker inputs" in GUI


def test_recovery_resume_button_is_not_enabled_only_for_startup_resume_gate():
    assert "startup_resume_only = bool(" in GUI
    assert "Stored cycle is paused until 4. Start strategy is clicked" in GUI
    assert "Use 4. Start strategy on the Live strategy tab to resume this stored cycle." in GUI
    assert "and not startup_resume_required" in GUI
    assert "permissions = _recovery_action_permissions(" in GUI
    assert 'can_stop_cycle = permissions["can_stop_cycle"]' in GUI
    assert "Recovery buttons remain disabled because no broker mismatch" in GUI
