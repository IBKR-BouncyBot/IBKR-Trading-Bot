"""v3.1.2 fill reconciliation, isolation, timestamps, and Stage-3 close tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import IbAsyncTwsAdapter, PolledOrderState
from app.models import Stage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


def _set_minutes_to_close(controller: Any, minutes: float, *, is_open: bool = True) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    controller._latest_rth_status = {
        "is_open": bool(is_open),
        "session_open": (now - dt.timedelta(hours=1)).isoformat(),
        "session_close": (now + dt.timedelta(minutes=minutes)).isoformat(),
        "time_zone": "UTC",
        "source": "v312_test",
        "message": "v3.1.2 deterministic session",
    }
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_since_open": 60.0,
        "minutes_to_close": float(minutes),
        "session_open_display": "14:30 UTC",
        "session_close_display": "21:00 UTC",
        "local_time": "test",
        "source": "v312_test",
        "message": "v3.1.2 deterministic session",
    }


def _controller(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, ticker: str = "AAPL") -> tuple[Any, DeterministicBrokerAdapter]:
    controller_module = _install_qt_stub(monkeypatch)
    broker = DeterministicBrokerAdapter(ticker=ticker)
    settings = permissive_strategy(ticker=ticker)
    controller = make_controller(controller_module, tmp_path / "bot_state.sqlite", broker, settings)
    controller.connection.account = "SIM"
    controller.storage.backup_database = lambda *args, **kwargs: None
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    return controller, broker


def _buy_cycle(controller: Any, broker: DeterministicBrokerAdapter, *, quantity: int = 10) -> Any:
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = int(quantity)
    cycle.buy_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "BUY_TRAIL")
    handle = broker.place_trailing_stop(
        contract=broker.contract,
        action="BUY",
        quantity=quantity,
        trailing_percent=1.0,
        initial_stop_price=101.0,
        order_ref=cycle.buy_order_ref,
        tif="GTC",
        account="SIM",
        outside_rth=False,
    )
    cycle.buy_order_id = handle.order_id
    cycle.buy_perm_id = handle.perm_id
    cycle.buy_status = handle.status
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller.storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="TRAIL",
        order_id=handle.order_id,
        perm_id=handle.perm_id,
        order_ref=cycle.buy_order_ref,
        quantity=quantity,
        trailing_percent=1.0,
        initial_stop_price=101.0,
        status=handle.status,
    )
    return cycle


def _callback_execution(cycle: Any, execution_id: str, shares: int, price: float) -> dict[str, Any]:
    return {
        "event_type": "EXEC_DETAILS",
        "created_at": "2026-07-23T14:00:00+00:00",
        "executed_at": "2026-07-23T13:59:59+00:00",
        "order_ref": cycle.buy_order_ref,
        "order_id": cycle.buy_order_id,
        "perm_id": cycle.buy_perm_id,
        "execution_id": execution_id,
        "side": "BOT",
        "shares": shares,
        "price": price,
        "currency": "USD",
        "ticker": cycle.ticker,
    }


def _callback_commission(cycle: Any, execution_id: str, commission: float) -> dict[str, Any]:
    return {
        "event_type": "COMMISSION_REPORT",
        "created_at": "2026-07-23T14:00:01+00:00",
        "order_ref": cycle.buy_order_ref,
        "order_id": cycle.buy_order_id,
        "perm_id": cycle.buy_perm_id,
        "execution_id": execution_id,
        "commission": commission,
        "currency": "USD",
        "ticker": cycle.ticker,
    }


def _stage3_cycle(
    controller: Any,
    *,
    current_price: float,
    average_buy_price: float = 100.0,
    protective: bool = False,
) -> Any:
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", current_price, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.quantity = 10
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = float(average_buy_price)
    cycle.buy_commission = 1_000.0
    cycle.buy_status = "Filled"
    cycle.cancel_sell_and_liquidate_before_close_enabled = True
    cycle.liquidate_before_close_minutes = 5
    if protective:
        cycle.protective_sell_order_ref = make_order_ref(
            cycle.ticker,
            cycle.cycle_number,
            cycle.id,
            "PROTECTIVE_SELL_TRAIL",
        )
        handle = controller.adapter.place_trailing_stop(
            contract=controller.adapter.contract,
            action="SELL",
            quantity=10,
            trailing_percent=3.0,
            initial_stop_price=97.0,
            order_ref=cycle.protective_sell_order_ref,
            tif="GTC",
            account="SIM",
            outside_rth=False,
        )
        cycle.protective_sell_order_id = handle.order_id
        cycle.protective_sell_perm_id = handle.perm_id
        cycle.protective_sell_status = handle.status
    controller.active_cycle = cycle
    controller.price_snapshot = {"price": float(current_price)}
    controller.storage.upsert_cycle(cycle)
    _set_minutes_to_close(controller, 5.0)
    return cycle


def _market_orders(broker: DeterministicBrokerAdapter) -> list[dict[str, Any]]:
    return [item for item in broker.placed_orders if item.get("order_type") == "MKT"]


def test_partial_buy_remains_stage2_until_terminal_and_reconciles_cancel_race(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=56)

    first = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=28,
        price=185.00,
        commission=0.18,
        execution_id="BUY-PART-1",
        terminal=False,
    )
    controller._handle_buy_order_poll(cycle, first)

    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.active_cycle.buy_filled_qty == 28
    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]

    second = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=28,
        price=185.08,
        commission=0.19,
        execution_id="BUY-PART-2",
        terminal=True,
    )
    controller._handle_buy_order_poll(controller.active_cycle, second)

    settled = controller.active_cycle
    assert settled.stage == Stage.WAIT_RISE_TRIGGER
    assert settled.buy_filled_qty == 56
    assert settled.avg_buy_price == pytest.approx(185.04)
    assert settled.buy_commission == pytest.approx(0.37)
    assert settled.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {"BUY-PART-1", "BUY-PART-2"}
    assert sum(float(row["shares"]) for row in rows) == pytest.approx(56.0)



def test_terminal_cumulative_fill_is_not_double_counted_by_late_callbacks(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=10)
    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=100.2,
        commission=0.30,
        executions=[],
        raw={"source": "order status before execDetails"},
    )

    controller._handle_buy_order_poll(cycle, terminal)

    settled = controller.active_cycle
    assert settled.stage == Stage.WAIT_RISE_TRIGGER
    assert settled.buy_filled_qty == 10
    placeholder_id = controller.storage.cumulative_execution_id(str(cycle.buy_order_ref), "BUY")
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {placeholder_id}

    first_exec = _callback_execution(cycle, "LATE-1", 4, 99.0)
    first_commission = _callback_commission(cycle, "LATE-1", 0.12)
    broker.events.extend([first_exec, first_commission])
    controller._drain_broker_events()

    after_first = controller.storage.get_cycle(cycle.id)
    assert after_first is not None
    assert after_first.buy_filled_qty == 10
    assert after_first.buy_commission == pytest.approx(0.30)
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    placeholder = next(row for row in rows if row["execution_id"] == placeholder_id)
    assert float(placeholder["shares"]) == pytest.approx(6.0)
    assert sum(float(row["shares"]) for row in rows) == pytest.approx(10.0)

    second_exec = _callback_execution(cycle, "LATE-2", 6, 101.0)
    second_commission = _callback_commission(cycle, "LATE-2", 0.18)
    broker.events.extend([second_exec, second_commission, second_exec, second_commission])
    controller._drain_broker_events()

    final_cycle = controller.storage.get_cycle(cycle.id)
    assert final_cycle is not None
    assert final_cycle.buy_filled_qty == 10
    assert final_cycle.avg_buy_price == pytest.approx(100.2)
    assert final_cycle.buy_commission == pytest.approx(0.30)
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {"LATE-1", "LATE-2"}
    assert sum(float(row["shares"]) for row in rows) == pytest.approx(10.0)
    assert sum(float(row["commission"]) for row in rows) == pytest.approx(0.30)


def test_failed_partial_buy_cancel_is_retried_without_losing_fill_tracking(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=10)
    broker.fail_operations.add("cancel")
    partial = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=4,
        price=99.0,
        commission=0.12,
        execution_id="CANCEL-RETRY",
        terminal=False,
    )

    controller._handle_buy_order_poll(cycle, partial)

    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.active_cycle.buy_filled_qty == 4
    assert controller.active_cycle.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []

    broker.fail_operations.remove("cancel")
    controller._handle_buy_order_poll(controller.active_cycle, partial)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]


def test_callback_replay_and_late_commissions_are_idempotent(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=10)
    first_exec = _callback_execution(cycle, "CALLBACK-1", 4, 99.0)
    # ib_async can expose a default 0.0 commission on execDetails before the
    # explicit commissionReport is attached.  The explicit callback must win.
    first_exec["commission"] = 0.0
    first_commission = _callback_commission(cycle, "CALLBACK-1", 0.12)

    broker.events.extend([first_commission, first_exec, first_exec, first_commission])
    controller._drain_broker_events()

    partial = controller.storage.get_cycle(cycle.id)
    assert partial is not None
    assert partial.stage == Stage.BUY_TRAIL_ACTIVE
    assert partial.buy_filled_qty == 4
    assert partial.buy_commission == pytest.approx(0.12)
    assert broker.cancelled_orders == [cycle.buy_order_ref]

    second_exec = _callback_execution(cycle, "CALLBACK-2", 6, 101.0)
    second_commission = _callback_commission(cycle, "CALLBACK-2", 0.18)
    broker.events.extend([second_exec, second_commission, second_commission, second_exec])
    controller._drain_broker_events()

    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=100.2,
        commission=0.30,
        executions=[],
        raw={"source": "terminal order status"},
    )
    controller._handle_buy_order_poll(controller.active_cycle, terminal)

    settled = controller.active_cycle
    assert settled.stage == Stage.WAIT_RISE_TRIGGER
    assert settled.buy_filled_qty == 10
    assert settled.avg_buy_price == pytest.approx(100.2)
    assert settled.buy_commission == pytest.approx(0.30)
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert len(rows) == 2
    assert {row["execution_id"] for row in rows} == {"CALLBACK-1", "CALLBACK-2"}

    broker.events.extend([first_exec, first_commission, second_exec, second_commission])
    controller._drain_broker_events()
    replayed = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert len(replayed) == 2
    assert sum(float(row["commission"]) for row in replayed) == pytest.approx(0.30)


def test_late_commission_updates_completed_cycle_once(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "BUY_TRAIL")
    cycle.sell_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "SELL_TRAIL")
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.buy_commission = 1.0
    cycle.sell_filled_qty = 10
    cycle.avg_sell_price = 105.0
    cycle.sell_commission = 0.0
    cycle.gross_pnl = 50.0
    cycle.net_pnl = 49.0
    controller.storage.upsert_cycle(cycle)
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="BUY",
        shares=10,
        price=100.0,
        commission=1.0,
        order_ref=cycle.buy_order_ref,
        execution_id="COMPLETE-BUY",
    )
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="SELL",
        shares=10,
        price=105.0,
        commission=None,
        order_ref=cycle.sell_order_ref,
        execution_id="COMPLETE-SELL",
    )
    controller.active_cycle = None

    event = {
        "event_type": "COMMISSION_REPORT",
        "created_at": "2026-07-23T15:00:00+00:00",
        "order_ref": cycle.sell_order_ref,
        "execution_id": "COMPLETE-SELL",
        "commission": 0.50,
        "currency": "USD",
        "ticker": cycle.ticker,
    }
    broker.events.extend([event, event])
    controller._drain_broker_events()

    updated = controller.storage.get_cycle(cycle.id)
    assert updated is not None
    assert updated.stage == Stage.CYCLE_COMPLETE
    assert updated.gross_pnl == pytest.approx(50.0)
    assert updated.sell_commission == pytest.approx(0.50)
    assert updated.net_pnl == pytest.approx(48.50)
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert len(rows) == 2


def test_foreign_prefixed_callbacks_are_persisted_unowned_and_never_applied(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch, ticker="IREN")
    cycle = _buy_cycle(controller, broker, quantity=10)
    original_status = controller.status
    foreign_ref = "IBKRBOT|NBIS|CYCLE-000027|FOREIGN01|BUY_TRAIL"
    broker.events.extend(
        [
            {
                "event_type": "EXEC_DETAILS",
                "created_at": "2026-07-23T16:00:00+00:00",
                "order_ref": foreign_ref,
                "execution_id": "FOREIGN-EXEC",
                "side": "BOT",
                "shares": 50,
                "price": 222.0,
                "ticker": "NBIS",
            },
            {
                "event_type": "COMMISSION_REPORT",
                "created_at": "2026-07-23T16:00:01+00:00",
                "order_ref": foreign_ref,
                "execution_id": "FOREIGN-EXEC",
                "commission": 0.36,
                "ticker": "NBIS",
            },
            {
                "event_type": "ORDER_ERROR",
                "created_at": "2026-07-23T16:00:02+00:00",
                "order_ref": foreign_ref,
                "error_code": 201,
                "message": "foreign order rejected",
                "ticker": "NBIS",
            },
        ]
    )
    controller._drain_broker_events()

    local = controller.storage.get_cycle(cycle.id)
    assert local is not None
    assert local.buy_filled_qty == 0
    assert local.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.status == original_status
    with controller.storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM executions WHERE execution_id='FOREIGN-EXEC'").fetchone()[0] == 0
        rows = con.execute(
            "SELECT cycle_id, ticker, event_type FROM broker_events WHERE order_ref=? ORDER BY id",
            (foreign_ref,),
        ).fetchall()
    assert len(rows) == 3
    assert all(row["cycle_id"] is None for row in rows)
    assert all(row["ticker"] == "NBIS" for row in rows)


def test_exact_historical_order_ref_updates_its_cycle_not_current_cycle(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    historical = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 100.0, 0.0)
    historical.stage = Stage.CYCLE_COMPLETE
    historical.buy_order_ref = make_order_ref(historical.ticker, 1, historical.id, "BUY_TRAIL")
    historical.buy_filled_qty = 2
    historical.avg_buy_price = 100.0
    controller.storage.upsert_cycle(historical)
    controller.storage.upsert_execution(
        cycle=historical,
        ticker=historical.ticker,
        side="BUY",
        shares=2,
        price=100.0,
        commission=None,
        order_ref=historical.buy_order_ref,
        execution_id="HIST-EXEC",
    )

    current = _buy_cycle(controller, broker, quantity=10)
    broker.events.append(
        {
            "event_type": "COMMISSION_REPORT",
            "created_at": "2026-07-23T16:10:00+00:00",
            "order_ref": historical.buy_order_ref,
            "execution_id": "HIST-EXEC",
            "commission": 0.20,
            "ticker": historical.ticker,
        }
    )
    controller._drain_broker_events()

    historical_after = controller.storage.get_cycle(historical.id)
    current_after = controller.storage.get_cycle(current.id)
    assert historical_after is not None and historical_after.buy_commission == pytest.approx(0.20)
    assert current_after is not None and current_after.buy_commission == pytest.approx(0.0)
    assert controller.active_cycle.id == current.id


def test_native_trailing_diagnostic_uses_stable_throttle_key(tmp_path, monkeypatch) -> None:
    controller, _broker = _controller(tmp_path, monkeypatch)
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.buy_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "BUY_TRAIL")
    cycle.sell_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "SELL_TRAIL")
    messages: list[str] = []
    controller._log = lambda level, message, active_cycle=None, raw=None: messages.append(message)

    controller.price_snapshot = {
        "native_order_trigger": {
            "active": True,
            "side": "SELL",
            "selected_price": 104.01,
            "raw_last_value": 104.00,
            "displayed_initial_stop": 103.00,
            "message": "waiting",
        }
    }
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    controller.price_snapshot["native_order_trigger"]["selected_price"] = 104.02
    controller.price_snapshot["native_order_trigger"]["raw_last_value"] = 104.01
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")

    assert len(messages) == 1
    assert len(controller._last_price_warning_at) == 1
    key = next(iter(controller._last_price_warning_at))
    assert cycle.sell_order_ref in key


def test_execution_timestamp_prefers_live_receipt_and_preserves_broker_time() -> None:
    adapter = IbAsyncTwsAdapter()
    execution = SimpleNamespace(
        execId="TIME-1",
        shares=10,
        price=100.0,
        avgPrice=100.0,
        side="BOT",
        orderRef="IBKRBOT|AAPL|CYCLE-000001|TIME|BUY_TRAIL",
        orderId=10,
        permId=20,
        acctNumber="DU_TEST",
        exchange="NASDAQ",
        time=dt.datetime(2026, 7, 23, 9, 50, tzinfo=dt.timezone(dt.timedelta(hours=-4))),
    )
    fill = SimpleNamespace(
        execution=execution,
        time=dt.datetime(2026, 7, 23, 13, 50, 1, tzinfo=dt.timezone.utc),
        contract=SimpleNamespace(symbol="AAPL", conId=123, secType="STK", currency="USD"),
        commissionReport=SimpleNamespace(commission=0.35, currency="USD"),
        order=SimpleNamespace(orderRef=execution.orderRef),
    )

    row = adapter._execution_dict_from_fill(fill)
    assert row is not None
    assert row["executed_at"] == "2026-07-23T13:50:01+00:00"
    assert row["time"] == row["executed_at"]
    assert row["broker_execution_time"] == "2026-07-23T13:50:00+00:00"
    assert row["fill_received_at"] == "2026-07-23T13:50:01+00:00"


def test_connect_preserves_configured_tws_decoder_timezone(monkeypatch) -> None:
    class FakeIB:
        def __init__(self) -> None:
            self.TimezoneTWS = "Europe/Amsterdam"
            self.connected = False
            self.timezone_at_connect = ""

        def isConnected(self) -> bool:
            return self.connected

        def connect(self, host: str, port: int, clientId: int, timeout: int) -> None:
            del host, port, clientId, timeout
            self.timezone_at_connect = self.TimezoneTWS
            self.connected = True

    fake = FakeIB()
    adapter = IbAsyncTwsAdapter()
    adapter.ib = fake
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (object, object, object))
    monkeypatch.setattr(adapter, "_register_broker_event_handlers", lambda: None)
    monkeypatch.setattr(adapter, "_reset_market_data_session_state", lambda cancel_existing: None)
    monkeypatch.setattr(adapter, "set_market_data_type", lambda value: None)
    monkeypatch.setattr(adapter, "refresh_open_trades_cache", lambda force=True: None)

    adapter.connect("127.0.0.1", 4002, 7, 1)

    assert fake.timezone_at_connect == "Europe/Amsterdam"
    assert fake.TimezoneTWS == "Europe/Amsterdam"


@pytest.mark.parametrize(
    ("price", "expected_orders"),
    [
        (99.99, 0),
        (100.00, 0),
        (100.01, 1),
    ],
)
def test_stage3_close_submits_only_when_selected_price_is_strictly_above_buy(
    tmp_path,
    monkeypatch,
    price: float,
    expected_orders: int,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=price)

    handled = controller._liquidate_profitable_stage3_before_close_if_needed(cycle, price)

    assert handled is (expected_orders == 1)
    assert len(_market_orders(broker)) == expected_orders
    if expected_orders:
        order = _market_orders(broker)[0]
        assert order["action"] == "SELL"
        assert order["quantity"] == 10
        assert order["tif"] == "DAY"
        assert order["outside_rth"] is False
        assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    else:
        assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
        assert controller.active_cycle.close_before_rth_liquidation_requested is False


def test_stage3_close_ignores_commissions_and_completes_stage5_after_fill(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=100.01)
    assert cycle.buy_commission == 1_000.0

    controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 100.01)
    order = _market_orders(broker)[0]
    fill = broker.fill_order(order["order_ref"], shares=10, price=100.02, commission=0.50, terminal=True)
    controller._handle_sell_order_poll(controller.active_cycle, fill)

    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 10
    assert controller.active_cycle.avg_sell_price == pytest.approx(100.02)
    assert controller.active_cycle.gross_pnl == pytest.approx(0.20)
    assert controller.active_cycle.net_pnl == pytest.approx(-1000.30)


def test_stage3_close_cancels_protective_sell_before_market_replacement(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=101.0, protective=True)
    protective_ref = cycle.protective_sell_order_ref

    controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0)

    assert broker.cancelled_orders == [protective_ref]
    assert _market_orders(broker) == []
    terminal = broker.poll_order(str(protective_ref))
    assert terminal is not None and terminal.status == "Cancelled"
    controller._handle_protective_sell_order_poll(controller.active_cycle, terminal)

    orders = _market_orders(broker)
    assert len(orders) == 1
    assert orders[0]["quantity"] == 10


def test_stage3_close_accounts_for_protective_partial_fill_before_replacement(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=101.0, protective=True)
    protective_ref = str(cycle.protective_sell_order_ref)
    controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0)

    partial = broker.fill_order(
        protective_ref,
        shares=4,
        price=100.80,
        commission=0.20,
        execution_id="PROTECTIVE-PART",
        terminal=False,
    )
    controller._handle_protective_sell_order_poll(controller.active_cycle, partial)
    broker.orders[protective_ref] = replace(broker.orders[protective_ref], status="Cancelled", remaining=6)
    controller._handle_protective_sell_order_poll(controller.active_cycle, broker.poll_order(protective_ref))

    orders = _market_orders(broker)
    assert len(orders) == 1
    assert orders[0]["quantity"] == 6
    replacement = broker.fill_order(
        orders[0]["order_ref"],
        shares=6,
        price=101.00,
        commission=0.30,
        execution_id="CLOSE-REMAINDER",
        terminal=True,
    )
    controller._handle_sell_order_poll(controller.active_cycle, replacement)

    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 10
    assert controller.active_cycle.avg_sell_price == pytest.approx(100.92)
    assert controller.active_cycle.sell_commission == pytest.approx(0.50)


def test_stage3_close_fails_safe_when_price_falls_after_protective_cancel(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=101.0, protective=True)
    protective_ref = str(cycle.protective_sell_order_ref)
    controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0)
    controller.price_snapshot = {"price": 99.0}

    terminal = broker.poll_order(protective_ref)
    assert terminal is not None and terminal.status == "Cancelled"
    controller._handle_protective_sell_order_poll(controller.active_cycle, terminal)

    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.ERROR
    assert "no longer strictly above" in str(controller.active_cycle.error_message)


def test_stage3_close_respects_cutoff_feature_and_rth_boundaries(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=101.0)

    cycle.cancel_sell_and_liquidate_before_close_enabled = False
    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0) is False
    cycle.cancel_sell_and_liquidate_before_close_enabled = True
    _set_minutes_to_close(controller, 5.01)
    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0) is False
    _set_minutes_to_close(controller, -0.01, is_open=False)
    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0) is False
    assert _market_orders(broker) == []


def test_exact_order_ref_with_wrong_side_or_ticker_cannot_change_execution_ledger(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=10)
    wrong_side = _callback_execution(cycle, "WRONG-SIDE", 10, 100.0)
    wrong_side["side"] = "SLD"
    wrong_ticker = _callback_execution(cycle, "WRONG-TICKER", 10, 100.0)
    wrong_ticker["ticker"] = "NBIS"
    broker.events.extend([wrong_side, wrong_ticker])

    controller._drain_broker_events()

    stored = controller.storage.get_cycle(cycle.id)
    assert stored is not None and stored.buy_filled_qty == 0
    with controller.storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


def test_late_commission_preserves_combined_protective_and_replacement_exit(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "BUY_TRAIL")
    cycle.protective_sell_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "PROTECTIVE_SELL_TRAIL")
    cycle.sell_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "RTH_CLOSE_SELL_MARKET")
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.buy_commission = 1.0
    cycle.protective_sell_filled_qty = 4
    cycle.protective_avg_sell_price = 101.0
    cycle.protective_sell_commission = 0.20
    cycle.sell_filled_qty = 10
    cycle.avg_sell_price = 101.60
    cycle.sell_commission = 0.20
    cycle.gross_pnl = 16.0
    cycle.net_pnl = 14.80
    controller.storage.upsert_cycle(cycle)
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="BUY",
        shares=10,
        price=100.0,
        commission=1.0,
        order_ref=cycle.buy_order_ref,
        execution_id="COMBINED-BUY",
    )
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="PROTECTIVE_SELL",
        shares=4,
        price=101.0,
        commission=0.20,
        order_ref=cycle.protective_sell_order_ref,
        execution_id="COMBINED-PROTECTIVE",
    )
    controller.storage.upsert_execution(
        cycle=cycle,
        ticker=cycle.ticker,
        side="SELL",
        shares=6,
        price=102.0,
        commission=None,
        order_ref=cycle.sell_order_ref,
        execution_id="COMBINED-REPLACEMENT",
    )
    controller.active_cycle = None
    broker.events.append(
        {
            "event_type": "COMMISSION_REPORT",
            "created_at": "2026-07-23T17:00:00+00:00",
            "order_ref": cycle.sell_order_ref,
            "execution_id": "COMBINED-REPLACEMENT",
            "commission": 0.30,
            "ticker": cycle.ticker,
        }
    )

    controller._drain_broker_events()

    updated = controller.storage.get_cycle(cycle.id)
    assert updated is not None
    assert updated.stage == Stage.CYCLE_COMPLETE
    assert updated.sell_filled_qty == 10
    assert updated.avg_sell_price == pytest.approx(101.60)
    assert updated.sell_commission == pytest.approx(0.50)
    assert updated.gross_pnl == pytest.approx(16.0)
    assert updated.net_pnl == pytest.approx(14.50)


def test_restart_reconciles_all_buy_fills_that_arrived_during_cancel_race(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=56)
    first = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=28,
        price=185.0,
        commission=0.18,
        execution_id="RESTART-1",
        terminal=False,
    )
    controller._handle_buy_order_poll(cycle, first)
    broker.fill_order(
        str(cycle.buy_order_ref),
        shares=28,
        price=185.08,
        commission=0.19,
        execution_id="RESTART-2",
        terminal=True,
    )

    controller_module = _install_qt_stub(monkeypatch)
    recovered = make_controller(controller_module, controller.storage.db_path, broker, controller.strategy)
    recovered.connection.account = "SIM"
    recovered.storage.backup_database = lambda *args, **kwargs: None
    recovered._start_trade_market_data_capture = lambda *args, **kwargs: None
    recovered._recover_after_connect()

    assert recovered.active_cycle is not None
    assert recovered.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert recovered.active_cycle.buy_filled_qty == 56
    assert recovered.active_cycle.avg_buy_price == pytest.approx(185.04)
    assert recovered.active_cycle.buy_commission == pytest.approx(0.37)


def test_stage3_close_can_trigger_later_within_cutoff_after_price_turns_profitable(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=99.0)

    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 99.0) is False
    controller.price_snapshot = {"price": 100.01}
    assert controller._liquidate_profitable_stage3_before_close_if_needed(controller.active_cycle, 100.01) is True
    assert len(_market_orders(broker)) == 1


def test_terminal_zero_poll_preserves_callback_buy_fill_and_enters_stage3(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, quantity=10)
    captures: list[tuple[str, int]] = []
    backups: list[str] = []
    controller._start_trade_market_data_capture = (
        lambda event_type, captured_cycle, polled=None, extra=None: captures.append(
            (event_type, int(captured_cycle.buy_filled_qty or 0))
        )
    )
    controller.storage.backup_database = lambda reason: backups.append(str(reason))

    broker.events.append(_callback_execution(cycle, "CALLBACK-PARTIAL", 4, 99.0))
    controller._drain_broker_events()

    partial = controller.storage.get_cycle(cycle.id)
    assert partial is not None
    assert partial.stage == Stage.BUY_TRAIL_ACTIVE
    assert partial.buy_filled_qty == 4
    assert captures == [("BUY_FILL", 4)]
    assert backups == ["after_buy_partial_fill"]

    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Cancelled",
        filled=0,
        remaining=6,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"source": "terminal status omitted cumulative quantity"},
    )
    controller._handle_buy_order_poll(partial, terminal)

    settled = controller.active_cycle
    assert settled.stage == Stage.WAIT_RISE_TRIGGER
    assert settled.buy_filled_qty == 4
    assert settled.avg_buy_price == pytest.approx(99.0)
    assert captures == [("BUY_FILL", 4)]
    assert backups == ["after_buy_partial_fill", "after_buy_fill"]


def test_close_market_cumulative_placeholder_is_replaced_by_late_sell_callbacks(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _stage3_cycle(controller, current_price=101.0, average_buy_price=100.0)

    assert controller._liquidate_profitable_stage3_before_close_if_needed(cycle, 101.0) is True
    submitted = controller.active_cycle
    assert submitted is not None
    assert submitted.stage == Stage.SELL_TRAIL_ACTIVE
    assert submitted.sell_order_ref

    terminal = PolledOrderState(
        order_ref=str(submitted.sell_order_ref),
        order_id=submitted.sell_order_id,
        perm_id=submitted.sell_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=101.0,
        commission=0.30,
        executions=[],
        raw={"source": "terminal close status before execDetails"},
    )
    controller._handle_sell_order_poll(submitted, terminal)

    completed = controller.storage.get_cycle(cycle.id)
    assert completed is not None
    assert completed.stage == Stage.CYCLE_COMPLETE
    placeholder_id = controller.storage.cumulative_execution_id(str(submitted.sell_order_ref), "SELL")
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {placeholder_id}

    first = {
        "event_type": "EXEC_DETAILS",
        "created_at": "2026-07-23T15:55:00+00:00",
        "executed_at": "2026-07-23T15:54:59+00:00",
        "order_ref": submitted.sell_order_ref,
        "order_id": submitted.sell_order_id,
        "perm_id": submitted.sell_perm_id,
        "execution_id": "CLOSE-SELL-1",
        "side": "SLD",
        "shares": 4,
        "price": 100.5,
        "currency": "USD",
        "ticker": submitted.ticker,
    }
    second = {
        **first,
        "execution_id": "CLOSE-SELL-2",
        "shares": 6,
        "price": 101.33333333333333,
    }
    first_commission = {
        "event_type": "COMMISSION_REPORT",
        "created_at": "2026-07-23T15:55:01+00:00",
        "order_ref": submitted.sell_order_ref,
        "order_id": submitted.sell_order_id,
        "perm_id": submitted.sell_perm_id,
        "execution_id": "CLOSE-SELL-1",
        "commission": 0.12,
        "currency": "USD",
        "ticker": submitted.ticker,
    }
    second_commission = {**first_commission, "execution_id": "CLOSE-SELL-2", "commission": 0.18}
    broker.events.extend([first, first_commission, second, second_commission, first, second_commission])
    controller._drain_broker_events()

    reconciled = controller.storage.get_cycle(cycle.id)
    assert reconciled is not None
    assert reconciled.stage == Stage.CYCLE_COMPLETE
    assert reconciled.sell_filled_qty == 10
    assert reconciled.avg_sell_price == pytest.approx(101.0)
    assert reconciled.sell_commission == pytest.approx(0.30)
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {"CLOSE-SELL-1", "CLOSE-SELL-2"}
    assert sum(float(row["shares"]) for row in rows) == pytest.approx(10.0)
    assert sum(float(row["commission"]) for row in rows) == pytest.approx(0.30)
