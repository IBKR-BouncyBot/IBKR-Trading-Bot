"""Risk4 guard, recovery, and failure-injection tests.

These tests use the headless Qt stub from the existing controller tests so the
controller logic can be exercised without starting a real GUI or connecting to
IBKR.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.ib_adapter import BrokerAdapterError, IbAsyncTwsAdapter, QualifiedContract, RthStatus
from app.lockfile import SingleInstanceError, SingleInstanceLock
from app.models import ConnectionSettings, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


def test_strategy_defaults_match_requested_startup_defaults():
    settings = StrategySettings(ticker="AAPL")

    assert settings.investment_amount == 10000.0
    assert settings.reinvest_profits is True
    assert settings.auto_repeat is True
    assert settings.block_delayed_data_in_live is True

    # Broker/timing safety defaults keep non-order-submitting data checks on.
    # ATR adaptive mode and the opening/closing BUY timing guard default on;
    # recent-volatility remains off because it is data-sensitive.
    assert settings.protective_sell_enabled is False
    assert settings.slippage_buffer_enabled is False
    assert settings.hard_risk_limits_enabled is False
    assert settings.what_if_check_enabled is True
    assert settings.stale_data_guard_enabled is True
    assert settings.volatility_filter_enabled is False
    assert settings.session_timing_guard_enabled is True
    assert settings.atr_adaptive_enabled is True
    assert settings.atr_block_new_buy_until_ready is True
    assert settings.atr_adapt_protective_sell_enabled is False
    assert settings.atr_period == 14
    assert settings.atr_bar_seconds == 60
    assert settings.atr_initial_drop_multiplier == 1.50
    assert settings.atr_buy_rebound_multiplier == 0.75
    assert settings.atr_minimum_profit_multiplier == 1.00
    assert settings.atr_sell_trail_multiplier == 1.00
    assert settings.atr_min_pct == 0.10
    assert settings.atr_max_pct == 20.00


def test_decision_event_log_and_backup_file(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "SIM", 100.0, 0.0)
    storage.upsert_cycle(cycle)
    storage.add_decision_event(
        event_type="TEST_EVENT",
        message="unit test event",
        cycle=cycle,
        stage_before=Stage.WAIT_INITIAL_DROP.value,
        stage_after=Stage.BUY_TRAIL_ACTIVE.value,
        decision_result="pass",
        raw={"ok": True},
    )

    backup = storage.backup_database("unit_test", keep=5)
    assert backup is not None
    assert backup.exists()
    with storage.connect() as con:
        row = con.execute("SELECT event_type, decision_result FROM decision_events").fetchone()
    assert dict(row) == {"event_type": "TEST_EVENT", "decision_result": "pass"}


def test_single_instance_lock_blocks_second_instance(tmp_path: Path):
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()

    # After release the same path can be acquired again.
    second.acquire()
    second.release()


class _WhatIfAdapter:
    def __init__(self, ok: bool):
        self.ok = ok

    def what_if_trailing_stop(self, **kwargs):
        return {"ok": self.ok, "message": "approved" if self.ok else "insufficient buying power"}


class _FailingSubmitAdapter:
    def is_connected(self):
        return True

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker, con_id=con_id or 111, raw=object(), primary_exchange=primary_exchange)

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def place_trailing_stop(self, **kwargs):
        raise BrokerAdapterError("simulated submit failure")


class _CancelRecordingAdapter:
    def __init__(self):
        self.cancelled: list[tuple[str, int | None]] = []

    def cancel_order(self, order_ref: str, order_id: int | None = None):
        self.cancelled.append((order_ref, order_id))


def _headless_controller(tmp_path: Path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.connection = ConnectionSettings(trading_mode="live", market_data_type=1)
    controller.connected = True
    return controller


def test_what_if_guard_blocks_failed_margin_check(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    controller.adapter = _WhatIfAdapter(ok=False)
    controller.contract = object()
    settings = StrategySettings(ticker="AAPL", what_if_check_enabled=True)
    controller.strategy = settings
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    payload = {"quantity": 10, "trailing_percent": 1.0, "initial_stop_price": 101.0, "order_ref": "IBKRBOT|AAPL|1|BUY"}

    message = controller._what_if_guard_message_for_buy(cycle, payload)

    assert message is not None
    assert "what-if" in message.lower()
    assert "insufficient" in message.lower()


def test_stale_data_guard_blocks_old_price_snapshot(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    settings = StrategySettings(ticker="AAPL", stale_data_guard_enabled=True)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    controller.price_snapshot = {"api_data_age_seconds": 10.0, "fields": {"bid": 100.0, "ask": 100.1}}

    message = controller._stale_data_guard_message_for_buy(cycle)

    assert message is not None
    assert "stale-data" in message.lower()


def test_volatility_guard_blocks_large_recent_move(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    settings = StrategySettings(ticker="AAPL", volatility_filter_enabled=True, max_recent_price_move_pct=2.0)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    now = time.monotonic()
    controller._price_history = deque([(now - 10, 100.0), (now - 5, 101.0), (now, 104.0)])

    message = controller._volatility_guard_message_for_buy(cycle)

    assert message is not None
    assert "volatility" in message.lower()


def test_session_timing_guard_blocks_first_minutes(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    settings = StrategySettings(ticker="AAPL", session_timing_guard_enabled=True, no_new_buy_first_minutes=5)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    controller._session_minutes_from_rth_status = lambda: {
        "available": True,
        "minutes_since_open": 2.0,
        "minutes_to_close": 300.0,
        "local_time": "test",
        "session_open_display": "09:30 EDT",
        "session_close_display": "16:00 EDT",
    }

    message = controller._session_timing_guard_message_for_buy(cycle)

    assert message is not None
    assert "first 5 minutes" in message.lower()


def test_session_timing_uses_contract_liquid_hours_early_close(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    now = datetime(2026, 7, 3, 16, 50, tzinfo=timezone.utc)  # 12:50 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260703:0930-20260703:1300",
        "America/New_York",
        now,
    )
    assert status is not None
    controller._latest_rth_status = status.to_dict()

    timing = controller._session_minutes_from_rth_status(now)

    assert timing["available"] is True
    assert timing["minutes_since_open"] == pytest.approx(200.0)
    assert timing["minutes_to_close"] == pytest.approx(10.0)
    assert timing["session_close_display"].startswith("13:00")


def test_session_timing_guard_blocks_before_liquid_hours_early_close(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    settings = StrategySettings(ticker="AAPL", session_timing_guard_enabled=True, no_new_buy_last_minutes=15)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    now = datetime(2026, 7, 3, 16, 50, tzinfo=timezone.utc)  # 12:50 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260703:0930-20260703:1300",
        "America/New_York",
        now,
    )
    assert status is not None
    controller._latest_rth_status = status.to_dict()
    real_helper = controller._session_minutes_from_rth_status
    controller._session_minutes_from_rth_status = lambda: real_helper(now)

    message = controller._session_timing_guard_message_for_buy(cycle)

    assert message is not None
    assert "last 15 minutes" in message.lower()
    assert "13:00" in message


def test_cancel_active_buy_uses_liquid_hours_early_close(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    adapter = _CancelRecordingAdapter()
    controller.adapter = adapter
    settings = StrategySettings(
        ticker="AAPL",
        session_timing_guard_enabled=True,
        cancel_buy_before_close_minutes=10,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    cycle.buy_order_id = 17
    cycle.buy_status = "Submitted"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    now = datetime(2026, 7, 3, 16, 55, tzinfo=timezone.utc)  # 12:55 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260703:0930-20260703:1300",
        "America/New_York",
        now,
    )
    assert status is not None
    controller._latest_rth_status = status.to_dict()
    real_helper = controller._session_minutes_from_rth_status
    controller._session_minutes_from_rth_status = lambda: real_helper(now)

    controller._cancel_buy_before_close_if_needed(cycle)

    assert adapter.cancelled == [(cycle.buy_order_ref, 17)]
    assert cycle.buy_status == "CancelRequested"
    assert "13:00" in str(cycle.error_message)


def test_session_timing_guard_fails_closed_without_session_boundaries(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    settings = StrategySettings(ticker="AAPL", session_timing_guard_enabled=True)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    controller._latest_rth_status = RthStatus(
        True,
        "legacy_adapter",
        "RTH open but no session boundaries were supplied.",
        utc_now_iso(),
    ).to_dict()

    message = controller._session_timing_guard_message_for_buy(cycle)

    assert message is not None
    assert "could not determine" in message.lower()
    assert "regular-session open/close" in message


def test_cancel_active_buy_does_not_guess_without_session_boundaries(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    adapter = _CancelRecordingAdapter()
    controller.adapter = adapter
    settings = StrategySettings(
        ticker="AAPL",
        session_timing_guard_enabled=True,
        cancel_buy_before_close_minutes=10,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    cycle.buy_order_id = 17
    cycle.buy_status = "Submitted"
    controller._latest_rth_status = RthStatus(
        True,
        "legacy_adapter",
        "RTH open but no session boundaries were supplied.",
        utc_now_iso(),
    ).to_dict()

    controller._cancel_buy_before_close_if_needed(cycle)

    assert adapter.cancelled == []
    assert cycle.buy_status == "Submitted"



def test_submit_failure_rolls_cycle_back_to_waiting_stage(tmp_path: Path, monkeypatch):
    controller = _headless_controller(tmp_path, monkeypatch)
    controller.adapter = _FailingSubmitAdapter()
    controller.contract = object()
    settings = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        block_delayed_data_in_live=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    controller.strategy = settings
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.last_price = 98.0
    cycle.drop_trigger_price = 99.0
    controller.active_cycle = cycle

    next_cycle, actions = StrategyEngine.on_price_update(cycle, 98.0)
    controller._execute_actions(actions, next_cycle)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert "failed" in (controller.active_cycle.error_message or "").lower()
