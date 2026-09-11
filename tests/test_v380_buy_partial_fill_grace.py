"""v3.8.0 delayed BUY-partial remainder cancellation regressions."""

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


def _expire_grace(controller: Any) -> None:
    assert controller.active_cycle is not None
    controller.active_cycle.buy_filled_at = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(
            seconds=controller.BUY_PARTIAL_FILL_GRACE_SECONDS + 1.0,
        )
    ).isoformat()
    controller.storage.upsert_cycle(controller.active_cycle)


def test_first_partial_fill_stays_working_during_grace(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.BUY_TRAIL_ACTIVE
    assert active.buy_filled_qty == 4
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    partial_event = next(row for row in events if row["event_type"] == "BUY_PARTIAL_FILL")
    assert partial_event["decision_result"] == "awaiting_terminal_buy"
    assert _decision_raw(partial_event)["partial_fill_policy"]["trigger"] == "grace"


def test_partial_market_buy_uses_the_same_grace_policy(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker, market=True)
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.BUY_TRAIL_ACTIVE
    assert active.buy_filled_qty == 4
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []


def test_full_fill_inside_grace_completes_without_cancellation(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    first = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, first)

    terminal = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=6,
        price=100.1,
        commission=0.15,
        execution_id="PART-6",
        terminal=True,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, terminal)

    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.WAIT_RISE_TRIGGER
    assert active.buy_filled_qty == 10
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []


def test_later_partial_progress_does_not_restart_the_grace_clock(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    first = _partial(broker, cycle, shares=2)
    controller._handle_buy_order_poll(cycle, first)

    first_fill_at = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(seconds=controller.BUY_PARTIAL_FILL_GRACE_SECONDS + 1.0)
    ).isoformat()
    controller.active_cycle.buy_filled_at = first_fill_at
    controller.storage.upsert_cycle(controller.active_cycle)
    second = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=2,
        price=100.1,
        commission=0.05,
        execution_id="PART-LATER-2",
        terminal=False,
    )
    broker.events.clear()

    controller._handle_buy_order_poll(controller.active_cycle, second)

    active = controller.active_cycle
    assert active is not None
    assert active.buy_filled_qty == 4
    assert active.buy_filled_at == first_fill_at
    assert active.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]


def test_full_fill_during_cancel_race_is_still_reconciled(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    first = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, first)
    _expire_grace(controller)
    controller._handle_buy_order_poll(controller.active_cycle, first)
    assert broker.cancelled_orders == [cycle.buy_order_ref]

    late_terminal = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=6,
        price=100.2,
        commission=0.15,
        execution_id="PART-AFTER-CANCEL",
        terminal=True,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, late_terminal)

    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.WAIT_RISE_TRIGGER
    assert active.buy_filled_qty == 10
    assert active.buy_remainder_cancel_requested is False
    rows = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in rows} == {
        "PART-4",
        "PART-AFTER-CANCEL",
    }


