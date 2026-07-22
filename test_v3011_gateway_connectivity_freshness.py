from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from app.ib_adapter import (
    BrokerConnectivityStatus,
    IbAsyncTwsAdapter,
    MarketPriceSnapshot,
    QualifiedContract,
)
from app.models import ConnectionSettings, Stage, StrategyAction, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class _Event:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class _FakeTicker:
    def __init__(self, contract) -> None:
        self.contract = contract
        self.marketDataType = 1
        self.time = "2026-07-10T14:30:00+00:00"
        self.last = 100.0
        self.delayedLast = None
        self.bid = 99.9
        self.ask = 100.1
        self.delayedBid = None
        self.delayedAsk = None
        self.close = 98.0
        self.delayedClose = None
        self.markPrice = 100.0
        self.delayedMarkPrice = None

    def marketPrice(self) -> float:
        return 100.0


class _FakeIb:
    def __init__(self) -> None:
        self.connected = True
        self.openOrderEvent = _Event()
        self.orderStatusEvent = _Event()
        self.execDetailsEvent = _Event()
        self.commissionReportEvent = _Event()
        self.errorEvent = _Event()
        self.disconnectedEvent = _Event()
        self.pendingTickersEvent = _Event()
        self.requested_tickers = []
        self.cancelled_contracts = []
        self.market_data_types = []

    def isConnected(self) -> bool:
        return self.connected

    def sleep(self, timeout: float) -> None:
        return None

    def reqMktData(self, contract, generic_tick_list, snapshot, regulatory_snapshot):
        ticker = _FakeTicker(contract)
        self.requested_tickers.append(ticker)
        return ticker

    def cancelMktData(self, contract) -> None:
        self.cancelled_contracts.append(contract)

    def reqMarketDataType(self, market_data_type: int) -> None:
        self.market_data_types.append(market_data_type)


class _ConnectivityAdapter:
    def __init__(self, *, upstream_connected: bool) -> None:
        self.ib = object()
        self.status = BrokerConnectivityStatus(
            local_connected=True,
            upstream_connected=upstream_connected,
            state="connected" if upstream_connected else "upstream_disconnected",
            message=(
                "IB Gateway is connected to IBKR servers."
                if upstream_connected
                else "Connectivity between IB Gateway and IBKR servers is lost."
            ),
            error_code=None if upstream_connected else 1100,
            market_data_event_tracking=True,
        )
        self.process_calls = 0
        self.price_calls = 0
        self.poll_calls = 0
        self.market_order_calls = 0

    def is_connected(self) -> bool:
        return True

    def connectivity_status(self) -> BrokerConnectivityStatus:
        return self.status

    def process_events(self, timeout: float = 0.0) -> None:
        self.process_calls += 1

    def drain_broker_events(self):
        return []

    def price_snapshot(self, contract, timeout: float = 0.75):
        self.price_calls += 1
        raise AssertionError("price_snapshot must not run while upstream connectivity is unavailable")

    def poll_order(self, order_ref):
        self.poll_calls += 1
        raise AssertionError("poll_order must not run while upstream connectivity is unavailable")

    def place_market_order(self, **kwargs):
        self.market_order_calls += 1
        raise AssertionError("place_market_order must not run while upstream connectivity is unavailable")


def _live_adapter() -> tuple[IbAsyncTwsAdapter, _FakeIb, QualifiedContract]:
    adapter = IbAsyncTwsAdapter()
    fake_ib = _FakeIb()
    adapter.ib = fake_ib
    adapter._register_broker_event_handlers()
    adapter._set_upstream_state(
        connected=True,
        state="connected_waiting_for_market_data",
        message="Waiting for the first fresh market-data update.",
        awaiting_fresh_market_data=True,
    )
    raw = SimpleNamespace(conId=123, exchange="SMART", primaryExchange="NASDAQ")
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=raw, primary_exchange="NASDAQ")
    return adapter, fake_ib, contract


