from __future__ import annotations

from pathlib import Path

from app.ib_adapter import MarketPriceSnapshot, QualifiedContract, RthStatus
from app.models import ConnectionSettings, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub

GUI = Path("app/gui.py").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")


class _AtrStartAdapter:
    def __init__(self, price: float = 90.0):
        self.price = price
        self.market_data_type = 1

    def is_connected(self):
        return True

    def set_market_data_type(self, market_data_type):
        self.market_data_type = market_data_type

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(
            ticker=ticker,
            con_id=con_id or 123,
            raw=object(),
            primary_exchange=primary_exchange,
        )

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def price_snapshot(self, contract, timeout=1.0):
        return MarketPriceSnapshot(
            price=self.price,
            source="test",
            requested_market_data_type=self.market_data_type,
            subscription_market_data_type=1,
            fields={"last": self.price, "bid": self.price - 0.01, "ask": self.price + 0.01},
            timestamp=utc_now_iso(),
            status="OK",
        )


def _atr_settings(**overrides):
    values = {
        "ticker": "AAPL",
        "investment_amount": 1000.0,
        "initial_drop_pct": 2.0,
        "buy_rebound_trail_pct": 1.0,
        "atr_adaptive_enabled": True,
        "atr_block_new_buy_until_ready": True,
        "atr_period": 14,
        "hard_risk_limits_enabled": False,
        "block_delayed_data_in_live": False,
        "what_if_check_enabled": False,
        "stale_data_guard_enabled": False,
        "volatility_filter_enabled": False,
        "session_timing_guard_enabled": False,
    }
    values.update(overrides)
    return StrategySettings(**values)


