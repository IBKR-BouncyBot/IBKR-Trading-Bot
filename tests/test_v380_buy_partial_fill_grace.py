"""BUY partial-fill regressions, updated for the v5.7.0 completion policy."""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

import pytest

from app.ib_adapter import BrokerAdapterError, PolledOrderState
from app.models import Stage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


def _decision_raw(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(str(row.get("raw_json") or "{}"))


def _controller(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticker: str = "AAPL",
) -> tuple[Any, DeterministicBrokerAdapter]:
    controller_module = _install_qt_stub(monkeypatch)
    broker = DeterministicBrokerAdapter(ticker=ticker)
    settings = permissive_strategy(ticker=ticker)
    controller = make_controller(
        controller_module,
        tmp_path / "v380.sqlite",
        broker,
        settings,
    )
    controller.connection.account = "SIM"
    controller.connection.trading_mode = "paper"
    controller.storage.backup_database = lambda *args, **kwargs: None
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    publish_fresh_price(controller, broker, 100.0)
    return controller, broker


def _buy_cycle(
    controller: Any,
    broker: DeterministicBrokerAdapter,
    *,
    quantity: int = 10,
    market: bool = False,
) -> Any:
    cycle = StrategyEngine.start_cycle(
        controller.strategy,
        1,
        "SIM",
        100.0,
        0.0,
    )
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = int(quantity)
    order_role = "BUY_MARKET" if market else "BUY_TRAIL"
    cycle.buy_order_ref = make_order_ref(
        cycle.ticker,
        cycle.cycle_number,
        cycle.id,
        order_role,
    )
    if market:
        handle = broker.place_market_order(
            contract=broker.contract,
            action="BUY",
            quantity=quantity,
            order_ref=cycle.buy_order_ref,
            tif="DAY",
            account="SIM",
            outside_rth=False,
        )
    else:
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
    broker.events.clear()
    cycle.buy_order_id = handle.order_id
    cycle.buy_perm_id = handle.perm_id
    cycle.buy_status = handle.status
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller.storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="MKT" if market else "TRAIL",
        order_id=handle.order_id,
        perm_id=handle.perm_id,
        order_ref=cycle.buy_order_ref,
        quantity=quantity,
        trailing_percent=None if market else 1.0,
        initial_stop_price=None if market else 101.0,
        status=handle.status,
    )
    return cycle


def _partial(
    broker: DeterministicBrokerAdapter,
    cycle: Any,
    *,
    shares: int = 4,
) -> Any:
    state = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=shares,
        price=100.0,
        commission=0.10,
        execution_id=f"PART-{shares}",
        terminal=False,
    )
    broker.events.clear()
    return state


def _age_first_fill(controller: Any) -> None:
    assert controller.active_cycle is not None
    controller.active_cycle.buy_filled_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    ).isoformat()
    controller.storage.upsert_cycle(controller.active_cycle)


def _request_preclose_cancel(controller: Any) -> None:
    cycle = controller.active_cycle
    cycle.session_timing_guard_enabled = True
    cycle.cancel_buy_before_close_minutes = 5
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_to_close": 2.0,
        "session_close_display": "21:00 UTC",
    }
    controller._cancel_buy_before_close_if_needed(cycle)


def _assert_waiting(controller: Any, broker: DeterministicBrokerAdapter, quantity: int = 4) -> None:
    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.BUY_TRAIL_ACTIVE
    assert active.buy_filled_qty == quantity
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []
    assert len(broker.placed_orders) == 1
    events = controller.storage.cycle_audit_details(active.id)["decision_events"]
    assert not any(row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED" for row in events)
    partial_event = next(row for row in events if row["event_type"] == "BUY_PARTIAL_FILL")
    assert partial_event["decision_result"] == "awaiting_terminal_buy"
    assert _decision_raw(partial_event)["partial_fill_policy"]["trigger"] == "wait"
    assert "grace" not in partial_event["message"]


@pytest.mark.parametrize("market", [False, True])
def test_first_partial_fill_keeps_original_order_working(tmp_path, monkeypatch, market) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, market=market)
    controller._handle_buy_order_poll(cycle, _partial(broker, cycle))
    _assert_waiting(controller, broker)