def _tracked_snapshot(sequence: int, *, event_age: float = 0.0, price: float = 100.0) -> MarketPriceSnapshot:
    return MarketPriceSnapshot(
        price=price,
        source="marketPrice",
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields={"marketPrice": price, "last": price, "bid": price - 0.1, "ask": price + 0.1},
        timestamp=utc_now_iso(),
        status="OK",
        api_data_received=True,
        api_data_field_count=4,
        market_data_update_sequence=sequence,
        market_data_subscription_id="AAPL|SMART|g1",
        market_data_update_received_at=utc_now_iso(),
        market_data_update_age_seconds=event_age,
        market_data_event_tracking=True,
        upstream_connected=True,
        upstream_state="connected",
    )


def test_live_adapter_distinguishes_actual_ticker_events_from_cached_reads():
    adapter, fake_ib, contract = _live_adapter()
    ticker = adapter._request_ticker(contract, "")

    before_event = adapter._snapshot_from_ticker(ticker, contract)
    assert before_event.price == 100.0
    assert before_event.api_data_received is False
    assert before_event.market_data_update_sequence == 0
    assert before_event.market_data_event_tracking is True

    fake_ib.pendingTickersEvent.emit([ticker])
    first = adapter._snapshot_from_ticker(ticker, contract)
    cached_read = adapter._snapshot_from_ticker(ticker, contract)

    assert first.api_data_received is True
    assert first.market_data_update_sequence is not None
    assert first.market_data_update_sequence > 0
    assert first.market_data_update_received_at
    assert cached_read.market_data_update_sequence == first.market_data_update_sequence

    fake_ib.pendingTickersEvent.emit([ticker])
    second = adapter._snapshot_from_ticker(ticker, contract)
    assert second.market_data_update_sequence > first.market_data_update_sequence


def test_1100_blocks_cached_quotes_and_1101_recreates_market_data_subscription():
    adapter, fake_ib, contract = _live_adapter()
    old_ticker = adapter._request_ticker(contract, "")
    fake_ib.pendingTickersEvent.emit([old_ticker])

    fake_ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    lost = adapter.connectivity_status()
    assert lost.local_connected is True
    assert lost.upstream_connected is False
    assert lost.trading_ready is False
    assert lost.awaiting_fresh_market_data is True

    blocked_snapshot = adapter.price_snapshot(contract)
    assert blocked_snapshot.price is None
    assert blocked_snapshot.upstream_connected is False
    assert "cached" not in blocked_snapshot.fields

    # A late callback from the old cached ticker cannot make the upstream state
    # ready while IBKR still reports code 1100.
    fake_ib.pendingTickersEvent.emit([old_ticker])
    assert adapter.connectivity_status().awaiting_fresh_market_data is True
    assert adapter.connectivity_status().upstream_connected is False

    events = adapter.drain_broker_events()
    assert events[-1]["event_type"] == "IBKR_UPSTREAM_DISCONNECTED"
    assert events[-1]["error_code"] == 1100

    fake_ib.errorEvent.emit(-1, 1101, "Connectivity restored - data lost.", None)
    restored = adapter.connectivity_status()
    assert restored.upstream_connected is True
    assert restored.market_data_resubscribe_required is True
    assert restored.awaiting_fresh_market_data is True
    assert adapter._tickers == {}

    new_ticker = adapter._request_ticker(contract, "")
    assert new_ticker is not old_ticker
    assert len(fake_ib.requested_tickers) == 2
    waiting = adapter._snapshot_from_ticker(new_ticker, contract)
    assert waiting.api_data_received is False
    assert waiting.market_data_update_sequence == 0

    fake_ib.pendingTickersEvent.emit([new_ticker])
    ready = adapter.connectivity_status()
    assert ready.upstream_connected is True
    assert ready.awaiting_fresh_market_data is False
    assert ready.market_data_resubscribe_required is False
    assert ready.state == "connected"