def test_start_with_atr_warmup_block_arms_no_initial_drop_trigger(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _AtrStartAdapter(price=90.0)
    controller.connected = True
    controller.connection = ConnectionSettings(account="", trading_mode="paper", market_data_type=1)
    controller.strategy = _atr_settings()

    controller._start_strategy(controller.strategy)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.anchor_price == 90.0
    assert controller.active_cycle.drop_trigger_price is None
    assert controller.active_cycle.error_message is not None
    assert controller.active_cycle.error_message.startswith(controller.ATR_WARMUP_BLOCK_PREFIX)


def test_atr_warmup_ignores_pre_ready_drop_and_uses_fresh_ready_anchor(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = _atr_settings(initial_drop_pct=2.0)
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)

    controller.price_snapshot = {
        "price": 90.0,
        "atr_ready": False,
        "atr_bars_available": 3,
        "atr_bars_required": 15,
        "atr": {"ready": False, "bars_available": 3, "bars_required": 15},
    }
    paused, actions = controller._advance_waiting_cycle_from_price(
        cycle,
        90.0,
        is_rth=True,
        rth_message="open",
    )
    assert actions == []
    assert paused.anchor_price == 90.0
    assert paused.drop_trigger_price is None

    # This readiness tick is far below the pre-warmup anchor. It establishes a new
    # post-warmup anchor and must not immediately submit a BUY.
    controller.price_snapshot = {
        "price": 80.0,
        "atr_ready": True,
        "atr_bars_available": 15,
        "atr_bars_required": 15,
        "atr": {"ready": True, "bars_available": 15, "bars_required": 15},
    }
    restarted, actions = controller._advance_waiting_cycle_from_price(
        paused,
        80.0,
        is_rth=True,
        rth_message="open",
    )
    assert actions == []
    assert restarted.stage == Stage.WAIT_INITIAL_DROP
    assert restarted.anchor_price == 80.0
    assert restarted.drop_trigger_price == 78.4
    assert restarted.error_message is None

    triggered, actions = controller._advance_waiting_cycle_from_price(
        restarted,
        78.0,
        is_rth=True,
        rth_message="open",
    )
    assert triggered.stage == Stage.BUY_TRAIL_ACTIVE
    assert len(actions) == 1
    assert actions[0].action_type == "PLACE_BUY_TRAIL"


def test_trading_status_lists_atr_and_other_active_buy_guards(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = _atr_settings(hard_risk_limits_enabled=True, max_spread_pct=0.5)
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    controller.active_cycle = cycle
    controller.connected = True
    controller.connection = ConnectionSettings(account="", trading_mode="paper", market_data_type=1)
    controller._latest_rth_status = {"is_open": True, "message": "RTH open"}
    controller.price_snapshot = {
        "price": 100.0,
        "atr_ready": False,
        "atr_bars_available": 3,
        "atr_bars_required": 15,
        "atr": {"ready": False, "bars_available": 3, "bars_required": 15},
        "fields": {"bid": 99.0, "ask": 101.0, "last": 100.0},
    }

    status = controller._trading_status_snapshot()

    assert status["summary"] == "BUY blocked: ATR 3/15 +1"
    assert status["state"] == "waiting"
    assert {item["code"] for item in status["blockers"]} == {"atr_warmup", "spread"}
    assert "ATR warmup guard blocked BUY" in status["tooltip"]
    assert "spread 2.00% exceeds max 0.50%" in status["tooltip"]


def test_trading_status_reports_sell_side_market_data_blocker(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = _atr_settings(atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 5
    cycle.avg_buy_price = 99.0
    controller.active_cycle = cycle
    controller.connected = True
    controller._latest_rth_status = {"is_open": True, "message": "RTH open"}
    controller.price_snapshot = {"price": None, "fields": {}}

    status = controller._trading_status_snapshot()

    assert status["summary"] == "SELL blocked: No usable price"
    assert status["blockers"][0]["side"] == "SELL"
    assert status["blockers"][0]["code"] == "no_price"


def test_trading_status_reports_unsold_app_owned_position(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    settings = _atr_settings(atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)

    prior = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    prior.stage = Stage.STOPPED
    prior.buy_filled_qty = 4
    prior.avg_buy_price = 99.0
    storage.upsert_cycle(prior)

    current = StrategyEngine.start_cycle(settings, 2, "", 100.0, 0.0)
    controller.active_cycle = current
    controller.connected = True
    controller._latest_rth_status = {"is_open": True, "message": "RTH open"}
    controller.price_snapshot = {"price": 100.0, "fields": {"last": 100.0}}

    status = controller._trading_status_snapshot()

    assert status["summary"] == "BUY blocked: App position 4"
    assert status["blockers"][0]["code"] == "app_owned_position"
    assert "Manual or externally acquired broker holdings are not counted" in status["tooltip"]


def test_gui_lock_disables_all_five_workflow_buttons_and_trading_pill_uses_blockers():
    lock_start = GUI.index("def _manual_input_lock_toggled")
    lock_block = GUI[lock_start: GUI.index("def _install_no_wheel_field_filter", lock_start)]
    states_start = GUI.index("def _update_command_bar_states")
    states_block = GUI[states_start: GUI.index("def _apply_view_mode", states_start)]

    assert "self._update_command_bar_states(self.current_snapshot)" in lock_block
    assert 'for key in ("connect", "ticker", "confirm", "start", "stop")' in states_block
    assert 'self.command_steps[key].set_state(' in states_block
    assert '"Locked"' in states_block
    assert '"Unlock the top-bar input lock to use this workflow action."' in states_block
    assert 'trading_status = snapshot.get("trading_status") or {}' in GUI
    assert 'self.pills["Trading"].setToolTip(trading_tooltip)' in GUI


def test_buy_preflight_uses_app_fill_ledger_not_account_wide_position():
    start = CONTROLLER.index("def _buy_submission_preflight_message")
    block = CONTROLLER[start: CONTROLLER.index("def _record_order_intent", start)]

    assert "_app_owned_position_blocker_for_buy" in block
    assert "position_size" not in block
    assert "Manual or externally acquired broker holdings are not counted" in CONTROLLER
