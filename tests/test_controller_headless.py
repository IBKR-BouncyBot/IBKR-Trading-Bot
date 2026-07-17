"""Controller tests that run without the real PySide6 package.

The production controller imports Qt only for signals. These tests inject a tiny
QtCore stub before importing the controller so pure recovery/guard behavior can
be tested in Linux CI without a GUI runtime.
"""

from __future__ import annotations

import importlib
import sys
import types

from app.ib_adapter import OrderHandle, QualifiedContract, RthStatus
from app.models import Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyAction, StrategyEngine


class _FakeSignal:
    def __init__(self, *args, **kwargs):
        self.emissions = []

    def emit(self, *args, **kwargs):
        self.emissions.append((args, kwargs))


class _FakeQObject:
    pass


def _install_qt_stub(monkeypatch):
    # Windows builds install real PySide6 before running tests. If app.controller
    # was imported during pytest collection, its Signal objects may still be real
    # Qt descriptors. Remove the cached controller module before installing the
    # stub so these headless tests never emit through real Qt. monkeypatch
    # restores the previous module after the test.
    monkeypatch.delitem(sys.modules, "app.controller", raising=False)
    app_pkg = sys.modules.get("app")
    if app_pkg is not None and hasattr(app_pkg, "controller"):
        monkeypatch.delattr(app_pkg, "controller", raising=False)

    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QObject = _FakeQObject
    qtcore.Signal = _FakeSignal
    qtcore.QByteArray = bytes
    pyside.QtCore = qtcore
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    return importlib.import_module("app.controller")


class RthFakeAdapter:
    def __init__(self, is_open: bool):
        self.is_open = is_open
        self.placed_orders = []

    def is_connected(self):
        return True

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker, con_id=con_id or 123, raw=object(), primary_exchange=primary_exchange)

    def regular_trading_hours_status(self, contract):
        return RthStatus(self.is_open, "test", "open" if self.is_open else "closed", utc_now_iso())

    def what_if_trailing_stop(self, **kwargs):
        return {"ok": True, "message": "test what-if pass"}

    def place_trailing_stop(self, **kwargs):
        self.placed_orders.append(kwargs)
        return OrderHandle(
            order_ref=kwargs["order_ref"],
            order_id=99,
            perm_id=199,
            status="Submitted",
            raw=dict(kwargs),
        )



def _controller_with_cycle(tmp_path, monkeypatch, is_open: bool):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = RthFakeAdapter(is_open=is_open)
    controller.connected = True
    controller.connection.account = "SIM"

    base = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    cycle = StrategyEngine.start_cycle(base, 1, "SIM", 100.0, 0.0)
    cycle.last_price = 94.0  # below the drop trigger; an edit can immediately advance the stage.
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return controller


def test_strategy_edit_does_not_bypass_rth_guard_when_market_closed(tmp_path, monkeypatch):
    controller = _controller_with_cycle(tmp_path, monkeypatch, is_open=False)

    edited = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    controller._apply_active_strategy_edits(edited)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.error_message.startswith("RTH guard:")
    assert controller.adapter.placed_orders == []


def test_strategy_edit_places_order_when_same_condition_is_rth_open(tmp_path, monkeypatch):
    controller = _controller_with_cycle(tmp_path, monkeypatch, is_open=True)

    edited = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    controller._apply_active_strategy_edits(edited)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert len(controller.adapter.placed_orders) == 1
    assert controller.adapter.placed_orders[0]["action"] == "BUY"


def test_buy_trail_terminal_without_fill_resets_to_stage_1_without_manual_review(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    from app.ib_adapter import PolledOrderState

    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = StrategySettings(ticker="AAPL", investment_amount=1000.0)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-1|BUY_TRAIL"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    polled = PolledOrderState(
        order_ref=cycle.buy_order_ref,
        order_id=1,
        perm_id=2,
        status="Inactive",
        filled=0,
        remaining=10,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"status": "Inactive"},
    )
    controller._handle_buy_order_poll(cycle, polled)

    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.buy_order_ref is None
    assert "no filled quantity" in controller.active_cycle.error_message