def test_1102_retains_subscription_but_requires_a_post_recovery_update():
    adapter, fake_ib, contract = _live_adapter()
    ticker = adapter._request_ticker(contract, "")
    fake_ib.pendingTickersEvent.emit([ticker])
    original_snapshot = adapter._snapshot_from_ticker(ticker, contract)
    original_sequence = original_snapshot.market_data_update_sequence
    original_subscription_id = original_snapshot.market_data_subscription_id

    fake_ib.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained.", None)
    restored = adapter.connectivity_status()
    assert restored.upstream_connected is True
    assert restored.market_data_resubscribe_required is False
    assert restored.awaiting_fresh_market_data is True
    assert next(iter(adapter._tickers.values())) is ticker

    cached = adapter._snapshot_from_ticker(ticker, contract)
    assert cached.price == 100.0
    assert cached.api_data_received is False
    assert cached.market_data_update_sequence == 0
    assert cached.market_data_subscription_id == original_subscription_id

    fake_ib.pendingTickersEvent.emit([ticker])
    fresh = adapter._snapshot_from_ticker(ticker, contract)
    assert fresh.market_data_subscription_id == original_subscription_id
    assert fresh.market_data_update_sequence > original_sequence
    assert adapter.connectivity_status().state == "connected"
    assert adapter.connectivity_status().awaiting_fresh_market_data is False


def test_controller_consumes_each_market_data_event_once_and_uses_event_age(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(
        ticker="AAPL",
        atr_adaptive_enabled=False,
        stale_data_guard_enabled=True,
        max_selected_price_age_seconds=3.0,
        max_bid_ask_age_seconds=3.0,
    )
    controller._latest_rth_status = {
        "is_open": True,
        "message": "RTH open",
        "checked_at": utc_now_iso(),
    }

    first = _tracked_snapshot(1, event_age=8.0)
    controller._record_price_snapshot(first, None)
    assert controller._api_data_seen_count == 1
    assert len(controller._price_history) == 1
    assert 7.0 <= float(controller.price_snapshot["api_data_age_seconds"]) <= 9.5
    assert 7.0 <= float(controller.price_snapshot["api_data_change_age_seconds"]) <= 9.5
    assert 7.0 <= time.monotonic() - controller._price_history[-1][0] <= 9.5

    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)
    assert "selected/API price age" in controller._stale_data_guard_message_for_buy(cycle)

    controller._record_price_snapshot(first, None)
    assert controller._api_data_seen_count == 1
    assert len(controller._price_history) == 1
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller.price_snapshot["cached_fields_only"] is True

    controller._invalidate_market_data_freshness("test invalidation")
    controller._record_price_snapshot(first, None)
    assert controller._api_data_seen_count == 1
    assert controller.price_snapshot["api_data_invalidated"] is True
    assert controller.price_snapshot["strategy_price_usable"] is False

    second = _tracked_snapshot(2, event_age=0.0)
    controller._record_price_snapshot(second, None)
    assert controller._api_data_seen_count == 2
    assert len(controller._price_history) == 2
    assert controller.price_snapshot["api_data_invalidated"] is False
    assert controller.price_snapshot["strategy_price_usable"] is True
    # An unchanged selected price is still a legitimate fresh feed event. It
    # advances freshness/ATR collection without pretending the value changed.
    assert controller._api_data_change_count == 1


def test_worker_tick_pauses_price_and_order_polling_during_upstream_outage(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ConnectivityAdapter(upstream_connected=False)
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True

    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-1|BUY_TRAIL"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._tick()

    assert adapter.process_calls == 1
    assert adapter.price_calls == 0
    assert adapter.poll_calls == 0
    assert controller.connected is True
    assert controller._api_data_invalidated is True
    status = controller._trading_status_snapshot()
    assert any(item["code"] == "upstream_disconnected" for item in status["blockers"])
    assert "IBKR link lost" in status["summary"]


def test_new_buy_and_sell_orders_are_blocked_while_upstream_is_unavailable(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ConnectivityAdapter(upstream_connected=False)
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller.strategy = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )

    buy_cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)
    buy_action = StrategyAction(
        "PLACE_BUY_MARKET",
        {"quantity": 10, "order_ref": "IBKRBOT|AAPL|CYCLE-1|BUY_MKT"},
    )
    controller.active_cycle = buy_cycle
    controller._place_market_order(buy_cycle, buy_action, "BUY")
    assert adapter.market_order_calls == 0
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert "IBKR server connectivity is not confirmed" in str(controller.active_cycle.error_message)

    sell_cycle = StrategyEngine.start_cycle(controller.strategy, 2, "", 100.0, 0.0)
    sell_cycle.stage = Stage.WAIT_RISE_TRIGGER
    sell_cycle.buy_filled_qty = 10
    sell_cycle.avg_buy_price = 100.0
    sell_action = StrategyAction(
        "PLACE_SELL_MARKET",
        {"quantity": 10, "order_ref": "IBKRBOT|AAPL|CYCLE-2|SELL_MKT"},
    )
    controller.active_cycle = sell_cycle
    controller._place_market_order(sell_cycle, sell_action, "SELL")
    assert adapter.market_order_calls == 0
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert "IBKR server connectivity is not confirmed" in str(controller.active_cycle.error_message)


