from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ib_adapter import BrokerAdapterError, IbAsyncTwsAdapter, MarketPriceSnapshot, OrderHandle, QualifiedContract, RthStatus
from app.market_data_capture import MarketDataCaptureManager
from app.models import ConnectionSettings, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyAction, StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class _BaseSubmitAdapter:
    def __init__(self, storage: BotStorage | None = None, *, position: float | None = 0.0):
        self.storage = storage
        self.position = position
        self.position_calls = 0
        self.place_calls = 0
        self.last_place_kwargs = None

    def is_connected(self):
        return True

    def set_market_data_type(self, market_data_type):
        self.market_data_type = market_data_type

    def price_snapshot(self, contract, timeout=1.0):
        return MarketPriceSnapshot(
            price=100.0,
            source="test",
            requested_market_data_type=getattr(self, "market_data_type", 1),
            subscription_market_data_type=1,
            fields={"last": 100.0},
            timestamp=utc_now_iso(),
            status="OK",
        )

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker, con_id=con_id or 123, raw=object(), primary_exchange=primary_exchange)

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def managed_accounts(self):
        return ["SIM"]

    def position_size(self, contract, account=""):
        self.position_calls += 1
        return self.position

    def what_if_trailing_stop(self, **kwargs):
        return {"ok": True, "message": "test pass"}

    def place_trailing_stop(self, **kwargs):
        self.place_calls += 1
        self.last_place_kwargs = dict(kwargs)
        if self.storage is not None:
            with self.storage.connect() as con:
                row = con.execute("SELECT status FROM orders WHERE order_ref=? ORDER BY id DESC LIMIT 1", (kwargs["order_ref"],)).fetchone()
            assert row is not None
            assert row["status"] == "INTENT_CREATED"
        return OrderHandle(kwargs["order_ref"], 101, 201, "Submitted", {"accepted": True})


class _FailAfterIntentAdapter(_BaseSubmitAdapter):
    def place_trailing_stop(self, **kwargs):
        self.place_calls += 1
        if self.storage is not None:
            with self.storage.connect() as con:
                row = con.execute("SELECT status FROM orders WHERE order_ref=? ORDER BY id DESC LIMIT 1", (kwargs["order_ref"],)).fetchone()
            assert row is not None
            assert row["status"] == "INTENT_CREATED"
        raise BrokerAdapterError("synthetic submit failure")


def _controller(tmp_path: Path, monkeypatch, adapter):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    controller.adapter = adapter
    controller.connected = True
    controller.connection = ConnectionSettings(account="SIM", trading_mode="live")
    controller.strategy = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    controller.contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")
    return controller


def _buy_action(cycle):
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    return StrategyAction(
        "PLACE_BUY_TRAIL",
        {
            "ticker": "AAPL",
            "quantity": 5,
            "order_type": "TRAIL",
            "trailing_percent": 1.0,
            "initial_stop_price": 99.0,
            "order_ref": cycle.buy_order_ref,
        },
    )


def test_order_intent_is_durable_before_broker_submit_and_updated_atomically(tmp_path, monkeypatch):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    adapter = _BaseSubmitAdapter(storage)
    controller = _controller(tmp_path, monkeypatch, adapter)
    controller.storage = storage
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    with controller.storage.connect() as con:
        row = con.execute("SELECT status, order_id, perm_id FROM orders WHERE order_ref=?", (cycle.buy_order_ref,)).fetchone()
        cycle_row = con.execute("SELECT buy_order_id, buy_perm_id, buy_status FROM cycles WHERE id=?", (cycle.id,)).fetchone()
    assert row["status"] == "Submitted"
    assert row["order_id"] == 101
    assert row["perm_id"] == 201
    assert cycle_row["buy_order_id"] == 101
    assert cycle_row["buy_perm_id"] == 201
    assert cycle_row["buy_status"] == "Submitted"


def test_order_intent_is_marked_failed_when_broker_submit_raises(tmp_path, monkeypatch):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    adapter = _FailAfterIntentAdapter(storage)
    controller = _controller(tmp_path, monkeypatch, adapter)
    controller.storage = storage
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    with pytest.raises(BrokerAdapterError):
        controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    with controller.storage.connect() as con:
        row = con.execute("SELECT status, raw_json FROM orders WHERE order_ref=?", (cycle.buy_order_ref,)).fetchone()
    assert row["status"] == "SUBMIT_FAILED"
    assert "synthetic submit failure" in row["raw_json"]