@pytest.mark.parametrize("market", [False, True])
@pytest.mark.parametrize("condition", [
    "elapsed", "rth_closed", "session_unavailable", "stale_bid", "non_live",
    "volatility", "spread", "missing_bid", "minimum_price", "gap",
])
def test_changed_market_conditions_do_not_cancel_executing_buy(
    tmp_path, monkeypatch, market, condition,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, market=market)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _age_first_fill(controller)
    cycle = controller.active_cycle
    first_fill_at = cycle.buy_filled_at
    if condition == "rth_closed":
        broker.rth_open = False
    elif condition == "session_unavailable":
        cycle.session_timing_guard_enabled = True
        cycle.cancel_buy_before_close_minutes = 5
        controller._session_minutes_from_rth_status = lambda: {"available": False}
    elif condition == "stale_bid":
        cycle.stale_data_guard_enabled = True
        cycle.max_bid_ask_age_seconds = 1.0
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)).isoformat()
        controller.price_snapshot["field_update_received_at"]["bid"] = old
        controller.price_snapshot["field_update_age_seconds"]["bid"] = 30.0
        assert "independent bid/ask" in controller._stale_data_guard_message_for_buy(cycle)
    elif condition == "non_live":
        controller.connection.trading_mode = "live"
        cycle.block_delayed_data_in_live = True
        controller.price_snapshot["subscription_market_data_type"] = None
        controller.price_snapshot["selected_market_data_type"] = 3
        assert controller._delayed_live_data_blocker_for_buy(cycle) is not None
    elif condition == "volatility":
        cycle.volatility_filter_enabled = True
        cycle.max_recent_price_move_pct = 1.0
        now = time.monotonic()
        controller._price_history.clear()
        controller._price_history.extend([(now - 2, 100.0), (now - 1, 102.0), (now, 101.0)])
        assert controller._volatility_guard_message_for_buy(cycle) is not None
    elif condition in {"spread", "missing_bid"}:
        cycle.max_spread_pct = 0.5
        controller.price_snapshot["fields"].update(
            bid=99.0 if condition == "spread" else None, ask=101.0,
        )
        assert controller._spread_guard_message_for_buy(cycle) is not None
    elif condition == "minimum_price":
        cycle.hard_risk_limits_enabled = True
        cycle.min_trade_price = 101.0
    elif condition == "gap":
        cycle.hard_risk_limits_enabled = True
        cycle.max_gap_from_prev_close_pct = 1.0
        controller.price_snapshot["fields"].update(close=95.0, marketPrice=100.0)

    controller._handle_buy_order_poll(cycle, state)

    _assert_waiting(controller, broker)
    assert controller.active_cycle.buy_filled_at == first_fill_at
    terminal = broker.fill_order(
        str(cycle.buy_order_ref), shares=6, price=100.1, commission=0.15,
        execution_id="REMAINDER-6", terminal=True,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, terminal)
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.buy_filled_qty == 10
    assert controller.storage.get_execution_totals(cycle.id, "BUY")["shares"] == 10
    assert broker.cancelled_orders == []
    assert len(broker.placed_orders) == 1


def test_later_partial_progress_preserves_first_fill_time_without_cancel(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    controller._handle_buy_order_poll(cycle, _partial(broker, cycle, shares=2))
    _age_first_fill(controller)
    first_fill_at = controller.active_cycle.buy_filled_at
    second = broker.fill_order(
        str(cycle.buy_order_ref), shares=2, price=100.1, commission=0.05,
        execution_id="PART-LATER-2", terminal=False,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, second)
    _assert_waiting(controller, broker)
    assert controller.active_cycle.buy_filled_at == first_fill_at


@pytest.mark.parametrize("market", [False, True])
def test_configured_preclose_cancel_still_cancels_partial_buy(tmp_path, monkeypatch, market) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, market=market)
    controller._handle_buy_order_poll(cycle, _partial(broker, cycle))
    _request_preclose_cancel(controller)
    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    controller._handle_buy_order_poll(controller.active_cycle, broker.poll_order(str(cycle.buy_order_ref)))
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.buy_filled_qty == 4
    assert len(broker.placed_orders) == 1