def test_restored_connectivity_blocks_orders_until_reconciliation_finishes(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ConnectivityAdapter(upstream_connected=True)
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True

    controller._handle_connectivity_broker_event(
        {
            "event_type": "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED",
            "local_connected": True,
            "upstream_connected": True,
            "upstream_state": "restored_data_maintained",
            "message": "Connectivity restored - data maintained.",
            "error_code": 1102,
            "created_at": utc_now_iso(),
        }
    )

    assert controller._upstream_recovery_pending is True
    assert controller._api_data_invalidated is True
    assert "post-reconnect broker reconciliation" in controller._order_submission_connectivity_message("BUY")


def test_live_adapter_fails_closed_if_pending_ticker_events_are_unavailable():
    adapter = IbAsyncTwsAdapter()
    fake_ib = _FakeIb()
    del fake_ib.pendingTickersEvent
    adapter.ib = fake_ib
    adapter._register_broker_event_handlers()
    adapter._set_upstream_state(
        connected=True,
        state="connected_waiting_for_market_data",
        message="Waiting for market data.",
        awaiting_fresh_market_data=True,
    )
    raw = SimpleNamespace(conId=123, exchange="SMART", primaryExchange="NASDAQ")
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=raw, primary_exchange="NASDAQ")
    ticker = adapter._request_ticker(contract, "")

    snapshot = adapter._snapshot_from_ticker(ticker, contract)

    assert snapshot.price == 100.0
    assert snapshot.market_data_event_tracking is True
    assert snapshot.market_data_event_tracking_available is False
    assert snapshot.api_data_received is False
    assert snapshot.market_data_update_sequence == 0
    assert "event tracking is unavailable" in snapshot.status
    assert adapter._snapshot_has_subscription_data(snapshot) is False


def test_trading_status_reports_stale_streaming_data_for_waiting_sell(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectivityAdapter(upstream_connected=True)
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller._api_data_invalidated = False
    controller.price_snapshot = {
        "price": 101.0,
        "api_data_state": "stale",
        "api_data_age_seconds": 12.0,
        "api_data_invalidated": False,
        "market_data_event_tracking": True,
    }

    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    controller.active_cycle = cycle

    status = controller._trading_status_snapshot()

    assert any(item["code"] == "stale_data" and item["side"] == "SELL" for item in status["blockers"])
    assert status["summary"].startswith("SELL blocked")


def test_submission_boundary_pumps_a_late_connectivity_event_before_place_order(tmp_path, monkeypatch):
    class _LateOutageAdapter(_ConnectivityAdapter):
        def process_events(self, timeout: float = 0.0) -> None:
            super().process_events(timeout)
            self.status = BrokerConnectivityStatus(
                local_connected=True,
                upstream_connected=False,
                state="upstream_disconnected",
                message="Connectivity between IB Gateway and IBKR servers is lost.",
                error_code=1100,
                market_data_event_tracking=True,
            )

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _LateOutageAdapter(upstream_connected=True)
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller.strategy = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)
    action = StrategyAction(
        "PLACE_BUY_MARKET",
        {"quantity": 10, "order_ref": "IBKRBOT|AAPL|CYCLE-1|BUY_MKT"},
    )
    controller.active_cycle = cycle

    controller._place_market_order(cycle, action, "BUY")

    assert adapter.process_calls >= 1
    assert adapter.market_order_calls == 0
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert "IBKR server connectivity is not confirmed" in str(controller.active_cycle.error_message)


def test_initial_local_connect_skips_broker_recovery_when_upstream_is_down(tmp_path, monkeypatch):
    class _ConnectOutageAdapter(_ConnectivityAdapter):
        def connect(self, host: str, port: int, client_id: int, market_data_type: int) -> None:
            return None

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectOutageAdapter(upstream_connected=False)
    recovery_calls = []
    controller._recover_after_connect = lambda: recovery_calls.append(True)

    connected_for_trading = controller._connect(ConnectionSettings())

    assert connected_for_trading is False
    assert controller.connected is True
    assert controller._broker_connectivity["upstream_connected"] is False
    assert recovery_calls == []
    assert "Connected locally" in controller.status
    assert "Trading and broker recovery are paused" in controller.status