def test_live_strategy_start_allows_blank_connection_account(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter()
    controller = _controller(tmp_path, monkeypatch, adapter)
    controller.connection = ConnectionSettings(account="", trading_mode="live", market_data_type=1)
    settings = StrategySettings(
        ticker="AAPL",
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )

    controller._start_strategy(settings)

    assert controller.active_cycle is not None
    assert controller.active_cycle.account == ""
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP


def test_explicit_live_account_override_is_still_validated(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter()
    controller = _controller(tmp_path, monkeypatch, adapter)
    controller.connection = ConnectionSettings(account="DU_NOT_MANAGED", trading_mode="live")
    settings = StrategySettings(
        ticker="AAPL",
        what_if_check_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "DU_NOT_MANAGED", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    assert adapter.place_calls == 0
    assert controller.active_cycle is not None
    assert "not in the IBKR managed-account list" in (controller.active_cycle.error_message or "")


def test_live_buy_allows_blank_account_and_delegates_routing_to_ibkr(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter()
    controller = _controller(tmp_path, monkeypatch, adapter)
    controller.connection = ConnectionSettings(account="", trading_mode="live")
    settings = StrategySettings(
        ticker="AAPL",
        what_if_check_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    assert adapter.place_calls == 1
    assert adapter.position_calls == 0
    assert adapter.last_place_kwargs is not None
    assert adapter.last_place_kwargs["account"] == ""
    assert controller.active_cycle is not None
    assert controller.active_cycle.buy_status == "Submitted"


def test_buy_preflight_ignores_external_broker_position(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter(position=890.0)
    controller = _controller(tmp_path, monkeypatch, adapter)
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    assert adapter.place_calls == 1
    assert adapter.position_calls == 0
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.active_cycle.buy_status == "Submitted"


def test_buy_preflight_blocks_unsold_app_owned_position(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter(position=890.0)
    controller = _controller(tmp_path, monkeypatch, adapter)
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)

    prior = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    prior.stage = Stage.STOPPED
    prior.buy_filled_qty = 3
    prior.avg_buy_price = 99.0
    controller.storage.upsert_cycle(prior)

    cycle = StrategyEngine.start_cycle(settings, 2, "SIM", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    assert adapter.place_calls == 0
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert "3 unsold app-owned AAPL shares" in (controller.active_cycle.error_message or "")
    assert "Manual or externally acquired broker holdings are not counted" in (controller.active_cycle.error_message or "")


def test_manually_resolved_app_position_does_not_block_new_buy(tmp_path, monkeypatch):
    adapter = _BaseSubmitAdapter(position=890.0)
    controller = _controller(tmp_path, monkeypatch, adapter)
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=False, block_delayed_data_in_live=False, stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)

    prior = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    prior.stage = Stage.STOPPED
    prior.buy_filled_qty = 3
    prior.avg_buy_price = 99.0
    controller.storage.upsert_cycle(prior)
    controller.storage.add_decision_event(
        event_type="MANUALLY_HANDLED",
        message="Operator confirmed that the prior app position was resolved outside the app.",
        cycle=prior,
        decision_result="resolved",
    )

    cycle = StrategyEngine.start_cycle(settings, 2, "SIM", 100.0, 0.0)
    cycle.con_id = 123
    controller.storage.upsert_cycle(cycle)

    controller._place_trailing_order(cycle, _buy_action(cycle), "BUY")

    assert adapter.place_calls == 1
    assert controller.active_cycle is not None
    assert controller.active_cycle.buy_status == "Submitted"


def test_broker_callback_events_are_persisted_for_active_cycle(tmp_path, monkeypatch):
    class EventAdapter(_BaseSubmitAdapter):
        def drain_broker_events(self):
            return [
                {
                    "event_type": "ORDER_STATUS",
                    "order_ref": "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL",
                    "order_id": 1,
                    "perm_id": 2,
                    "status": "Submitted",
                    "ticker": "AAPL",
                }
            ]

    adapter = EventAdapter()
    controller = _controller(tmp_path, monkeypatch, adapter)
    settings = StrategySettings(ticker="AAPL", stale_data_guard_enabled=False, volatility_filter_enabled=False, session_timing_guard_enabled=False, atr_adaptive_enabled=False, atr_block_new_buy_until_ready=False)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._drain_broker_events()

    rows = controller.storage.recent_broker_events()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "ORDER_STATUS"
    assert rows[0]["cycle_id"] == cycle.id
    assert rows[0]["order_ref"] == cycle.buy_order_ref


def test_poll_order_uses_cached_trade_without_full_open_order_refresh():
    adapter = IbAsyncTwsAdapter()
    order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    trade = SimpleNamespace(
        order=SimpleNamespace(orderRef=order_ref, orderId=1, permId=2, action="BUY", orderType="TRAIL", totalQuantity=5),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=5, avgFillPrice=0.0, permId=2),
        fills=[],
    )

    class FakeIB:
        def __init__(self):
            self.req_open_orders = 0
        def isConnected(self):
            return True
        def reqOpenOrders(self):
            self.req_open_orders += 1
        def sleep(self, seconds):
            pass
        def trades(self):
            return []
        def openTrades(self):
            return []

    fake = FakeIB()
    adapter.ib = fake
    adapter._trades_by_ref[order_ref] = trade

    state = adapter.poll_order(order_ref)

    assert state is not None
    assert state.status == "Submitted"
    assert fake.req_open_orders == 0


def test_open_app_orders_forces_broker_refresh():
    adapter = IbAsyncTwsAdapter()
    order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    trade = SimpleNamespace(
        order=SimpleNamespace(orderRef=order_ref, orderId=1, permId=2, action="BUY", orderType="TRAIL", totalQuantity=5),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=5, avgFillPrice=0.0, permId=2),
        fills=[],
    )

    class FakeIB:
        def __init__(self):
            self.req_open_orders = 0
        def isConnected(self):
            return True
        def reqOpenOrders(self):
            self.req_open_orders += 1
        def sleep(self, seconds):
            pass
        def trades(self):
            return []
        def openTrades(self):
            return [trade]

    fake = FakeIB()
    adapter.ib = fake

    states = adapter.open_app_orders()

    assert len(states) == 1
    assert fake.req_open_orders == 1


def test_history_summary_cache_invalidates_when_completed_cycles_change(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    settings = StrategySettings(ticker="AAPL")
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.avg_sell_price = 101.0
    cycle.net_pnl = 10.0
    cycle.gross_pnl = 10.0
    cycle.updated_at = "2026-01-01T12:00:00+00:00"
    storage.upsert_cycle(cycle)

    first = storage.history_summary("AAPL")
    second = storage.history_summary("AAPL")
    assert first == second
    assert first["cycles"] == 1

    cycle2 = StrategyEngine.start_cycle(settings, 2, "SIM", 100.0, 0.0)
    cycle2.stage = Stage.CYCLE_COMPLETE
    cycle2.buy_filled_qty = 10
    cycle2.avg_buy_price = 100.0
    cycle2.avg_sell_price = 99.0
    cycle2.net_pnl = -10.0
    cycle2.gross_pnl = -10.0
    cycle2.updated_at = "2026-01-01T12:01:00+00:00"
    storage.upsert_cycle(cycle2)

    updated = storage.history_summary("AAPL")
    assert updated["cycles"] == 2
    assert updated["total_net_pnl"] == 0.0


def test_sqlite_composite_indexes_exist_for_snapshot_and_history_queries(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    with storage.connect() as con:
        names = {row["name"] for row in con.execute("PRAGMA index_list(cycles)").fetchall()}
    assert "idx_cycles_stage_ticker_updated" in names
    assert "idx_cycles_stage_sell_updated" in names
    assert "idx_cycles_stage_ticker_sell_updated" in names


def test_market_capture_async_writer_starts_lazily_to_avoid_idle_threads(tmp_path):
    manager = MarketDataCaptureManager(tmp_path, pre_window_seconds=1.0, post_window_seconds=1.0, async_writes=True)
    assert manager._writer_thread is None
    manager.shutdown(wait=True, timeout=1.0)
    assert manager._writer_thread is None


def test_market_capture_async_writer_moves_zip_work_off_caller_path(tmp_path):
    manager = MarketDataCaptureManager(tmp_path, pre_window_seconds=1.0, post_window_seconds=0.0, async_writes=True)
    manager.record_snapshot({"price": 100.0, "source": "test"}, monotonic_ts=1.0, wall_time_utc="2026-01-01T00:00:00+00:00")
    event_id = manager.start_capture(
        event_type="BUY_FILL",
        event_monotonic=1.0,
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        order_ref="IBKRBOT|AAPL|TEST",
        payload={"filled": 1},
    )
    assert event_id
    assert manager.finalize_ready(1.0) == []

    manager.shutdown(wait=True, timeout=5.0)

    completed = manager.completed_files
    assert len(completed) == 1
    assert completed[0].exists()