def test_protective_cancel_confirmation_does_not_force_manual_review(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    from app.ib_adapter import PolledOrderState

    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = StrategySettings(ticker="AAPL", investment_amount=1000.0, protective_sell_enabled=True)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 5
    cycle.avg_buy_price = 95.0
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|CYCLE-1|PROTECTIVE_SELL"
    cycle.protective_sell_cancel_requested = True
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    polled = PolledOrderState(
        order_ref=cycle.protective_sell_order_ref,
        order_id=1,
        perm_id=2,
        status="Cancelled",
        filled=0,
        remaining=5,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"status": "Cancelled"},
    )
    handled = controller._handle_protective_sell_order_poll(cycle, polled)

    assert handled is False
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.protective_sell_cancel_requested is False


def test_market_data_refreshes_after_cycle_stopped(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    from app.ib_adapter import MarketPriceSnapshot

    class PriceAdapter(RthFakeAdapter):
        def __init__(self):
            super().__init__(is_open=True)
            self.market_data_types = []
            self.price_reads = 0
        def set_market_data_type(self, market_data_type):
            self.market_data_types.append(market_data_type)
        def price_snapshot(self, contract, timeout=0.75):
            self.price_reads += 1
            return MarketPriceSnapshot(
                price=101.23,
                source="marketPrice",
                requested_market_data_type=1,
                subscription_market_data_type=1,
                fields={"marketPrice": 101.23, "bid": 101.22, "ask": 101.24},
                timestamp="2026-01-01T14:30:00Z",
                status="OK",
            )

    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = PriceAdapter()
    controller.adapter = adapter
    controller.connected = True
    controller.contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.STOPPED
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._tick()

    assert adapter.price_reads == 1
    assert controller.price_snapshot is not None
    assert controller.price_snapshot["price"] == 101.23


def test_buy_trailing_order_price_is_rounded_up_to_contract_tick(tmp_path, monkeypatch):
    controller = _controller_with_cycle(tmp_path, monkeypatch, is_open=True)
    controller.contract = QualifiedContract(ticker="NBIS", con_id=123, raw=object(), primary_exchange="NASDAQ", min_tick=0.01)
    controller.active_cycle.ticker = "NBIS"
    controller.active_cycle.budget = 10000.0
    controller.active_cycle.what_if_check_enabled = False
    controller.price_snapshot = {
        "price": 212.81,
        "fields": {"marketPrice": 212.81, "ask": 212.99, "last": 212.80},
    }
    action = StrategyAction(
        "PLACE_BUY_TRAIL",
        {
            "ticker": "NBIS",
            "quantity": 46,
            "trailing_percent": 0.10,
            "initial_stop_price": 213.0428,
            "order_ref": "IBKRBOT|NBIS|CYCLE-000001|BUY_TRAIL",
            "sizing_price": 213.0428,
        },
    )

    controller._place_trailing_order(controller.active_cycle, action, "BUY")

    placed = controller.adapter.placed_orders[-1]
    assert placed["initial_stop_price"] == 213.21
    assert placed["quantity"] == 46


def test_sell_trailing_order_price_is_rounded_down_to_contract_tick(tmp_path, monkeypatch):
    controller = _controller_with_cycle(tmp_path, monkeypatch, is_open=True)
    controller.contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ", min_tick=0.01)
    cycle = controller.active_cycle
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.rise_trigger_pct = 1.0
    cycle.sell_trailing_stop_pct = 0.5
    cycle.what_if_check_enabled = False
    cycle.sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|SELL_TRAIL"
    controller.price_snapshot = {
        "price": 102.00,
        "fields": {"marketPrice": 102.00, "bid": 101.97, "last": 102.01},
    }
    action = StrategyAction(
        "PLACE_SELL_TRAIL",
        {
            "ticker": "AAPL",
            "quantity": 10,
            "trailing_percent": 0.5,
            "initial_stop_price": 101.4899,
            "order_ref": cycle.sell_order_ref,
        },
    )

    controller._place_trailing_order(cycle, action, "SELL")

    placed = controller.adapter.placed_orders[-1]
    assert placed["initial_stop_price"] == 101.46