def test_live_adapter_connect_does_not_refresh_open_orders_after_handshake_1100(monkeypatch):
    class _HandshakeOutageIb(_FakeIb):
        def __init__(self) -> None:
            super().__init__()
            self.connected = False
            self.open_order_requests = 0

        def connect(self, host: str, port: int, clientId: int, timeout: float) -> None:
            self.connected = True
            self.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)

        def reqOpenOrders(self) -> None:
            self.open_order_requests += 1

        def openTrades(self):
            return []

    adapter = IbAsyncTwsAdapter()
    fake_ib = _HandshakeOutageIb()
    adapter.ib = fake_ib
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (object, object, object))

    adapter.connect("127.0.0.1", 4001, 11, 1)

    assert adapter.is_connected() is True
    assert adapter.connectivity_status().upstream_connected is False
    assert fake_ib.open_order_requests == 0


def test_local_auto_reconnect_skips_broker_recovery_when_upstream_is_down(tmp_path, monkeypatch):
    class _ReconnectOutageAdapter(_ConnectivityAdapter):
        def connect(self, host: str, port: int, client_id: int, market_data_type: int) -> None:
            return None

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ReconnectOutageAdapter(upstream_connected=False)
    controller.connection = ConnectionSettings()
    controller.connected = False
    controller._auto_reconnect_enabled = True
    controller._last_reconnect_attempt_monotonic = 0.0
    recovery_calls = []
    controller._recover_after_connect = lambda: recovery_calls.append(True)

    assert controller._attempt_reconnect_if_due() is True

    assert controller.connected is True
    assert controller._broker_connectivity["upstream_connected"] is False
    assert recovery_calls == []
    assert "Reconnected locally" in controller.status
    assert "Trading and broker recovery are paused" in controller.status


def test_start_command_does_not_continue_after_local_only_connect(tmp_path, monkeypatch):
    class _ConnectOutageAdapter(_ConnectivityAdapter):
        def connect(self, host: str, port: int, client_id: int, market_data_type: int) -> None:
            return None

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectOutageAdapter(upstream_connected=False)
    start_calls = []
    controller._start_strategy = lambda settings: start_calls.append(settings)

    controller._handle_command(
        "START_STRATEGY",
        {
            "connection": ConnectionSettings(),
            "strategy": StrategySettings(ticker="AAPL"),
        },
    )

    assert controller.connected is True
    assert start_calls == []
    assert "Trading and broker recovery are paused" in controller.status


def test_contract_search_and_recovery_refresh_are_blocked_during_upstream_outage(tmp_path, monkeypatch):
    class _ReadCountingAdapter(_ConnectivityAdapter):
        def __init__(self) -> None:
            super().__init__(upstream_connected=False)
            self.search_calls = 0
            self.open_order_calls = 0

        def search_stock_contracts(self, query: str):
            self.search_calls += 1
            raise AssertionError("contract search must not run while upstream is unavailable")

        def open_app_orders(self):
            self.open_order_calls += 1
            raise AssertionError("broker refresh must not run while upstream is unavailable")

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ReadCountingAdapter()
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True

    controller._search_contracts(ConnectionSettings(), "AAPL")
    assert adapter.search_calls == 0
    assert "IBKR server connectivity is not confirmed" in controller.status

    controller._refresh_broker_state_for_recovery()
    assert adapter.open_order_calls == 0
    assert controller._last_recovery_probe["upstream_connected"] is False


def test_stop_side_broker_actions_are_blocked_during_upstream_outage(tmp_path, monkeypatch):
    class _SideEffectCountingAdapter(_ConnectivityAdapter):
        def __init__(self) -> None:
            super().__init__(upstream_connected=False)
            self.cancel_calls = 0

        def cancel_order(self, order_ref: str, order_id=None) -> None:
            self.cancel_calls += 1
            raise AssertionError("cancel_order must not run while upstream is unavailable")

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _SideEffectCountingAdapter()
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True

    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|CYCLE-1|PROTECTIVE_SELL"
    cycle.protective_sell_status = "Submitted"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._request_market_close_for_app_position(cycle)

    assert adapter.cancel_calls == 0
    assert adapter.market_order_calls == 0
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.close_position_market_requested is False
    assert "IBKR server connectivity is not confirmed" in str(controller.active_cycle.error_message)

