"""v3.9.0 fail-closed worker watchdog and replacement-process regressions."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import CycleState, Stage, recovery_cycle_signature
from app.storage import BotStorage
from app.strategy import StrategyEngine
from app.watchdog import (
    WATCHDOG_RESTART_BACKOFF_SECONDS,
    append_emergency_log,
    consume_watchdog_restart_request,
    create_watchdog_restart_request,
    discard_watchdog_restart_request,
    emergency_log_path,
    record_watchdog_restart_attempt,
    watchdog_history_path,
    watchdog_request_path,
    watchdog_restart_delay_seconds,
)
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_controller_headless import _install_qt_stub

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def test_emergency_log_is_plain_file_and_never_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    append_emergency_log(
        "worker failed",
        exc=RuntimeError("boom"),
        context={"operation": "strategy"},
        base_dir=tmp_path,
    )
    text = emergency_log_path(tmp_path).read_text(encoding="utf-8")
    assert "worker failed" in text
    assert "RuntimeError: boom" in text
    assert '"operation": "strategy"' in text

    monkeypatch.setattr("app.watchdog.os.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    append_emergency_log("must not escape", base_dir=tmp_path)


def test_restart_request_is_one_time_exact_cycle_handoff(tmp_path: Path) -> None:
    snapshot = {
        "active_cycle": {
            "id": "cycle-1",
            "stage": Stage.WAIT_RISE_TRIGGER.value,
            "ticker": "AAPL",
            "con_id": 123,
            "sell_order_ref": None,
        },
        "status": "running",
    }
    request = create_watchdog_restart_request(
        "secret-token",
        snapshot,
        "worker stalled",
        base_dir=tmp_path,
        now_epoch=1_000.0,
    )
    assert request["auto_resume"] is True
    assert request["was_monitoring_active"] is True
    assert request["expected_cycle_id"] == "cycle-1"
    assert request["expected_cycle_stage"] == Stage.WAIT_RISE_TRIGGER.value
    assert request["expected_cycle_signature"]["id"] == "cycle-1"
    assert request["expected_cycle_signature"]["stage"] == Stage.WAIT_RISE_TRIGGER.value
    if sys.platform != "win32":
        assert watchdog_request_path(tmp_path).stat().st_mode & 0o077 == 0

    assert consume_watchdog_restart_request(
        "wrong-token",
        base_dir=tmp_path,
        now_epoch=1_001.0,
    ) is None
    consumed = consume_watchdog_restart_request(
        "secret-token",
        base_dir=tmp_path,
        now_epoch=1_001.0,
    )
    assert consumed is not None
    assert consumed["age_seconds"] == pytest.approx(1.0)
    assert "token" not in consumed
    assert consume_watchdog_restart_request(
        "secret-token",
        base_dir=tmp_path,
        now_epoch=1_002.0,
    ) is None


def test_expired_restart_request_never_leaks_token_to_emergency_log(
    tmp_path: Path,
) -> None:
    secret = "do-not-log-this-token"
    create_watchdog_restart_request(
        secret,
        {
            "active_cycle": {
                "id": "cycle-1",
                "stage": Stage.WAIT_RISE_TRIGGER.value,
                "ticker": "AAPL",
            }
        },
        "test expiry",
        base_dir=tmp_path,
        now_epoch=1_000.0,
    )

    assert consume_watchdog_restart_request(
        secret,
        base_dir=tmp_path,
        now_epoch=2_000.0,
        max_age_seconds=10.0,
    ) is None
    diagnostic = emergency_log_path(tmp_path).read_text(encoding="utf-8")
    assert secret not in diagnostic
    assert "[redacted]" in diagnostic


def test_restart_loop_history_allows_three_fast_attempts_then_cools_down(tmp_path: Path) -> None:
    for moment in (100.0, 101.0, 102.0):
        assert record_watchdog_restart_attempt(
            base_dir=tmp_path,
            now_epoch=moment,
            reason="test",
        ) is True
    delay = watchdog_restart_delay_seconds(base_dir=tmp_path, now_epoch=103.0)
    assert delay == pytest.approx(WATCHDOG_RESTART_BACKOFF_SECONDS - 1.0)
    assert watchdog_restart_delay_seconds(
        base_dir=tmp_path,
        now_epoch=102.0 + WATCHDOG_RESTART_BACKOFF_SECONDS,
    ) == pytest.approx(0.0)


def test_restart_history_write_failure_is_not_silently_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.watchdog._atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert record_watchdog_restart_attempt(base_dir=tmp_path, reason="test") is False
    assert not watchdog_history_path(tmp_path).exists()


def test_storage_write_probe_does_not_modify_application_rows(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "probe.sqlite")
    before = storage.get_recent_events(10)
    assert storage.probe_writable(timeout_seconds=0.05) is True
    assert storage.get_recent_events(10) == before
    with storage.connect() as con:
        probe_table = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='__bouncybot_watchdog_write_probe'"
        ).fetchone()
    assert probe_table is None


def test_log_failure_enters_storage_fault_and_signal_failure_stays_nonfatal(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "log-fault.sqlite")
    )
    monkeypatch.setattr(
        controller.storage,
        "add_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    monkeypatch.setattr(
        controller.signals.event_logged,
        "emit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("signal failed")),
    )

    controller._log("ERROR", "original failure")

    assert controller._storage_fault_active is True
    assert "database is locked" in controller._storage_fault_message
    text = emergency_log_path(tmp_path).read_text(encoding="utf-8")
    assert "SQLite storage fault entered" in text
    assert "Qt event signal emission failed" in text


def test_waiting_transition_is_persisted_before_publish_or_broker_action(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = permissive_strategy(ticker="AAPL")
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "transition.sqlite")
    )
    current = StrategyEngine.start_cycle(strategy, 1, "SIM", 100.0, 0.0)
    next_cycle = CycleState.from_dict(current.to_dict())
    next_cycle.last_price = 99.0
    next_cycle.touch()
    controller.active_cycle = current
    actions: list[Any] = [SimpleNamespace(action_type="PLACE_BUY_TRAIL")]
    executed: list[Any] = []
    monkeypatch.setattr(
        controller.storage,
        "upsert_cycle",
        lambda cycle: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    monkeypatch.setattr(
        controller,
        "_execute_actions",
        lambda values, cycle: executed.append((values, cycle)),
    )

    assert controller._commit_waiting_transition(next_cycle, actions) is False
    assert controller.active_cycle is current
    assert executed == []
    assert controller._storage_fault_active is True


def test_storage_fault_suppresses_strategy_work_but_snapshot_exposes_health(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "fault-snapshot.sqlite")
    )
    controller._storage_fault_active = True
    controller._storage_fault_message = "locked"
    controller._storage_fault_operation = "upsert"
    controller._storage_fault_started_at = "2026-07-27T10:03:24+00:00"
    controller._storage_fault_started_monotonic = time.monotonic() - 5.0
    controller.connected = True
    monkeypatch.setattr(
        controller.adapter,
        "is_connected",
        lambda: (_ for _ in ()).throw(AssertionError("strategy must not touch broker")),
    )
    controller._run_strategy_cycle()

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(controller.signals.snapshot_updated, "emit", emitted.append)
    controller.emit_snapshot(force=True, refresh_database=False)
    assert emitted[-1]["storage_fault"]["active"] is True
    assert emitted[-1]["worker_health"]["snapshot_sequence"] >= 1
    assert emitted[-1]["trading_status"]["summary"] == "Storage fault"


def test_storage_fault_pumps_only_broker_transport_and_does_not_apply_callbacks(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "transport-only.sqlite")
    )
    events: list[Any] = []
    controller.connected = True
    controller._storage_fault_active = True
    controller.adapter = SimpleNamespace(
        ib=object(),
        is_connected=lambda: True,
        process_events=lambda timeout=0.0: events.append(("process", timeout)),
        connectivity_status=lambda: {
            "local_connected": True,
            "upstream_connected": True,
            "state": "connected",
            "message": "connected",
        },
        drain_broker_events=lambda: (_ for _ in ()).throw(
            AssertionError("broker callbacks must not be applied without SQLite")
        ),
    )

    assert controller._run_broker_cycle(process_timeout=0.0) is False
    assert events == [("process", 0.0)]
    assert controller._broker_connectivity["upstream_connected"] is True


def test_storage_fault_blocks_all_strategy_broker_actions(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "action-block.sqlite")
    )
    cycle = StrategyEngine.start_cycle(
        permissive_strategy(ticker="AAPL"),
        1,
        "SIM",
        100.0,
        0.0,
    )
    controller.active_cycle = cycle
    controller._storage_fault_active = True
    controller._storage_fault_message = "database is locked"
    broker_calls: list[Any] = []
    controller.adapter = SimpleNamespace(
        cancel_order=lambda *args, **kwargs: broker_calls.append((args, kwargs)),
    )
    action = SimpleNamespace(
        action_type="CANCEL_ORDER",
        payload={"order_ref": "IBKRBOT|AAPL|1", "order_id": 10},
    )

    controller._execute_actions([action], cycle)

    assert broker_calls == []
    assert "blocked" in controller.status.lower()


def test_unhandled_worker_exception_writes_plain_traceback_and_sets_failure(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "worker-exit.sqlite")
    )
    monkeypatch.setattr(
        controller,
        "_run_database_cycle",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fatal cadence")),
    )
    controller._thread_main()

    assert "fatal cadence" in controller._worker_failure_message
    assert controller._shutdown_complete.is_set()
    assert "Unhandled exception terminated" in emergency_log_path(tmp_path).read_text(
        encoding="utf-8"
    )


def _watchdog_request_for_cycle(cycle: CycleState) -> dict[str, Any]:
    return {
        "auto_resume": True,
        "was_monitoring_active": True,
        "startup_resume_required": False,
        "recovery_required": False,
        "expected_cycle_id": cycle.id,
        "expected_cycle_stage": cycle.stage.value,
        "ticker": cycle.ticker,
        "con_id": cycle.con_id,
        "order_refs": {
            "buy": cycle.buy_order_ref,
            "protective_sell": cycle.protective_sell_order_ref,
            "sell": cycle.sell_order_ref,
        },
        "expected_cycle_signature": recovery_cycle_signature(cycle),
        "reason": "test watchdog restart",
    }


@pytest.mark.parametrize(
    ("snapshot_update", "field"),
    [
        ({"startup_resume_required": True}, "startup_resume_required"),
        ({"recovery_required": True}, "recovery_required"),
        ({"active_cycle": {"recovery_required": True}}, "recovery_required"),
    ],
)
def test_restart_request_never_auto_resumes_a_cycle_that_was_not_actively_monitored(
    tmp_path: Path,
    snapshot_update: dict[str, Any],
    field: str,
) -> None:
    cycle = {
        "id": "cycle-1",
        "stage": Stage.WAIT_RISE_TRIGGER.value,
        "ticker": "AAPL",
        "con_id": 123,
    }
    snapshot: dict[str, Any] = {
        "active_cycle": cycle,
        "startup_resume_required": False,
        "recovery_required": False,
    }
    for key, value in snapshot_update.items():
        if key == "active_cycle":
            cycle.update(value)
        else:
            snapshot[key] = value
    request = create_watchdog_restart_request(
        f"token-{field}",
        snapshot,
        "test",
        base_dir=tmp_path,
    )
    assert request["auto_resume"] is False
    assert request["was_monitoring_active"] is False


def test_restart_history_failure_is_fail_closed_and_request_can_be_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = create_watchdog_restart_request(
        "cleanup-token",
        {
            "active_cycle": {
                "id": "cycle-1",
                "stage": Stage.WAIT_RISE_TRIGGER.value,
            },
            "startup_resume_required": False,
            "recovery_required": False,
        },
        "test",
        base_dir=tmp_path,
    )
    assert request["auto_resume"] is True
    monkeypatch.setattr(
        "app.watchdog._atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert record_watchdog_restart_attempt(base_dir=tmp_path, reason="test") is False
    assert discard_watchdog_restart_request(
        "cleanup-token",
        base_dir=tmp_path,
    ) is True
    assert consume_watchdog_restart_request(
        "cleanup-token",
        base_dir=tmp_path,
    ) is None


def test_watchdog_auto_recovery_rejects_mismatched_cycle_without_connecting(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "mismatch.sqlite")
    )
    cycle = StrategyEngine.start_cycle(
        permissive_strategy(ticker="AAPL"), 1, "SIM", 100.0, 0.0
    )
    controller.storage.upsert_cycle(cycle)
    controller.active_cycle = cycle
    request = _watchdog_request_for_cycle(cycle)
    request["expected_cycle_id"] = "another-cycle"

    controller._prepare_watchdog_auto_recovery(request)

    assert controller._watchdog_auto_recovery_pending is False
    assert controller._recovery_required is True
    assert "automatic strategy recovery was blocked" in controller.status


def test_watchdog_auto_recovery_rejects_changed_broker_relevant_signature(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "signature-mismatch.sqlite")
    )
    cycle = StrategyEngine.start_cycle(
        permissive_strategy(ticker="AAPL"), 1, "SIM", 100.0, 0.0
    )
    controller.storage.upsert_cycle(cycle)
    request = create_watchdog_restart_request(
        "signature-token",
        {"active_cycle": cycle.snapshot()},
        "test",
        base_dir=tmp_path,
    )

    changed = CycleState.from_dict(cycle.to_dict())
    changed.buy_order_ref = "IBKR|unexpected-order"
    controller.storage.upsert_cycle(changed)
    controller.active_cycle = changed
    controller._prepare_watchdog_auto_recovery(request)

    assert controller._watchdog_auto_recovery_pending is False
    assert controller._recovery_required is True
    assert "broker-relevant persisted cycle facts changed" in controller.status


def test_watchdog_auto_recovery_uses_existing_start_path_for_exact_cycle(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = DeterministicBrokerAdapter(ticker="AAPL", con_id=265598)
    settings = permissive_strategy(ticker="AAPL")
    settings.contract_con_id = 265598
    controller = make_controller(
        controller_module,
        tmp_path / "exact.sqlite",
        broker,
        settings,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.con_id = 265598
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.quantity = 10
    cycle.avg_buy_price = 99.0
    controller.storage.upsert_cycle(cycle)
    controller.active_cycle = cycle
    calls: list[str] = []

    def fake_start(recovered_settings: Any) -> None:
        calls.append(recovered_settings.normalized_ticker())
        controller.active_cycle = cycle
        controller._startup_resume_required = False
        controller._recovery_required = False

    monkeypatch.setattr(controller, "_start_strategy", fake_start)
    controller._prepare_watchdog_auto_recovery(_watchdog_request_for_cycle(cycle))

    assert calls == ["AAPL"]
    assert controller.active_cycle.id == cycle.id
    assert controller._watchdog_auto_recovery_pending is False
    assert "monitoring resumed" in controller.status


def test_gui_watchdog_override_advances_age_and_invalidates_rth() -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        window = object.__new__(gui.MainWindow)
        window._watchdog_auto_restart_enabled = True
        snapshot = {
            "connected": True,
            "broker_connectivity": {
                "local_connected": True,
                "upstream_connected": True,
            },
            "price_snapshot": {
                "api_data_age_seconds": 27.4,
                "rth_open": False,
                "rth_status": {"is_open": False, "message": "opens later"},
            },
        }
        override = gui.MainWindow._watchdog_override_snapshot(
            window,
            snapshot,
            message="WORKER UNRESPONSIVE",
            elapsed_seconds=60.0,
            state="risk",
        )

        assert override["connected"] is False
        assert override["price_snapshot"]["api_data_age_seconds"] == pytest.approx(87.4)
        assert override["price_snapshot"]["strategy_price_usable"] is False
        assert override["price_snapshot"]["rth_open"] is None
        assert override["trading_status"]["state"] == "risk"


def test_gui_storage_fault_restarts_only_after_write_probe_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        window = object.__new__(gui.MainWindow)
        now = time.monotonic()
        window._watchdog_shutdown_expected = False
        window._watchdog_started_monotonic = now - 100.0
        window._watchdog_last_snapshot_monotonic = now
        window._watchdog_last_snapshot_sequence = 10
        window._watchdog_auto_restart_enabled = True
        window._watchdog_restart_requested = False
        window._watchdog_state = "healthy"
        window.current_snapshot = {}
        window.controller = SimpleNamespace(worker_is_alive=lambda: True)
        window._watchdog_restart_cooldown_text = lambda: ""
        window._render_watchdog_override = lambda snapshot: None
        restart_reasons: list[str] = []
        window._request_watchdog_restart = lambda reason: restart_reasons.append(reason) or True
        window._watchdog_last_worker_snapshot = {
            "storage_fault": {
                "active": True,
                "elapsed_seconds": 100.0,
                "restart_recommended": False,
                "operation": "upsert",
                "message": "database is locked",
            },
            "price_snapshot": {},
        }
        monkeypatch.setattr(gui.time, "monotonic", lambda: now)

        rendered: list[dict[str, Any]] = []
        window._render_watchdog_override = rendered.append
        gui.MainWindow._check_worker_watchdog(window)
        assert restart_reasons == []
        assert rendered[-1]["trading_status"]["summary"] == "Storage fault"

        window._watchdog_last_worker_snapshot["storage_fault"][
            "restart_recommended"
        ] = True
        gui.MainWindow._check_worker_watchdog(window)
        assert len(restart_reasons) == 1
        assert "write access recovered" in restart_reasons[0]


def test_gui_storage_fault_does_not_mask_a_dead_or_hard_stalled_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        window = object.__new__(gui.MainWindow)
        now = time.monotonic()
        window._watchdog_shutdown_expected = False
        window._watchdog_started_monotonic = now - 100.0
        window._watchdog_last_snapshot_monotonic = (
            now - gui.WATCHDOG_AUTO_RESTART_SECONDS - 1.0
        )
        window._watchdog_last_snapshot_sequence = 10
        window._watchdog_auto_restart_enabled = True
        window._watchdog_restart_requested = False
        window._watchdog_state = "healthy"
        window.current_snapshot = {}
        window.controller = SimpleNamespace(worker_is_alive=lambda: True)
        window._watchdog_restart_cooldown_text = lambda: ""
        window._render_watchdog_override = lambda snapshot: None
        restart_reasons: list[str] = []
        window._request_watchdog_restart = (
            lambda reason: restart_reasons.append(reason) or True
        )
        window._watchdog_last_worker_snapshot = {
            "storage_fault": {
                "active": True,
                "elapsed_seconds": 100.0,
                "restart_recommended": False,
                "operation": "upsert",
                "message": "database is locked",
            },
            "price_snapshot": {},
        }
        monkeypatch.setattr(gui.time, "monotonic", lambda: now)

        gui.MainWindow._check_worker_watchdog(window)

        assert len(restart_reasons) == 1
        assert "unresponsive while SQLite was unavailable" in restart_reasons[0]
        assert window._watchdog_state == "storage_fault_unresponsive"


def test_main_watchdog_restart_releases_lock_before_process_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    events: list[Any] = []

    class App:
        def exec(self) -> int:
            return main_module.WATCHDOG_RESTART_EXIT_CODE

    class Window:
        def __init__(self, controller: Any) -> None:
            self.controller = controller

        def show(self) -> None:
            events.append("show")

        def watchdog_restart_token(self) -> str:
            return "one-time-token"

    lock = SimpleNamespace(
        acquire=lambda: events.append("acquire"),
        release=lambda: events.append("release"),
    )
    controller = SimpleNamespace(
        shutdown=lambda: events.append("shutdown"),
        resume_after_watchdog_restart=lambda request: events.append(("resume", request)),
    )
    monkeypatch.setattr(main_module, "QApplication", lambda argv: App())
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda app: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda app: None)
    monkeypatch.setattr(main_module, "_install_system_theme_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "_install_session_shutdown_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)
    monkeypatch.setattr(
        main_module,
        "_replace_with_watchdog_process",
        lambda token, argv: events.append(("replace", token, list(argv))),
    )
    monkeypatch.setattr(sys, "argv", ["main.py"])

    assert main_module.main() == 0
    assert events.index("release") < next(
        index for index, item in enumerate(events) if isinstance(item, tuple) and item[0] == "replace"
    )
    assert events[-1][0:2] == ("replace", "one-time-token")


def test_main_replacement_failure_discards_handoff_without_logging_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    events: list[Any] = []
    diagnostics: list[dict[str, Any]] = []

    class App:
        def exec(self) -> int:
            return main_module.WATCHDOG_RESTART_EXIT_CODE

    class Window:
        def __init__(self, controller: Any) -> None:
            self.controller = controller

        def show(self) -> None:
            pass

        def watchdog_restart_token(self) -> str:
            return "secret-restart-token"

    lock = SimpleNamespace(acquire=lambda: None, release=lambda: None)
    controller = SimpleNamespace(shutdown=lambda: None)
    monkeypatch.setattr(main_module, "QApplication", lambda argv: App())
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda app: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda app: None)
    monkeypatch.setattr(main_module, "_install_system_theme_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "_install_session_shutdown_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)
    monkeypatch.setattr(
        main_module,
        "_replace_with_watchdog_process",
        lambda token, argv: (_ for _ in ()).throw(OSError("exec failed")),
    )
    monkeypatch.setattr(
        main_module,
        "discard_watchdog_restart_request",
        lambda token, **kwargs: events.append(("discard", token)) or True,
    )
    monkeypatch.setattr(
        main_module,
        "append_emergency_log",
        lambda message, **kwargs: diagnostics.append({"message": message, **kwargs}),
    )
    monkeypatch.setattr(sys, "argv", ["main.py"])

    assert main_module.main() == 4
    assert events == [("discard", "secret-restart-token")]
    serialized = json.dumps(diagnostics, default=str)
    assert "secret-restart-token" not in serialized
    assert '"token_present": true' in serialized.lower()


def test_main_queues_authenticated_recovery_before_window_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    events: list[Any] = []
    request = {"expected_cycle_id": "cycle-1", "auto_resume": True}

    class App:
        def exec(self) -> int:
            return 0

    class Window:
        def __init__(self, controller: Any) -> None:
            events.append("window")

        def show(self) -> None:
            events.append("show")

    controller = SimpleNamespace(
        resume_after_watchdog_restart=lambda value: events.append(("resume", value)),
        shutdown=lambda: events.append("shutdown"),
    )
    lock = SimpleNamespace(acquire=lambda: None, release=lambda: events.append("release"))
    monkeypatch.setattr(main_module, "QApplication", lambda argv: App())
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda app: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda app: None)
    monkeypatch.setattr(main_module, "_install_system_theme_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "_install_session_shutdown_hook", lambda app, window: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)
    monkeypatch.setattr(main_module, "consume_watchdog_restart_request", lambda *args, **kwargs: request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--watchdog-recovery-token=valid"],
    )

    assert main_module.main() == 0
    assert events.index(("resume", request)) < events.index("window")


def test_main_private_restart_token_is_removed_before_qt_parsing() -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")
    cleaned, token = main_module._split_watchdog_recovery_argument(
        ["main.py", "--style=fusion", "--watchdog-recovery-token=abc123"]
    )
    assert cleaned == ["main.py", "--style=fusion"]
    assert token == "abc123"


def test_audit_bundle_includes_watchdog_diagnostics_and_redacts_token(
    tmp_path: Path,
) -> None:
    storage = BotStorage(tmp_path / "audit.sqlite")
    reports = storage.debug_reports_dir()
    (reports / "worker_emergency.log").write_text("worker stack\n", encoding="utf-8")
    (reports / "watchdog_restart_history.json").write_text(
        '{"attempts": [1.0]}\n',
        encoding="utf-8",
    )
    request_path = watchdog_request_path(tmp_path)
    request_path.write_text(
        json.dumps({"token": "secret", "expected_cycle_id": "cycle-1"}),
        encoding="utf-8",
    )

    bundle = storage.create_audit_export_bundle(
        target_dir=tmp_path / "exports",
        snapshot={"status": "test"},
    )
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        assert "debug_reports/worker_emergency.log" in names
        assert "debug_reports/watchdog_restart_history.json" in names
        redacted = json.loads(
            archive.read(
                "debug_reports/watchdog_restart_request_redacted.json"
            ).decode("utf-8")
        )
    assert redacted["token"] == "[redacted]"
    assert redacted["expected_cycle_id"] == "cycle-1"


def test_callable_gate_enters_remaining_watchdog_controller_storage_paths(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / "callable-controller.sqlite")
    )
    request = {"auto_resume": False, "reason": "callable coverage"}
    controller.resume_after_watchdog_restart(request)
    queued_name, queued_payload = controller._commands.get_nowait()
    assert queued_name == "WATCHDOG_AUTO_RECOVER"
    assert queued_payload["request"] == request

    controller._storage_fault_active = True
    controller._storage_fault_started_at = "2026-07-27T10:03:24+00:00"
    controller._storage_fault_started_monotonic = time.monotonic() - 4.0
    controller._storage_fault_last_probe_monotonic = 0.0
    monkeypatch.setattr(controller.storage, "probe_writable", lambda timeout_seconds=0.25: True)
    controller._probe_storage_fault_if_due()
    assert controller._storage_fault_probe_succeeded is True
    assert controller._storage_fault_restart_recommended is True

    storage = BotStorage(tmp_path / "callable-storage.sqlite")
    assert storage.cycle_audit_details("") == {
        "cycle": None,
        "orders": [],
        "executions": [],
        "events": [],
        "decision_events": [],
    }

    import app.watchdog as watchdog_module

    assert watchdog_module._json_default(Stage.WAIT_RISE_TRIGGER) == Stage.WAIT_RISE_TRIGGER.value


def test_callable_gate_enters_remaining_gui_watchdog_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT) as gui:
        window = object.__new__(gui.MainWindow)
        window._watchdog_restart_token = "token-value"
        window.controller = SimpleNamespace(db_path=tmp_path / "bot.sqlite")
        assert gui.MainWindow.watchdog_restart_token(window) == "token-value"
        assert gui.MainWindow._watchdog_base_dir(window) == tmp_path

        labels: list[str] = []
        window.connection_status = SimpleNamespace(
            text=lambda: "old",
            setText=labels.append,
        )
        rendered: list[Any] = []
        window.live_status_bar = SimpleNamespace(update_data=rendered.append)
        window._update_command_bar_states = lambda snapshot: rendered.append(("commands", snapshot))
        disabled: list[bool] = []
        window.command_step_buttons = {
            "start": SimpleNamespace(setEnabled=disabled.append),
        }
        gui.MainWindow._render_watchdog_override(window, {"status": "stale"})
        assert labels == ["stale"]
        assert disabled == [False]

        window._watchdog_auto_restart_enabled = True
        window._watchdog_base_dir = lambda: tmp_path
        monkeypatch.setattr(gui, "watchdog_restart_delay_seconds", lambda **kwargs: 0.0)
        assert "armed" in gui.MainWindow._watchdog_restart_cooldown_text(window)

        window._watchdog_restart_requested = False
        window._watchdog_last_worker_snapshot = {"status": "last", "active_cycle": None}
        window.current_snapshot = {}
        window._watchdog_last_snapshot_sequence = 7
        window._worker_watchdog_timer = SimpleNamespace(stop=lambda: None)
        assert gui.MainWindow._request_watchdog_restart(window, "test worker stall") is True
        assert window._watchdog_restart_requested is True
        assert window._watchdog_restart_token

        healthy = object.__new__(gui.MainWindow)
        healthy._watchdog_shutdown_expected = False
        healthy._watchdog_started_monotonic = time.monotonic() - 1.0
        healthy._watchdog_last_snapshot_monotonic = time.monotonic()
        healthy._watchdog_last_worker_snapshot = {}
        healthy.current_snapshot = {}
        healthy.controller = SimpleNamespace(worker_is_alive=lambda: True)
        healthy._watchdog_state = "starting"
        healthy._watchdog_deferred_restart_until = 1.0
        healthy._watchdog_deferred_reason = "old"
        healthy._watchdog_base_dir = lambda: tmp_path
        gui.MainWindow._check_worker_watchdog(healthy)
        assert healthy._watchdog_state == "healthy"
        assert healthy._watchdog_deferred_reason == ""


def test_callable_gate_enters_main_replacement_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    monkeypatch.setattr(main_module.sys, "frozen", False, raising=False)
    argv = main_module._watchdog_replacement_argv(
        "abc123",
        ["main.py", "--style=fusion"],
    )
    assert argv[0] == sys.executable
    assert argv[-1] == "--watchdog-recovery-token=abc123"

    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        main_module.os,
        "execv",
        lambda executable, replacement_argv: calls.append(
            ("execv", list(replacement_argv))
        ),
    )
    monkeypatch.setattr(
        main_module.subprocess,
        "Popen",
        lambda replacement_argv, **kwargs: calls.append(
            ("popen", list(replacement_argv))
        ),
    )
    main_module._replace_with_watchdog_process(
        "abc123",
        ["main.py", "--style=fusion"],
    )
    assert calls
    # os.exec* applies no argv quoting on Windows, so the fixed relaunch uses a
    # properly quoted subprocess.Popen there and keeps atomic execv on POSIX.
    assert calls[0][0] == ("popen" if main_module.os.name == "nt" else "execv")
    assert calls[0][1][-1] == "--watchdog-recovery-token=abc123"
