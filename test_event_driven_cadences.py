"""Regression tests for the event-driven worker and independent cadences."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.models import StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub

CONTROLLER_SOURCE = Path("app/controller.py").read_text(encoding="utf-8")
ADAPTER_SOURCE = Path("app/ib_adapter.py").read_text(encoding="utf-8")


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    controller_module = _install_qt_stub(monkeypatch)
    return controller_module.TradingController(
        storage=BotStorage(tmp_path / "event-driven.sqlite")
    )


def _disable_shutdown_io(controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller.storage, "add_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller.storage, "backup_database", lambda *_args, **_kwargs: None)


def test_worker_source_uses_interruptible_queue_wait_instead_of_fixed_sleep() -> None:
    thread_source = CONTROLLER_SOURCE.split("    def _thread_main", 1)[1].split(
        "    def _process_queued_command", 1
    )[0]

    assert "time.sleep(1.0)" not in thread_source
    assert "self._commands.get(timeout=wait_timeout)" in thread_source
    assert "next_broker" in thread_source
    assert "next_strategy" in thread_source
    assert "next_database" in thread_source
    assert "next_gui" in thread_source
    assert "next_maintenance" in thread_source


def test_market_data_waits_are_sliced_and_have_no_fixed_quarter_second_delay() -> None:
    price_region = ADAPTER_SOURCE.split("    def _try_price_for_contract", 1)[1].split(
        "    def _annotate_auto_snapshot", 1
    )[0]
    mode_region = ADAPTER_SOURCE.split("    def _apply_market_data_type_to_tws", 1)[1].split(
        "    def set_market_data_type", 1
    )[0]

    assert "self.ib.sleep(0.25)" not in price_region
    assert "self.ib.sleep(0.25)" not in mode_region
    assert "_MARKET_DATA_WAIT_SLICE_SECONDS = 0.05" in ADAPTER_SOURCE


def test_scheduled_strategy_reads_are_nonblocking_but_compatibility_tick_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)

    class ConnectedAdapter:
        @staticmethod
        def is_connected() -> bool:
            return True

    controller.adapter = ConnectedAdapter()
    controller.connected = True
    controller._broker_connectivity = {"upstream_connected": True}
    controller._broker_connectivity_initialized = True
    controller.active_cycle = None
    observed: list[float | None] = []
    monkeypatch.setattr(
        controller,
        "_refresh_confirmed_market_data_if_due",
        lambda *, force=False, timeout=None: observed.append(timeout),
    )

    controller._run_strategy_cycle(price_timeout=0.0)
    monkeypatch.setattr(controller, "_run_broker_cycle", lambda process_timeout=0.0: True)
    controller._tick()

    assert observed == [0.0, 0.75]


def test_gui_snapshots_do_not_repeat_database_reads_between_database_cadences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    strategy = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=False,
        atr_adaptive_enabled=False,
    )
    controller.strategy = strategy
    controller.active_cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)

    counts = {"events": 0, "history": 0, "position": 0}

    def recent_events(_limit: int) -> list[dict[str, Any]]:
        counts["events"] += 1
        return []

    def history_summary(_ticker: str) -> dict[str, Any]:
        counts["history"] += 1
        return {"cycles": 0}

    def app_position(_ticker: str) -> dict[str, Any]:
        counts["position"] += 1
        return {"quantity": 0}

    monkeypatch.setattr(controller.storage, "get_recent_events", recent_events)
    monkeypatch.setattr(controller.storage, "history_summary", history_summary)
    monkeypatch.setattr(controller.storage, "get_app_owned_unsold_position", app_position)

    controller._run_database_cycle(force=True)
    after_database_cycle = dict(counts)
    for _ in range(5):
        controller.emit_snapshot(force=True, refresh_database=True)

    assert after_database_cycle == {"events": 1, "history": 1, "position": 1}
    assert counts == after_database_cycle
    emitted = controller.signals.snapshot_updated.emissions[-1][0][0]
    assert emitted["database_snapshot"]["refreshed_at"]
    assert emitted["worker_cadences"]["database_seconds"] == pytest.approx(
        controller.DATABASE_CADENCE_SECONDS
    )


def test_order_safety_reads_live_database_state_instead_of_gui_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    strategy = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=False,
        atr_adaptive_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)
    controller.active_cycle = cycle
    quantity = [0]

    monkeypatch.setattr(
        controller.storage,
        "get_app_owned_unsold_position",
        lambda _ticker: {"quantity": quantity[0], "cycles": []},
    )
    controller._run_database_cycle(force=True)
    cached_facts = controller._snapshot_database_cache["guard_facts"]

    quantity[0] = 7
    assert controller._app_owned_position_blocker_for_buy(cycle, cached_facts) is None
    live_blocker = controller._app_owned_position_blocker_for_buy(cycle)

    assert live_blocker is not None
    assert live_blocker["code"] == "app_owned_position"
    assert "7 unsold app-owned" in live_blocker["message"]



def test_order_risk_limits_read_live_database_state_instead_of_gui_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    strategy = StrategySettings(
        ticker="AAPL",
        hard_risk_limits_enabled=True,
        max_daily_loss_ticker=100.0,
        atr_adaptive_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)
    controller.active_cycle = cycle
    pnl = [0.0]

    monkeypatch.setattr(
        controller.storage,
        "get_daily_net_pnl_for_ticker",
        lambda _ticker: pnl[0],
    )
    controller._run_database_cycle(force=True)
    cached_facts = controller._snapshot_database_cache["guard_facts"]

    pnl[0] = -125.0
    cached_blockers = controller._risk_guard_blockers_for_buy(cycle, database_facts=cached_facts)
    live_blockers = controller._risk_guard_blockers_for_buy(cycle)

    assert all(item["code"] != "daily_loss_ticker" for item in cached_blockers)
    daily_loss = next(item for item in live_blockers if item["code"] == "daily_loss_ticker")
    assert "-125.00" in daily_loss["message"]

def test_human_report_is_owned_by_maintenance_not_gui_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    reports: list[dict[str, Any]] = []
    monkeypatch.setattr(
        controller.storage,
        "write_human_debug_report",
        lambda snapshot: reports.append(dict(snapshot)),
    )

    controller._run_database_cycle(force=True)
    controller.emit_snapshot(force=True, refresh_database=False)
    assert reports == []

    controller._run_maintenance_cycle(force=True)
    assert reports == [controller._last_snapshot_payload]


def test_command_queue_wakes_worker_without_waiting_for_any_periodic_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _disable_shutdown_io(controller, monkeypatch)
    entered_strategy = threading.Event()
    command_seen = threading.Event()

    controller.BROKER_CADENCE_SECONDS = 5.0
    controller.STRATEGY_CADENCE_SECONDS = 5.0
    controller.GUI_CADENCE_SECONDS = 5.0
    controller.DATABASE_CADENCE_SECONDS = 5.0
    controller.MAINTENANCE_CADENCE_SECONDS = 5.0
    controller.MAX_IDLE_WAIT_SECONDS = 5.0

    monkeypatch.setattr(controller, "_run_database_cycle", lambda *, force=False: None)
    monkeypatch.setattr(
        controller,
        "emit_snapshot",
        lambda force=False, *, refresh_database=True: None,
    )
    monkeypatch.setattr(
        controller,
        "_run_maintenance_cycle",
        lambda *, force=False: None,
    )
    monkeypatch.setattr(
        controller,
        "_run_broker_cycle",
        lambda process_timeout=0.0: True,
    )

    def strategy_cycle(*, price_timeout: float = 0.0) -> None:
        assert price_timeout == 0.0
        entered_strategy.set()

    def handle_command(name: str, payload: dict[str, Any]) -> None:
        del payload
        if name == "PING":
            command_seen.set()
        elif name == "SHUTDOWN":
            controller._stop_event.set()

    monkeypatch.setattr(controller, "_run_strategy_cycle", strategy_cycle)
    monkeypatch.setattr(controller, "_handle_command", handle_command)

    controller.start_thread()
    try:
        assert entered_strategy.wait(1.0)
        started = time.perf_counter()
        controller._commands.put(("PING", {}))
        assert command_seen.wait(1.0)
        assert time.perf_counter() - started < 0.5
    finally:
        controller.shutdown()

    assert controller._shutdown_complete.is_set()



def test_shutdown_preempts_an_already_queued_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _disable_shutdown_io(controller, monkeypatch)
    queue_wait_entered = threading.Event()
    release_queue_wait = threading.Event()
    handled: list[str] = []

    class ControlledQueue:
        def get(self, timeout: float | None = None) -> tuple[str, dict[str, Any]]:
            del timeout
            queue_wait_entered.set()
            assert release_queue_wait.wait(1.0)
            return ("DANGEROUS_BROKER_COMMAND", {})

        @staticmethod
        def get_nowait() -> tuple[str, dict[str, Any]]:
            raise queue.Empty

        @staticmethod
        def put(_item: tuple[str, dict[str, Any]]) -> None:
            return None

    controller.BROKER_CADENCE_SECONDS = 5.0
    controller.STRATEGY_CADENCE_SECONDS = 5.0
    controller.GUI_CADENCE_SECONDS = 5.0
    controller.DATABASE_CADENCE_SECONDS = 5.0
    controller.MAINTENANCE_CADENCE_SECONDS = 5.0
    controller.MAX_IDLE_WAIT_SECONDS = 5.0
    controller._commands = ControlledQueue()

    monkeypatch.setattr(controller, "_run_database_cycle", lambda *, force=False: None)
    monkeypatch.setattr(
        controller,
        "emit_snapshot",
        lambda force=False, *, refresh_database=True: None,
    )
    monkeypatch.setattr(controller, "_run_maintenance_cycle", lambda *, force=False: None)
    monkeypatch.setattr(controller, "_run_broker_cycle", lambda process_timeout=0.0: False)
    monkeypatch.setattr(
        controller,
        "_handle_command",
        lambda name, _payload: handled.append(name),
    )

    controller.start_thread()
    assert queue_wait_entered.wait(1.0)
    controller._stop_event.set()
    release_queue_wait.set()
    assert controller._shutdown_complete.wait(2.0)

    assert handled == []

def test_periodic_responsibilities_run_on_distinct_cadences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _disable_shutdown_io(controller, monkeypatch)
    counts = {"broker": 0, "strategy": 0, "database": 0, "gui": 0, "maintenance": 0}
    started = threading.Event()

    controller.BROKER_CADENCE_SECONDS = 0.010
    controller.STRATEGY_CADENCE_SECONDS = 0.025
    controller.DATABASE_CADENCE_SECONDS = 0.060
    controller.GUI_CADENCE_SECONDS = 0.090
    controller.MAINTENANCE_CADENCE_SECONDS = 0.140
    controller.MAX_IDLE_WAIT_SECONDS = 0.020

    def broker_cycle(process_timeout: float = 0.0) -> bool:
        assert process_timeout == 0.0
        counts["broker"] += 1
        return True

    def strategy_cycle(*, price_timeout: float = 0.0) -> None:
        assert price_timeout == 0.0
        counts["strategy"] += 1
        started.set()

    def database_cycle(*, force: bool = False) -> None:
        del force
        counts["database"] += 1

    def gui_cycle(force: bool = False, *, refresh_database: bool = True) -> None:
        del force, refresh_database
        counts["gui"] += 1

    def maintenance_cycle(*, force: bool = False) -> None:
        del force
        counts["maintenance"] += 1

    monkeypatch.setattr(controller, "_run_broker_cycle", broker_cycle)
    monkeypatch.setattr(controller, "_run_strategy_cycle", strategy_cycle)
    monkeypatch.setattr(controller, "_run_database_cycle", database_cycle)
    monkeypatch.setattr(controller, "emit_snapshot", gui_cycle)
    monkeypatch.setattr(controller, "_run_maintenance_cycle", maintenance_cycle)

    controller.start_thread()
    try:
        assert started.wait(1.0)
        time.sleep(0.45)
    finally:
        controller.shutdown()

    # Startup/finalization intentionally add one database, GUI, and maintenance
    # call. Even with those calls included, the configured ordering must remain.
    assert counts["broker"] > counts["strategy"] > counts["database"]
    assert counts["database"] >= counts["gui"] >= counts["maintenance"]
    assert counts["maintenance"] >= 3


def test_broker_cadence_failure_prevents_strategy_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _disable_shutdown_io(controller, monkeypatch)
    broker_attempted = threading.Event()
    strategy_calls = 0

    controller.BROKER_CADENCE_SECONDS = 0.010
    controller.STRATEGY_CADENCE_SECONDS = 0.010
    controller.GUI_CADENCE_SECONDS = 1.0
    controller.DATABASE_CADENCE_SECONDS = 1.0
    controller.MAINTENANCE_CADENCE_SECONDS = 1.0
    controller.MAX_IDLE_WAIT_SECONDS = 0.020

    monkeypatch.setattr(controller, "_run_database_cycle", lambda *, force=False: None)
    monkeypatch.setattr(
        controller,
        "emit_snapshot",
        lambda force=False, *, refresh_database=True: None,
    )
    monkeypatch.setattr(
        controller,
        "_run_maintenance_cycle",
        lambda *, force=False: None,
    )

    def failing_broker(process_timeout: float = 0.0) -> bool:
        del process_timeout
        broker_attempted.set()
        raise RuntimeError("broker callback pump failed")

    def strategy_cycle(*, price_timeout: float = 0.0) -> None:
        nonlocal strategy_calls
        del price_timeout
        strategy_calls += 1

    monkeypatch.setattr(controller, "_run_broker_cycle", failing_broker)
    monkeypatch.setattr(controller, "_run_strategy_cycle", strategy_cycle)

    controller.start_thread()
    try:
        assert broker_attempted.wait(1.0)
        time.sleep(0.08)
    finally:
        controller.shutdown()

    assert strategy_calls == 0