def test_initial_connect_consumes_handshake_restore_reconciliation_once(tmp_path, monkeypatch):
    class _HandshakeRestoredAdapter(_ConnectivityAdapter):
        def __init__(self) -> None:
            super().__init__(upstream_connected=True)
            self.events = [
                {
                    "event_type": "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED",
                    "error_code": 1102,
                    "message": "Connectivity restored; data maintained.",
                    "upstream_state": "restored_data_maintained",
                    "market_data_resubscribe_required": False,
                    "created_at": utc_now_iso(),
                }
            ]

        def connect(self, host: str, port: int, client_id: int, market_data_type: int) -> None:
            return None

        def drain_broker_events(self):
            events = list(self.events)
            self.events.clear()
            return events

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _HandshakeRestoredAdapter()
    recovery_calls = []
    controller._recover_after_connect = lambda: recovery_calls.append(True)

    assert controller._connect(ConnectionSettings()) is True

    assert recovery_calls == [True]
    assert controller._upstream_recovery_pending is False
    assert "broker state reconciled" in controller.status


def test_command_bar_requires_local_and_upstream_broker_readiness():
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("    def _update_command_bar_states")
    end = source.index("    def ", start + 8)
    method = source[start:end]

    assert "broker_ready = bool(" in method
    assert "and upstream_connected is True" in method
    assert "and not upstream_recovery_pending" in method
    assert 'self.command_steps["ticker"].set_state("Blocked", False, detail)' in method
    assert 'self.command_steps["confirm"].set_state("Blocked", False, detail)' in method
    assert 'self.command_steps["start"].set_state("Blocked", False, detail)' in method



def test_search_and_confirmation_wait_for_post_restore_reconciliation(tmp_path, monkeypatch):
    class _ReadCountingAdapter(_ConnectivityAdapter):
        def __init__(self) -> None:
            super().__init__(upstream_connected=True)
            self.search_calls = 0

        def search_stock_contracts(self, query: str):
            self.search_calls += 1
            return []

    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ReadCountingAdapter()
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller._upstream_recovery_pending = True
    confirm_calls = []
    controller._confirm_ticker_price = lambda settings: confirm_calls.append(settings)

    controller._search_contracts(ConnectionSettings(), "AAPL")
    controller._handle_command(
        "CONFIRM_TICKER_PRICE",
        {
            "connection": ConnectionSettings(),
            "strategy": StrategySettings(ticker="AAPL"),
        },
    )

    assert adapter.search_calls == 0
    assert confirm_calls == []
    assert "post-reconnect broker reconciliation" in controller.status


def test_10197_competing_session_invalidates_cached_quotes_until_a_new_event():
    adapter, fake_ib, contract = _live_adapter()
    ticker = adapter._request_ticker(contract, "")
    fake_ib.pendingTickersEvent.emit([ticker])
    original = adapter._snapshot_from_ticker(ticker, contract)
    assert original.market_data_update_sequence and original.market_data_update_sequence > 0

    fake_ib.errorEvent.emit(
        7,
        10197,
        "No market data during competing live session",
        contract.raw,
    )

    blocked = adapter.connectivity_status()
    assert blocked.local_connected is True
    assert blocked.upstream_connected is True
    assert blocked.trading_ready is True
    assert blocked.state == "market_data_competing_session"
    assert blocked.awaiting_fresh_market_data is True
    assert blocked.error_code == 10197

    cached = adapter._snapshot_from_ticker(ticker, contract)
    assert cached.price == 100.0
    assert cached.api_data_received is False
    assert cached.market_data_update_sequence == 0
    assert cached.upstream_connected is True

    events = adapter.drain_broker_events()
    assert events[-1]["event_type"] == "IBKR_MARKET_DATA_COMPETING_SESSION"
    assert events[-1]["error_code"] == 10197

    fake_ib.pendingTickersEvent.emit([ticker])
    recovered = adapter.connectivity_status()
    assert recovered.state == "connected"
    assert recovered.awaiting_fresh_market_data is False
    fresh = adapter._snapshot_from_ticker(ticker, contract)
    assert fresh.api_data_received is True
    assert fresh.market_data_update_sequence > original.market_data_update_sequence