def test_nonterminal_partial_is_cancelled_after_grace_timeout(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _expire_grace(controller)

    controller._handle_buy_order_poll(controller.active_cycle, state)

    active = controller.active_cycle
    assert active is not None
    assert active.buy_remainder_cancel_requested is True
    assert active.buy_status == "CancelRequested"
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = [
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    ]
    assert len(requested) == 1
    assert requested[0]["decision_result"] == "timeout"
    assert _decision_raw(requested[0])["partial_fill_policy"]["code"] == "partial_fill_timeout"


def test_rth_close_cancels_partial_remainder_before_timeout(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    broker.rth_open = False

    controller._handle_buy_order_poll(cycle, state)

    active = controller.active_cycle
    assert active is not None
    assert active.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert requested["decision_result"] == "safety"
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "rth_closed"


def test_session_close_cutoff_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    controller.strategy.session_timing_guard_enabled = True
    controller.strategy.cancel_buy_before_close_minutes = 5
    cycle = _buy_cycle(controller, broker)
    cycle.session_timing_guard_enabled = True
    cycle.cancel_buy_before_close_minutes = 5
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_since_open": 60.0,
        "minutes_to_close": 2.0,
        "session_close_display": "21:00 UTC",
        "local_time": "test",
        "source": "test",
        "message": "test",
    }
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "session_close"


def test_stale_market_data_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.stale_data_guard_enabled = True
    cycle.max_selected_price_age_seconds = 1.0
    cycle.max_bid_ask_age_seconds = 1.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._api_last_data_monotonic = time.monotonic() - 5.0
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "stale_data"


def test_live_data_downgrade_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    controller.connection.trading_mode = "live"
    cycle = _buy_cycle(controller, broker)
    cycle.block_delayed_data_in_live = True
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    assert controller.price_snapshot is not None
    controller.price_snapshot["subscription_market_data_type"] = None
    controller.price_snapshot["selected_market_data_type"] = 3
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "non_live_data"


def test_volatility_guard_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.volatility_filter_enabled = True
    cycle.volatility_window_seconds = 300
    cycle.max_recent_price_move_pct = 1.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    now = time.monotonic()
    controller._price_history.clear()
    controller._price_history.extend(
        [(now - 2.0, 100.0), (now - 1.0, 102.0), (now, 101.0)]
    )
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "volatility"


def test_excessive_spread_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.hard_risk_limits_enabled = True
    cycle.max_spread_pct = 0.5
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    publish_fresh_price(controller, broker, 100.0)
    assert controller.price_snapshot is not None
    controller.price_snapshot["fields"]["bid"] = 99.0
    controller.price_snapshot["fields"]["ask"] = 101.0
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "spread"


def test_unverifiable_spread_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.hard_risk_limits_enabled = True
    cycle.max_spread_pct = 0.5
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    assert controller.price_snapshot is not None
    controller.price_snapshot["fields"]["bid"] = None
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert (
        _decision_raw(requested)["partial_fill_policy"]["code"]
        == "spread_unverified"
    )


def test_minimum_trade_price_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.hard_risk_limits_enabled = True
    cycle.min_trade_price = 101.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert (
        _decision_raw(requested)["partial_fill_policy"]["code"]
        == "min_trade_price"
    )


def test_gap_limit_cancels_partial_remainder_before_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.hard_risk_limits_enabled = True
    cycle.max_gap_from_prev_close_pct = 1.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    assert controller.price_snapshot is not None
    controller.price_snapshot["fields"]["close"] = 95.0
    controller.price_snapshot["fields"]["marketPrice"] = 100.0
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert _decision_raw(requested)["partial_fill_policy"]["code"] == "gap"


def test_existing_cancel_request_is_not_duplicated(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _expire_grace(controller)
    controller._handle_buy_order_poll(controller.active_cycle, state)
    assert broker.cancelled_orders == [cycle.buy_order_ref]

    controller._handle_buy_order_poll(controller.active_cycle, state)

    assert broker.cancelled_orders == [cycle.buy_order_ref]



def test_persisted_first_fill_time_survives_reload_and_expires(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _expire_grace(controller)

    reloaded = controller.storage.get_cycle(cycle.id)
    assert reloaded is not None
    controller.active_cycle = reloaded
    controller._handle_buy_order_poll(reloaded, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]


def test_unavailable_session_boundaries_cancel_before_timeout(tmp_path, monkeypatch) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    cycle.session_timing_guard_enabled = True
    cycle.cancel_buy_before_close_minutes = 5
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._session_minutes_from_rth_status = lambda: {
        "available": False,
        "minutes_since_open": None,
        "minutes_to_close": None,
        "message": "contract session boundaries unavailable",
    }
    state = _partial(broker, cycle)

    controller._handle_buy_order_poll(cycle, state)

    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    requested = next(
        row
        for row in events
        if row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
    )
    assert (
        _decision_raw(requested)["partial_fill_policy"]["code"]
        == "session_timing_unavailable"
    )



def test_failed_remainder_cancel_is_retried_on_a_later_poll(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _expire_grace(controller)

    original_cancel = broker.cancel_order
    attempts = 0

    def fail_once(order_ref: str, order_id: int | None = None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BrokerAdapterError("temporary cancel transport failure")
        original_cancel(order_ref, order_id)

    monkeypatch.setattr(broker, "cancel_order", fail_once)

    controller._handle_buy_order_poll(controller.active_cycle, state)

    assert attempts == 1
    assert controller.active_cycle.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []
    first_events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert not any(
        row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
        for row in first_events
    )

    controller._handle_buy_order_poll(controller.active_cycle, state)

    assert attempts == 2
    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]
    final_events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert sum(
        row["event_type"] == "BUY_REMAINDER_CANCEL_REQUESTED"
        for row in final_events
    ) == 1


def test_pending_cancel_status_does_not_send_a_duplicate_request(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    _expire_grace(controller)
    pending = PolledOrderState(
        order_ref=state.order_ref,
        order_id=state.order_id,
        perm_id=state.perm_id,
        status="PendingCancel",
        filled=state.filled,
        remaining=state.remaining,
        avg_fill_price=state.avg_fill_price,
        commission=state.commission,
        executions=list(state.executions),
        raw={**dict(state.raw or {}), "status": "PendingCancel"},
    )

    controller._handle_buy_order_poll(controller.active_cycle, pending)

    assert broker.cancelled_orders == []
    assert controller.active_cycle.buy_status == "PendingCancel"


def test_missing_first_fill_timestamp_starts_a_fresh_bounded_grace(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    state = _partial(broker, cycle)
    controller._handle_buy_order_poll(cycle, state)
    controller.active_cycle.buy_filled_at = None
    controller.storage.upsert_cycle(controller.active_cycle)

    controller._handle_buy_order_poll(controller.active_cycle, state)

    active = controller.active_cycle
    assert active is not None
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []
    assert dt.datetime.fromisoformat(str(active.buy_filled_at)).tzinfo is not None


def test_terminal_partial_before_timeout_settles_without_extra_cancel(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Cancelled",
        filled=4,
        remaining=6,
        avg_fill_price=100.0,
        commission=0.10,
        executions=[],
        raw={"reason": "exchange cancelled remainder"},
    )

    controller._handle_buy_order_poll(cycle, terminal)

    active = controller.active_cycle
    assert active is not None
    assert active.stage == Stage.WAIT_RISE_TRIGGER
    assert active.buy_filled_qty == 4
    assert active.buy_remainder_cancel_requested is False
    assert broker.cancelled_orders == []


def test_future_first_fill_timestamp_starts_a_fresh_bounded_grace(
    tmp_path,
    monkeypatch,
) -> None:
    controller, broker = _controller(tmp_path, monkeypatch)
    cycle = _buy_cycle(controller, broker)
    now = dt.datetime.now(dt.timezone.utc)
    cycle.buy_filled_at = (now + dt.timedelta(hours=1)).isoformat()

    elapsed = controller._buy_partial_fill_elapsed_seconds(cycle, now_utc=now)

    assert elapsed == 0.0
    assert dt.datetime.fromisoformat(str(cycle.buy_filled_at)) == now
