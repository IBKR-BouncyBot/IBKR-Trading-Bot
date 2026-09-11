from __future__ import annotations

import importlib
import sys
import types

from app.ib_adapter import OrderHandle, PolledOrderState, QualifiedContract, RthStatus
from app.models import Stage, StopAction, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine


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

    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QObject = _FakeQObject
    qtcore.Signal = _FakeSignal
    qtcore.QByteArray = bytes
    pyside.QtCore = qtcore
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    return importlib.import_module("app.controller")


class MarketCloseAdapter:
    def __init__(self):
        self.cancelled = []
        self.market_orders = []

    def is_connected(self):
        return True

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker, con_id=con_id or 123, raw=object(), primary_exchange=primary_exchange, min_tick=0.01)

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def cancel_order(self, order_ref, order_id=None):
        self.cancelled.append((order_ref, order_id))

    def place_market_order(self, **kwargs):
        self.market_orders.append(dict(kwargs))
        return OrderHandle(
            order_ref=kwargs["order_ref"],
            order_id=777,
            perm_id=1777,
            status="Submitted",
            raw={"orderType": "MKT", **kwargs},
        )


def _controller_and_cycle(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = MarketCloseAdapter()
    controller.adapter = adapter
    controller.connected = True
    controller.contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ", min_tick=0.01)
    settings = StrategySettings(ticker="AAPL", investment_amount=1000.0)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|abc|BUY_TRAIL"
    cycle.buy_status = "Filled"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return controller, cycle, adapter


def test_stop_close_market_sells_unsold_app_quantity_immediately_when_no_sell_order_working(tmp_path, monkeypatch):
    controller, cycle, adapter = _controller_and_cycle(tmp_path, monkeypatch)

    controller._apply_stop_action(StopAction.SELL_APP_POSITION_MARKET)

    assert len(adapter.market_orders) == 1
    order = adapter.market_orders[0]
    assert order["action"] == "SELL"
    assert order["quantity"] == 10
    assert "FORCED_SELL_MARKET" in order["order_ref"]
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert controller.active_cycle.sell_order_id == 777


def test_stop_close_market_waits_for_protective_sell_cancel_before_market_order(tmp_path, monkeypatch):
    controller, cycle, adapter = _controller_and_cycle(tmp_path, monkeypatch)
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|abc|PROTECTIVE_SELL_TRAIL"
    cycle.protective_sell_order_id = 111
    cycle.protective_sell_status = "Submitted"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._apply_stop_action(StopAction.SELL_APP_POSITION_MARKET)

    assert adapter.cancelled == [(cycle.protective_sell_order_ref, 111)]
    assert adapter.market_orders == []
    assert controller.active_cycle.close_position_market_requested is True

    polled = PolledOrderState(
        order_ref=cycle.protective_sell_order_ref,
        order_id=111,
        perm_id=211,
        status="Cancelled",
        filled=0,
        remaining=10,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"status": "Cancelled"},
    )
    controller._handle_protective_sell_order_poll(controller.active_cycle, polled)

    assert len(adapter.market_orders) == 1
    assert adapter.market_orders[0]["quantity"] == 10
    assert "FORCED_SELL_MARKET" in adapter.market_orders[0]["order_ref"]


def test_stop_close_market_waits_for_existing_final_sell_cancel_before_market_order(tmp_path, monkeypatch):
    controller, cycle, adapter = _controller_and_cycle(tmp_path, monkeypatch)
    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|abc|SELL_TRAIL"
    cycle.sell_order_id = 222
    cycle.sell_status = "Submitted"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._apply_stop_action(StopAction.SELL_APP_POSITION_MARKET)

    assert adapter.cancelled == [(cycle.sell_order_ref, 222)]
    assert adapter.market_orders == []

    polled = PolledOrderState(
        order_ref=cycle.sell_order_ref,
        order_id=222,
        perm_id=322,
        status="Cancelled",
        filled=0,
        remaining=10,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"status": "Cancelled"},
    )
    controller._handle_sell_order_poll(controller.active_cycle, polled)

    assert len(adapter.market_orders) == 1
    assert adapter.market_orders[0]["action"] == "SELL"
    assert adapter.market_orders[0]["quantity"] == 10


def test_stop_close_market_does_not_sell_when_no_app_bought_quantity_exists(tmp_path, monkeypatch):
    controller, cycle, adapter = _controller_and_cycle(tmp_path, monkeypatch)
    cycle.buy_filled_qty = 0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._apply_stop_action(StopAction.SELL_APP_POSITION_MARKET)

    assert adapter.market_orders == []
    assert controller.active_cycle.stage == Stage.STOPPED
    assert "no unsold bought quantity" in controller.active_cycle.error_message