def test_market_data_farm_messages_invalidate_and_wait_for_a_fresh_event():
    adapter, fake_ib, contract = _live_adapter()
    ticker = adapter._request_ticker(contract, "")
    fake_ib.pendingTickersEvent.emit([ticker])

    fake_ib.errorEvent.emit(-1, 2103, "A market data farm is disconnected", None)
    disconnected = adapter.connectivity_status()
    assert disconnected.upstream_connected is True
    assert disconnected.state == "market_data_farm_disconnected"
    assert disconnected.awaiting_fresh_market_data is True
    assert adapter._snapshot_from_ticker(ticker, contract).market_data_update_sequence == 0

    fake_ib.errorEvent.emit(-1, 2104, "Market data farm connection is OK", None)
    restored = adapter.connectivity_status()
    assert restored.upstream_connected is True
    assert restored.state == "market_data_farm_restored"
    assert restored.awaiting_fresh_market_data is True

    event_types = [event["event_type"] for event in adapter.drain_broker_events()]
    assert event_types[-2:] == [
        "IBKR_MARKET_DATA_FARM_DISCONNECTED",
        "IBKR_MARKET_DATA_FARM_RESTORED",
    ]

    fake_ib.pendingTickersEvent.emit([ticker])
    assert adapter.connectivity_status().state == "connected"
    assert adapter.connectivity_status().awaiting_fresh_market_data is False


def test_emit_snapshot_reclassifies_a_frozen_green_quote_as_stale(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectivityAdapter(upstream_connected=True)
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller.strategy = StrategySettings(
        ticker="AAPL",
        max_selected_price_age_seconds=3.0,
        stale_data_guard_enabled=True,
        atr_adaptive_enabled=False,
    )
    controller.price_snapshot = {
        "price": 100.0,
        "fields": {"last": 100.0, "bid": 99.9, "ask": 100.1},
        "api_data_state": "receiving",
        "api_data_received_in_latest_read": True,
        "market_data_event_tracking": True,
        "strategy_price_usable": True,
    }
    controller._api_data_seen_count = 1
    controller._api_data_invalidated = False
    controller._api_data_invalidated_reason = ""
    controller._api_last_data_monotonic = time.monotonic() - 12.0
    controller._api_last_change_monotonic = controller._api_last_data_monotonic
    controller._last_price_poll_monotonic = time.monotonic() - 12.0

    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)
    controller.active_cycle = cycle
    controller.emit_snapshot(force=True)

    emitted = controller.signals.snapshot_updated.emissions[-1][0][0]
    price_snapshot = emitted["price_snapshot"]
    assert price_snapshot["api_data_state"] == "stale"
    assert price_snapshot["strategy_price_usable"] is False
    assert float(price_snapshot["api_data_age_seconds"]) >= 11.0
    assert any(item["code"] == "stale_data" for item in emitted["trading_status"]["blockers"])