def test_full_fill_during_configured_cancel_race_is_still_reconciled(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    controller._handle_buy_order_poll(cycle, _partial(broker, cycle))
    _request_preclose_cancel(controller)
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    late = broker.fill_order(
        str(cycle.buy_order_ref), shares=6, price=100.2, commission=0.15,
        execution_id="PART-AFTER-CANCEL", terminal=True,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, late)
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.buy_filled_qty == 10
    assert controller.active_cycle.buy_remainder_cancel_requested is False
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {"PART-4", "PART-AFTER-CANCEL"}


def test_existing_cancel_request_is_not_duplicated(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _request_preclose_cancel(controller)
    controller._handle_buy_order_poll(controller.active_cycle, state)
    _request_preclose_cancel(controller)
    assert broker.cancelled_orders == [cycle.buy_order_ref]


def test_persisted_partial_fill_stays_working_after_reload(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _age_first_fill(controller)
    reloaded = controller.storage.get_cycle(cycle.id)
    assert reloaded is not None
    controller.active_cycle = reloaded
    controller._handle_buy_order_poll(reloaded, state)
    _assert_waiting(controller, broker)


def test_failed_configured_cancel_is_retried_without_losing_fills(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    original_cancel = broker.cancel_order
    attempts = 0

    def fail_once(order_ref: str, order_id: int | None = None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BrokerAdapterError("temporary cancel transport failure")
        original_cancel(order_ref, order_id)

    monkeypatch.setattr(broker, "cancel_order", fail_once)
    _request_preclose_cancel(controller)
    assert attempts == 1
    assert controller.active_cycle.buy_remainder_cancel_requested is False
    assert controller.active_cycle.buy_filled_qty == 4
    assert broker.cancelled_orders == []
    _request_preclose_cancel(controller)
    assert attempts == 2
    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]


def test_pending_cancel_status_does_not_send_duplicate_request(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    pending = PolledOrderState(
        order_ref=state.order_ref, order_id=state.order_id, perm_id=state.perm_id,
        status="PendingCancel", filled=state.filled, remaining=state.remaining,
        avg_fill_price=state.avg_fill_price, commission=state.commission,
        executions=list(state.executions), raw={"status": "PendingCancel"},
    )
    controller._handle_buy_order_poll(cycle, pending)
    assert broker.cancelled_orders == []
    assert controller.active_cycle.buy_status == "PendingCancel"
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    event = next(row for row in events if row["event_type"] == "BUY_PARTIAL_FILL")
    assert _decision_raw(event)["partial_fill_policy"]["cancel_pending"] is True
    assert "existing BUY cancellation" in event["message"]


@pytest.mark.parametrize("first_fill_at", [None, "invalid", "2099-01-01T00:00:00+00:00"])
def test_missing_or_future_timestamp_does_not_cancel_partial_buy(tmp_path, monkeypatch, first_fill_at) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    cycle.buy_filled_at = first_fill_at
    controller._handle_buy_order_poll(cycle, state)
    _assert_waiting(controller, broker)


@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled", "Inactive", "Rejected"])
def test_terminal_partial_settles_actual_quantity_without_extra_cancel(tmp_path, monkeypatch, status) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref), order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id, status=status, filled=4, remaining=6,
        avg_fill_price=100.0, commission=0.10, executions=[],
        raw={"reason": "broker cancelled remainder"},
    )
    controller._handle_buy_order_poll(cycle, terminal)
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.buy_filled_qty == 4
    assert controller.active_cycle.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []
