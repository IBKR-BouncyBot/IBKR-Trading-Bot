"""Regression coverage for the optional Stage-4 close-before-RTH policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


def _set_minutes_to_close(controller, minutes: float, *, is_open: bool = True) -> None:
    controller._latest_rth_status = {
        "is_open": bool(is_open),
        "session_open": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "session_close": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        "time_zone": "UTC",
        "source": "test",
        "message": "test session",
    }
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_since_open": 60.0,
        "minutes_to_close": float(minutes),
        "session_open_display": "14:30 UTC",
        "session_close_display": "21:00 UTC",
        "local_time": "test",
        "source": "test",
        "message": "test session",
    }


def _stage4_setup(tmp_path, monkeypatch, *, enabled: bool = True, auto_repeat: bool = False):
    controller_module = _install_qt_stub(monkeypatch)
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy(auto_repeat=auto_repeat)
    settings.cancel_sell_and_liquidate_before_close_enabled = enabled
    settings.liquidate_before_close_minutes = 5
    controller = make_controller(controller_module, tmp_path / "bot_state.sqlite", broker, settings)
    controller.connection.account = "SIM"

    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.buy_commission = 1.0
    cycle.buy_status = "Filled"
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "SELL_TRAIL")
    cycle.sell_initial_trail_stop_price = 102.0
    handle = broker.place_trailing_stop(
        contract=broker.contract,
        action="SELL",
        quantity=10,
        trailing_percent=1.0,
        initial_stop_price=102.0,
        order_ref=cycle.sell_order_ref,
        tif="GTC",
        account="SIM",
        outside_rth=False,
    )
    cycle.sell_order_id = handle.order_id
    cycle.sell_perm_id = handle.perm_id
    cycle.sell_status = handle.status
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    _set_minutes_to_close(controller, 5.0)
    return controller, broker, cycle


def _market_orders(broker: DeterministicBrokerAdapter) -> list[dict]:
    return [row for row in broker.placed_orders if row.get("order_type") == "MKT"]


def test_defaults_validation_and_storage_round_trip(tmp_path) -> None:
    settings = StrategySettings(ticker="AAPL", atr_adaptive_enabled=False)
    assert settings.cancel_sell_and_liquidate_before_close_enabled is False
    assert settings.liquidate_before_close_minutes == 5
    assert not [error for error in settings.validate() if "Liquidate-before-close" in error]

    settings.cancel_sell_and_liquidate_before_close_enabled = True
    settings.liquidate_before_close_minutes = 0
    assert "Liquidate-before-close minutes must be between 1 and 240." in settings.validate()

    settings.liquidate_before_close_minutes = 5
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.close_before_rth_liquidation_requested = True
    cycle.close_before_rth_cancel_requested = True
    storage = BotStorage(tmp_path / "roundtrip.sqlite")
    storage.upsert_cycle(cycle)
    loaded = storage.get_cycle(cycle.id)
    assert loaded is not None
    assert loaded.cancel_sell_and_liquidate_before_close_enabled is True
    assert loaded.liquidate_before_close_minutes == 5
    assert loaded.close_before_rth_liquidation_requested is True
    assert loaded.close_before_rth_cancel_requested is True


def test_disabled_or_before_cutoff_does_not_touch_stage4_order(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch, enabled=False)
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert broker.cancelled_orders == []
    assert _market_orders(broker) == []
    assert controller.active_cycle.close_before_rth_liquidation_requested is False

    controller.active_cycle.cancel_sell_and_liquidate_before_close_enabled = True
    _set_minutes_to_close(controller, 5.01)
    controller._cancel_sell_and_liquidate_before_close_if_needed(controller.active_cycle)
    assert broker.cancelled_orders == []
    assert _market_orders(broker) == []


def test_cutoff_requests_one_cancel_and_waits_for_terminal_confirmation(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref

    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    controller._cancel_sell_and_liquidate_before_close_if_needed(controller.active_cycle)

    assert broker.cancelled_orders == [original_ref]
    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert controller.active_cycle.close_before_rth_liquidation_requested is True
    assert controller.active_cycle.close_before_rth_cancel_requested is True
    assert controller.active_cycle.stop_after_current_cycle is False


def test_cancel_confirmation_submits_day_rth_only_market_sell(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    polled = broker.poll_order(original_ref)
    assert polled is not None and polled.status == "Cancelled"
    controller._handle_sell_order_poll(controller.active_cycle, polled)

    orders = _market_orders(broker)
    assert len(orders) == 1
    assert orders[0]["action"] == "SELL"
    assert orders[0]["quantity"] == 10
    assert orders[0]["tif"] == "DAY"
    assert orders[0]["outside_rth"] is False
    assert orders[0]["order_ref"].endswith("|RTH_CLOSE_SELL_MARKET")
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert controller.active_cycle.close_before_rth_liquidation_requested is True
    assert controller.active_cycle.close_before_rth_cancel_requested is False


def test_original_trail_full_fill_during_cancel_race_completes_without_replacement(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    filled = broker.fill_order(original_ref, shares=10, price=103.0, commission=1.5, terminal=True)
    controller._handle_sell_order_poll(controller.active_cycle, filled)

    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 10
    assert controller.active_cycle.avg_sell_price == 103.0
    assert controller.active_cycle.close_before_rth_liquidation_requested is False
    assert controller.active_cycle.stop_after_current_cycle is False


def test_partial_original_fill_is_aggregated_with_replacement_fill(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    partial = broker.fill_order(original_ref, shares=4, price=103.0, commission=0.4, terminal=False)
    controller._handle_sell_order_poll(controller.active_cycle, partial)
    broker.orders[original_ref] = replace(broker.orders[original_ref], status="Cancelled", remaining=6)
    cancelled = broker.poll_order(original_ref)
    assert cancelled is not None
    controller._handle_sell_order_poll(controller.active_cycle, cancelled)

    orders = _market_orders(broker)
    assert len(orders) == 1
    assert orders[0]["quantity"] == 6
    replacement_ref = orders[0]["order_ref"]
    replacement_fill = broker.fill_order(replacement_ref, shares=6, price=104.0, commission=0.6, terminal=True)
    controller._handle_sell_order_poll(controller.active_cycle, replacement_fill)

    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 10
    assert controller.active_cycle.avg_sell_price == 103.6
    assert controller.active_cycle.sell_commission == 1.0
    assert controller.active_cycle.net_pnl == pytest.approx(34.0)


def test_unconfirmed_cancel_at_close_leaves_original_as_only_sell(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref

    def leave_working(order_ref, order_id=None):
        del order_id
        broker.cancelled_orders.append(order_ref)

    broker.cancel_order = leave_working
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    _set_minutes_to_close(controller, -0.1, is_open=False)
    controller._cancel_sell_and_liquidate_before_close_if_needed(controller.active_cycle)
    polled = broker.poll_order(original_ref)
    assert polled is not None and polled.status == "Submitted"
    controller._handle_sell_order_poll(controller.active_cycle, polled)

    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert controller.active_cycle.sell_order_ref == original_ref
    assert "No second SELL was submitted" in controller.active_cycle.error_message


def test_cancel_confirmed_after_close_moves_to_error_without_market_sell(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    broker.rth_open = False
    _set_minutes_to_close(controller, -0.1, is_open=False)

    polled = broker.poll_order(original_ref)
    assert polled is not None and polled.status == "Cancelled"
    controller._handle_sell_order_poll(controller.active_cycle, polled)

    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.ERROR
    assert "No outside-RTH replacement order was submitted" in controller.active_cycle.error_message


def test_market_submission_failure_moves_to_error_without_retry_order(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    broker.fail_operations.add("place")

    polled = broker.poll_order(original_ref)
    assert polled is not None
    controller._handle_sell_order_poll(controller.active_cycle, polled)

    assert _market_orders(broker) == []
    assert controller.active_cycle.stage == Stage.ERROR
    assert "DAY market SELL" in controller.active_cycle.error_message


def test_replacement_not_filled_by_close_is_cancelled_then_errors_on_terminal_status(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    original_terminal = broker.poll_order(original_ref)
    assert original_terminal is not None
    controller._handle_sell_order_poll(controller.active_cycle, original_terminal)
    replacement_ref = controller.active_cycle.sell_order_ref

    _set_minutes_to_close(controller, 0.0, is_open=False)
    controller._cancel_sell_and_liquidate_before_close_if_needed(controller.active_cycle)
    assert broker.cancelled_orders[-1] == replacement_ref
    replacement_terminal = broker.poll_order(replacement_ref)
    assert replacement_terminal is not None and replacement_terminal.status == "Cancelled"
    controller._handle_sell_order_poll(controller.active_cycle, replacement_terminal)

    assert controller.active_cycle.stage == Stage.ERROR
    assert "remain unsold" in controller.active_cycle.error_message
    assert len(_market_orders(broker)) == 1


def test_full_close_preserves_normal_auto_repeat_decision(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch, auto_repeat=True)
    original_ref = cycle.sell_order_ref
    called: list[bool] = []
    controller._maybe_start_next_cycle = lambda: called.append(True)
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    original_terminal = broker.poll_order(original_ref)
    assert original_terminal is not None
    controller._handle_sell_order_poll(controller.active_cycle, original_terminal)
    replacement_ref = controller.active_cycle.sell_order_ref
    filled = broker.fill_order(replacement_ref, shares=10, price=104.0, terminal=True)
    controller._handle_sell_order_poll(controller.active_cycle, filled)

    assert called == [True]
    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.stop_after_current_cycle is False


def test_option_applies_only_to_stage4_final_sell_trail(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    controller.active_cycle = cycle
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert broker.cancelled_orders == []

    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "FORCED_SELL_MARKET")
    cycle.sell_status = "Submitted"
    controller.active_cycle = cycle
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert broker.cancelled_orders == []


def test_manual_close_request_cannot_submit_second_sell_during_automatic_workflow(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    original_terminal = broker.poll_order(original_ref)
    assert original_terminal is not None and original_terminal.status == "Cancelled"
    controller._handle_sell_order_poll(controller.active_cycle, original_terminal)
    replacement_ref = controller.active_cycle.sell_order_ref

    partial = broker.fill_order(replacement_ref, shares=3, price=103.5, commission=0.3, terminal=False)
    controller._handle_sell_order_poll(controller.active_cycle, partial)
    orders_before = list(_market_orders(broker))

    controller._request_market_close_for_app_position(controller.active_cycle)

    assert _market_orders(broker) == orders_before
    assert controller.active_cycle.sell_order_ref == replacement_ref
    assert controller.active_cycle.close_before_rth_liquidation_requested is True
    assert controller.active_cycle.close_position_market_requested is False
    assert "No second market SELL was submitted" in controller.active_cycle.error_message



def test_policy_settings_apply_before_stage4_but_stage4_order_settings_remain_locked() -> None:
    base = StrategySettings(ticker="AAPL", atr_adaptive_enabled=False)
    edited = replace(
        base,
        cancel_sell_and_liquidate_before_close_enabled=True,
        liquidate_before_close_minutes=17,
    )

    for stage in (Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE, Stage.WAIT_RISE_TRIGGER):
        cycle = StrategyEngine.start_cycle(base, 1, "SIM", 100.0, 0.0)
        cycle.stage = stage
        updated, changed = StrategyEngine.apply_editable_settings(cycle, edited)
        assert updated.cancel_sell_and_liquidate_before_close_enabled is True
        assert updated.liquidate_before_close_minutes == 17
        assert "cancel SELL trail and liquidate before close" in changed
        assert "liquidate before close minutes" in changed

    stage4 = StrategyEngine.start_cycle(base, 1, "SIM", 100.0, 0.0)
    stage4.stage = Stage.SELL_TRAIL_ACTIVE
    updated, changed = StrategyEngine.apply_editable_settings(stage4, edited)
    assert updated.cancel_sell_and_liquidate_before_close_enabled is False
    assert updated.liquidate_before_close_minutes == 5
    assert changed == []


def test_cutoff_uses_actual_contract_session_boundary_including_early_close(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    delattr(controller, "_session_minutes_from_rth_status")
    now = datetime.now(timezone.utc)
    controller._latest_rth_status = {
        "is_open": True,
        "session_open": (now - timedelta(hours=2)).isoformat(),
        "session_close": (now + timedelta(minutes=4, seconds=30)).isoformat(),
        "time_zone": "UTC",
        "source": "ibkr_liquid_hours_test",
        "message": "date-specific shortened session",
    }

    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    assert broker.cancelled_orders == [cycle.sell_order_ref]
    assert controller.active_cycle.close_before_rth_liquidation_requested is True


def test_enabled_policy_fails_safe_when_session_boundary_is_unavailable(tmp_path, monkeypatch) -> None:
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    warnings: list[str] = []
    controller._session_minutes_from_rth_status = lambda: {
        "available": False,
        "minutes_to_close": None,
        "message": "missing liquidHours",
    }
    controller._log_price_warning_throttled = lambda _cycle, message, interval_seconds=30.0: warnings.append(message)

    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    assert broker.cancelled_orders == []
    assert _market_orders(broker) == []
    assert warnings and "Automatic liquidation will not start" in warnings[-1]


def test_restart_after_confirmed_cancel_recovers_and_submits_one_replacement(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot_state.sqlite"
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert broker.orders[original_ref].status == "Cancelled"

    controller_module = _install_qt_stub(monkeypatch)
    recovered = make_controller(controller_module, db_path, broker, controller.strategy)
    recovered.connection.account = "SIM"
    recovered._start_trade_market_data_capture = lambda *args, **kwargs: None
    recovered._recover_after_connect()

    orders = _market_orders(broker)
    assert len(orders) == 1
    assert orders[0]["quantity"] == 10
    assert orders[0]["order_ref"].endswith("|RTH_CLOSE_SELL_MARKET")
    assert recovered.active_cycle is not None
    assert recovered.active_cycle.sell_order_ref == orders[0]["order_ref"]


def test_restart_with_partial_original_and_replacement_fills_preserves_cumulative_quantity(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot_state.sqlite"
    controller, broker, cycle = _stage4_setup(tmp_path, monkeypatch)
    original_ref = cycle.sell_order_ref
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)

    partial_original = broker.fill_order(original_ref, shares=4, price=103.0, commission=0.4, terminal=False)
    controller._handle_sell_order_poll(controller.active_cycle, partial_original)
    broker.orders[original_ref] = replace(broker.orders[original_ref], status="Cancelled", remaining=6)
    controller._handle_sell_order_poll(controller.active_cycle, broker.poll_order(original_ref))
    replacement_ref = controller.active_cycle.sell_order_ref
    partial_replacement = broker.fill_order(replacement_ref, shares=3, price=104.0, commission=0.3, terminal=False)
    controller._handle_sell_order_poll(controller.active_cycle, partial_replacement)

    controller_module = _install_qt_stub(monkeypatch)
    recovered = make_controller(controller_module, db_path, broker, controller.strategy)
    recovered.connection.account = "SIM"
    recovered._start_trade_market_data_capture = lambda *args, **kwargs: None
    recovered._recover_after_connect()
    assert recovered.active_cycle is not None
    assert recovered.active_cycle.sell_order_ref == replacement_ref

    final_replacement = broker.fill_order(replacement_ref, shares=3, price=105.0, commission=0.3, terminal=True)
    recovered._handle_sell_order_poll(recovered.active_cycle, final_replacement)

    assert recovered.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert recovered.active_cycle.sell_filled_qty == 10
    assert recovered.active_cycle.avg_sell_price == pytest.approx(103.9)
    assert recovered.active_cycle.sell_commission == pytest.approx(1.0)
    assert len(_market_orders(broker)) == 1