def test_controller_marks_competing_session_as_market_data_only_block(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectivityAdapter(upstream_connected=True)
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller.price_snapshot = {
        "price": 100.0,
        "fields": {"last": 100.0},
        "api_data_state": "receiving",
        "strategy_price_usable": True,
    }

    controller._handle_connectivity_broker_event(
        {
            "event_type": "IBKR_MARKET_DATA_COMPETING_SESSION",
            "local_connected": True,
            "upstream_connected": True,
            "upstream_state": "market_data_competing_session",
            "message": "No market data during competing live session",
            "error_code": 10197,
            "created_at": utc_now_iso(),
        }
    )

    assert controller._broker_connectivity["upstream_connected"] is True
    assert controller._broker_connectivity["state"] == "market_data_competing_session"
    assert controller._broker_connectivity["awaiting_fresh_market_data"] is True
    assert controller._api_data_invalidated is True
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller.price_snapshot["api_data_state"] == "invalidated"
    assert "Trading decisions that require a quote are paused" in controller.status


def test_adapter_waiting_flag_alone_invalidates_emitted_cached_quote(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    adapter = _ConnectivityAdapter(upstream_connected=True)
    adapter.status = BrokerConnectivityStatus(
        local_connected=True,
        upstream_connected=True,
        state="market_data_competing_session",
        message="Waiting for a fresh quote after a competing session.",
        error_code=10197,
        awaiting_fresh_market_data=True,
        market_data_event_tracking=True,
    )
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller._api_data_invalidated = False
    controller._api_data_invalidated_reason = ""
    controller._api_last_data_monotonic = time.monotonic() - 1.0
    controller.price_snapshot = {
        "price": 100.0,
        "fields": {"last": 100.0},
        "api_data_state": "receiving",
        "api_data_received_in_latest_read": True,
        "market_data_event_tracking": True,
        "strategy_price_usable": True,
    }
    controller.strategy = StrategySettings(ticker="AAPL", atr_adaptive_enabled=False)
    controller.active_cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)

    controller.emit_snapshot(force=True)

    emitted = controller.signals.snapshot_updated.emissions[-1][0][0]
    price_snapshot = emitted["price_snapshot"]
    assert price_snapshot["api_data_state"] == "invalidated"
    assert price_snapshot["api_data_invalidated"] is True
    assert price_snapshot["strategy_price_usable"] is False
    assert any(
        item["code"] == "fresh_market_data_pending"
        for item in emitted["trading_status"]["blockers"]
    )


def test_market_data_messages_do_not_upgrade_a_known_full_upstream_outage():
    adapter, fake_ib, contract = _live_adapter()
    ticker = adapter._request_ticker(contract, "")
    fake_ib.pendingTickersEvent.emit([ticker])

    fake_ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    assert adapter.connectivity_status().upstream_connected is False

    for error_code, message, expected_state in [
        (2103, "A market data farm is disconnected", "market_data_farm_disconnected"),
        (2104, "Market data farm connection is OK", "market_data_farm_restored"),
        (10197, "No market data during competing live session", "market_data_competing_session"),
    ]:
        fake_ib.errorEvent.emit(-1, error_code, message, None)
        status = adapter.connectivity_status()
        assert status.upstream_connected is False
        assert status.trading_ready is False
        assert status.state == expected_state
        assert status.awaiting_fresh_market_data is True

    # A ticker callback alone cannot claim that a full 1100 outage recovered.
    fake_ib.pendingTickersEvent.emit([ticker])
    assert adapter.connectivity_status().upstream_connected is False
    assert adapter.connectivity_status().awaiting_fresh_market_data is True

    events = adapter.drain_broker_events()
    market_data_events = [
        event
        for event in events
        if event["event_type"].startswith("IBKR_MARKET_DATA_")
    ]
    assert len(market_data_events) == 3
    assert all(event["upstream_connected"] is False for event in market_data_events)


def test_controller_market_data_event_preserves_stronger_upstream_outage(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.adapter = _ConnectivityAdapter(upstream_connected=False)
    controller.connected = True
    controller._broker_connectivity_initialized = True
    controller._broker_connectivity = {
        "local_connected": True,
        "upstream_connected": False,
        "state": "upstream_disconnected",
        "message": "IBKR server link is down",
        "error_code": 1100,
        "awaiting_fresh_market_data": True,
        "market_data_event_tracking": True,
        "trading_ready": False,
    }
    controller.price_snapshot = {
        "price": 100.0,
        "fields": {"last": 100.0},
        "api_data_state": "upstream_disconnected",
        "strategy_price_usable": False,
    }

    controller._handle_connectivity_broker_event(
        {
            "event_type": "IBKR_MARKET_DATA_FARM_RESTORED",
            "local_connected": True,
            "upstream_connected": False,
            "upstream_state": "market_data_farm_restored",
            "message": "Market data farm connection is OK",
            "error_code": 2104,
            "created_at": utc_now_iso(),
        }
    )

    assert controller._broker_connectivity["upstream_connected"] is False
    assert controller._broker_connectivity["trading_ready"] is False
    assert controller._broker_connectivity["awaiting_fresh_market_data"] is True
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert "remains disconnected from IBKR servers" in controller.status
