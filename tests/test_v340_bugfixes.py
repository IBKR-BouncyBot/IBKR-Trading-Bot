"""v3.4.0 local bug-fix review and regression coverage."""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import ib_platform, lockfile
from app.ib_adapter import IbAsyncTwsAdapter, PolledOrderState
from app.market_data_capture import MarketDataCaptureManager, PendingTradeCapture
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from app.watchdog import (
    WATCHDOG_REQUEST_SCHEMA_VERSION,
    create_watchdog_restart_request,
    discard_expired_watchdog_restart_request,
    watchdog_request_path,
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


def _protective_cycle(controller: Any, *, quantity: int = 100) -> CycleState:
    settings = permissive_strategy(auto_repeat=False)
    settings.protective_sell_enabled = True
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.buy_filled_qty = quantity
    cycle.quantity = quantity
    cycle.avg_buy_price = 100.0
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|PROTECTIVE_SELL_TRAIL"
    cycle.protective_sell_order_id = 77
    cycle.protective_sell_perm_id = 88
    cycle.protective_sell_status = "Submitted"
    controller.strategy = settings
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return cycle


def _protective_poll(
    cycle: CycleState,
    *,
    status: str,
    filled: int,
    remaining: int,
    price: float = 95.0,
) -> PolledOrderState:
    return PolledOrderState(
        order_ref=str(cycle.protective_sell_order_ref),
        order_id=cycle.protective_sell_order_id,
        perm_id=cycle.protective_sell_perm_id,
        status=status,
        filled=filled,
        remaining=remaining,
        avg_fill_price=price,
        commission=0.5,
        executions=[],
        raw={"status": status, "filled": filled, "remaining": remaining},
    )


def test_protective_sell_live_partial_is_persisted_without_completing(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "partial.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)

    handled = controller._handle_protective_sell_order_poll(
        cycle,
        _protective_poll(cycle, status="Submitted", filled=40, remaining=60),
    )

    assert handled is True
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.protective_sell_filled_qty == 40
    totals = controller.storage.get_execution_totals(cycle.id, "PROTECTIVE_SELL")
    assert totals["shares"] == pytest.approx(40.0)


def test_protective_sell_terminal_partial_fails_closed_and_is_audited(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "terminal.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)

    handled = controller._handle_protective_sell_order_poll(
        cycle,
        _protective_poll(cycle, status="Cancelled", filled=40, remaining=60),
    )

    assert handled is True
    assert controller.active_cycle.stage == Stage.ERROR
    assert controller.active_cycle.protective_sell_filled_qty == 40
    assert "60 shares remain" in str(controller.active_cycle.error_message)
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "PROTECTIVE_SELL_PARTIAL_TERMINAL" for row in events)


@pytest.mark.parametrize("status", ["Filled", "Cancelled"])
def test_protective_sell_terminal_partial_with_zero_reported_remainder_never_completes(
    controller_module: Any,
    tmp_path: Path,
    status: str,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / f"mismatch-{status}.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)

    controller._handle_protective_sell_order_poll(
        cycle,
        _protective_poll(cycle, status=status, filled=40, remaining=0),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert "60 shares remain" in str(controller.active_cycle.error_message)
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "PROTECTIVE_SELL_PARTIAL_TERMINAL" for row in events)


@pytest.mark.parametrize(
    ("status", "filled", "remaining"),
    [("Filled", 120, 0), ("Submitted", 100, 0)],
)
def test_protective_sell_inconsistent_quantity_never_completes(
    controller_module: Any,
    tmp_path: Path,
    status: str,
    filled: int,
    remaining: int,
) -> None:
    controller = controller_module.TradingController(
        storage=BotStorage(tmp_path / f"quantity-mismatch-{status}-{filled}.sqlite")
    )
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)
    if status == "Submitted":
        cycle.buy_filled_qty = 120
        cycle.quantity = 120
        controller.storage.upsert_cycle(cycle)

    controller._handle_protective_sell_order_poll(
        cycle,
        _protective_poll(cycle, status=status, filled=filled, remaining=remaining),
    )

    assert controller.active_cycle.stage == Stage.ERROR
    assert "does not prove" in str(controller.active_cycle.error_message)
    events = controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    assert any(row["event_type"] == "PROTECTIVE_SELL_QUANTITY_MISMATCH" for row in events)


def test_protective_sell_exact_full_quantity_completes_cycle(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "full.sqlite"))
    controller.emit_snapshot = lambda *args, **kwargs: None
    cycle = _protective_cycle(controller)

    controller._handle_protective_sell_order_poll(
        cycle,
        _protective_poll(cycle, status="Filled", filled=100, remaining=0),
    )

    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.active_cycle.sell_filled_qty == 100


def test_reconnect_recovery_routes_terminal_protective_partial_to_error(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy(auto_repeat=False)
    settings.protective_sell_enabled = True
    controller = make_controller(controller_module, tmp_path / "recover.sqlite", broker, settings)
    cycle = _protective_cycle(controller)
    broker.external_position = float(cycle.buy_filled_qty)
    broker.orders[str(cycle.protective_sell_order_ref)] = _protective_poll(
        cycle,
        status="Cancelled",
        filled=40,
        remaining=60,
    )

    controller._recover_after_connect()

    assert controller.active_cycle.stage == Stage.ERROR
    assert "60 shares remain" in str(controller.active_cycle.error_message)


def test_missing_poll_after_protective_partial_blocks_price_exit_logic(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = make_controller(
        controller_module,
        tmp_path / "protective-partial-missing-poll.sqlite",
        broker,
        permissive_strategy(auto_repeat=False),
    )
    cycle = _protective_cycle(controller)
    cycle.protective_sell_filled_qty = 40
    controller.storage.upsert_cycle(cycle)
    broker.connected = True
    broker.upstream_connected = True
    monkeypatch.setattr(broker, "poll_order", lambda order_ref: None)
    monkeypatch.setattr(controller, "_poll_price_if_due", lambda *args, **kwargs: (True, 150.0))
    price_advances: list[object] = []
    monkeypatch.setattr(
        controller,
        "_advance_waiting_cycle_from_price",
        lambda *args, **kwargs: price_advances.append(object()),
    )

    controller._run_strategy_cycle()

    assert price_advances == []
    assert controller.active_cycle.stage == Stage.WAIT_RISE_TRIGGER


@pytest.mark.parametrize("side", ["SELL", "PROTECTIVE_SELL"])
@pytest.mark.parametrize("recovered_qty", [40, 120])
def test_execution_recovery_requires_exact_exit_quantity(
    controller_module: Any,
    tmp_path: Path,
    side: str,
    recovered_qty: int,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = make_controller(
        controller_module,
        tmp_path / f"execution-{side}-{recovered_qty}.sqlite",
        broker,
        permissive_strategy(auto_repeat=False),
    )
    cycle = _protective_cycle(controller)
    if side == "SELL":
        cycle.stage = Stage.SELL_TRAIL_ACTIVE
        cycle.sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|SELL_TRAIL"
        cycle.sell_order_id = 101
        cycle.sell_perm_id = 201
        order_ref = cycle.sell_order_ref
        order_id = cycle.sell_order_id
        perm_id = cycle.sell_perm_id
    else:
        order_ref = cycle.protective_sell_order_ref
        order_id = cycle.protective_sell_order_id
        perm_id = cycle.protective_sell_perm_id
    controller.storage.upsert_cycle(cycle)
    broker.executions = [
        {
            "execution_id": f"{side}-{recovered_qty}",
            "side": "SLD",
            "shares": recovered_qty,
            "price": 95.0,
            "avg_price": 95.0,
            "commission": 0.5,
            "currency": "USD",
            "order_ref": order_ref,
            "order_id": order_id,
            "perm_id": perm_id,
            "executed_at": "2026-07-27T12:00:00+00:00",
        }
    ]

    if side == "SELL":
        recovered = controller._recover_sell_from_executions(cycle)
    else:
        recovered = controller._recover_protective_sell_from_executions(cycle)

    assert recovered is None
    assert controller.active_cycle.stage != Stage.CYCLE_COMPLETE


def test_checkpoint_fallback_uses_only_two_matching_serializations(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "checkpoint.sqlite"))
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "SIM", 100.0, 0.0)
    controller.active_cycle = cycle
    values = iter([100.0, 101.0, 102.0, 102.0])
    original_to_dict = CycleState.to_dict

    def changing_to_dict(self: CycleState) -> dict[str, Any]:
        data = original_to_dict(self)
        if self is cycle:
            data["last_price"] = next(values, 102.0)
        return data

    captured: list[CycleState | None] = []
    monkeypatch.setattr(CycleState, "to_dict", changing_to_dict)
    monkeypatch.setattr(
        controller.storage,
        "save_resume_checkpoint",
        lambda connection, strategy, saved_cycle, **kwargs: captured.append(saved_cycle),
    )

    assert controller.checkpoint_for_resume_later(
        ConnectionSettings(),
        StrategySettings(ticker="AAPL"),
        reason="test",
    ) is True
    assert captured and captured[0] is not None
    assert captured[0].last_price == pytest.approx(102.0)


def test_checkpoint_fallback_refuses_continuously_torn_cycle(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "checkpoint-torn.sqlite"))
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "SIM", 100.0, 0.0)
    controller.active_cycle = cycle
    original_to_dict = CycleState.to_dict
    counter = 0

    def never_stable(self: CycleState) -> dict[str, Any]:
        nonlocal counter
        data = original_to_dict(self)
        if self is cycle:
            counter += 1
            data["last_price"] = float(counter)
        return data

    saved: list[object] = []
    monkeypatch.setattr(CycleState, "to_dict", never_stable)
    monkeypatch.setattr(
        controller.storage,
        "save_resume_checkpoint",
        lambda *args, **kwargs: saved.append(object()),
    )

    assert controller.checkpoint_for_resume_later(
        ConnectionSettings(),
        StrategySettings(ticker="AAPL"),
        reason="test",
    ) is False
    assert saved == []


def test_expired_watchdog_cleanup_preserves_fresh_request_and_removes_bad_files(tmp_path: Path) -> None:
    create_watchdog_restart_request(
        "fresh-token",
        {"active_cycle": None},
        "test",
        base_dir=tmp_path,
        now_epoch=1_000.0,
    )
    assert discard_expired_watchdog_restart_request(base_dir=tmp_path, now_epoch=1_001.0) is False
    assert watchdog_request_path(tmp_path).exists()

    assert discard_expired_watchdog_restart_request(
        base_dir=tmp_path,
        now_epoch=2_000.0,
        max_age_seconds=10.0,
    ) is True
    assert not watchdog_request_path(tmp_path).exists()

    path = watchdog_request_path(tmp_path)
    path.write_text(json.dumps({"schema_version": WATCHDOG_REQUEST_SCHEMA_VERSION, "created_epoch": 3_000.0}), encoding="utf-8")
    assert discard_expired_watchdog_restart_request(base_dir=tmp_path, now_epoch=3_001.0) is True
    assert not path.exists()

    path.write_text("not-json", encoding="utf-8")
    assert discard_expired_watchdog_restart_request(base_dir=tmp_path, now_epoch=3_002.0) is True
    assert not path.exists()


def test_windows_watchdog_relaunch_uses_argument_list_for_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    script = tmp_path / "folder with spaces" / "main.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(main_module, "__file__", str(script))
    monkeypatch.setattr(main_module.sys, "frozen", False, raising=False)
    monkeypatch.setattr(main_module.sys, "executable", r"C:\Program Files\Python311\python.exe")
    replacement_argv = main_module._watchdog_replacement_argv(
        "token",
        [str(script), "--style=fusion"],
    )
    monkeypatch.setattr(main_module, "_watchdog_replacement_argv", lambda token, qt_argv: list(replacement_argv))
    host_os_name = os.name
    monkeypatch.setattr(
        main_module,
        "os",
        SimpleNamespace(
            name="nt",
            execv=lambda *args: pytest.fail("Windows must not call execv"),
        ),
    )
    monkeypatch.setattr(
        main_module.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((list(argv), dict(kwargs))),
    )

    main_module._replace_with_watchdog_process("token", [str(script), "--style=fusion"])

    assert calls == [
        (
            [
                r"C:\Program Files\Python311\python.exe",
                str(script.resolve()),
                "--style=fusion",
                "--watchdog-recovery-token=token",
            ],
            {"close_fds": True},
        )
    ]
    assert os.name == host_os_name


def test_posix_watchdog_relaunch_keeps_atomic_execv(monkeypatch: pytest.MonkeyPatch) -> None:
    with imported_gui_with_stubs(ROOT):
        sys.modules.pop("main", None)
        main_module = importlib.import_module("main")

    calls: list[tuple[str, list[str]]] = []
    host_os_name = os.name
    monkeypatch.setattr(
        main_module,
        "os",
        SimpleNamespace(
            name="posix",
            execv=lambda exe, argv: calls.append((str(exe), list(argv))),
        ),
    )
    monkeypatch.setattr(main_module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("POSIX must not call Popen"))

    main_module._replace_with_watchdog_process("token", ["main.py"])

    assert calls and calls[0][0] == main_module.sys.executable
    assert calls[0][1][-1] == "--watchdog-recovery-token=token"
    assert os.name == host_os_name


def test_windows_pid_probe_uses_last_error_aware_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        def __init__(self, result: Any) -> None:
            self.result = result
            self.restype: Any = None
            self.argtypes: Any = None

        def __call__(self, *args: Any) -> Any:
            return self.result

    kernel = SimpleNamespace(
        OpenProcess=Function(0),
        GetExitCodeProcess=Function(True),
        CloseHandle=Function(True),
    )
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        lockfile.ctypes,
        "WinDLL",
        lambda name, use_last_error=False: (calls.append((name, use_last_error)) or kernel),
        raising=False,
    )
    monkeypatch.setattr(lockfile.ctypes, "get_last_error", lambda: 5, raising=False)

    assert lockfile._pid_is_running_windows(1234) is True
    assert calls == [("kernel32", True)]
    assert kernel.OpenProcess.restype is lockfile.ctypes.c_void_p
    assert kernel.OpenProcess.argtypes is not None


def test_lockfile_write_failure_closes_descriptor_and_removes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.lock"
    closed: list[int] = []
    real_close = os.close

    monkeypatch.setattr(lockfile.os, "write", lambda fd, payload: (_ for _ in ()).throw(OSError("disk full")))

    def tracked_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lockfile.os, "close", tracked_close)
    lock = lockfile.SingleInstanceLock(path)
    with pytest.raises(OSError, match="disk full"):
        lock.acquire()

    assert closed
    assert lock.fd is None
    assert not path.exists()
    assert lockfile._lock_key(path) not in lockfile._ACQUIRED_LOCK_PATHS


def _pending_capture(name: str) -> PendingTradeCapture:
    return PendingTradeCapture(
        event_id=name,
        event_type="TEST",
        event_monotonic=1.0,
        post_window_seconds=0.0,
        ticker="AAPL",
        cycle_id="cycle",
        cycle_number=1,
        order_ref="ref",
        perm_id=1,
        pre_rows=[],
    )


def test_capture_writer_survives_one_write_failure_and_processes_next(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MarketDataCaptureManager(tmp_path, async_writes=True)
    calls = 0

    def write(pending: PendingTradeCapture) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        return tmp_path / f"{pending.event_id}.zip"

    monkeypatch.setattr(manager, "_write_capture", write)
    manager._ensure_writer_thread()
    manager._write_queue.put(_pending_capture("first"))
    manager._write_queue.put(_pending_capture("second"))
    deadline = time.monotonic() + 2.0
    while manager._write_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)

    assert manager._write_queue.unfinished_tasks == 0
    assert manager._writer_thread is not None and manager._writer_thread.is_alive()
    assert manager.completed_files == [tmp_path / "second.zip"]
    manager.shutdown(timeout=1.0)


def test_capture_shutdown_restarts_dead_writer_and_has_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MarketDataCaptureManager(tmp_path, async_writes=True)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    manager._writer_thread = dead
    manager._write_queue.put(_pending_capture("queued"))
    processed: list[str] = []
    monkeypatch.setattr(
        manager,
        "_write_capture",
        lambda pending: (processed.append(pending.event_id) or (tmp_path / f"{pending.event_id}.zip")),
    )

    started = time.monotonic()
    manager.shutdown(timeout=1.0)
    elapsed = time.monotonic() - started

    assert processed == ["queued"]
    assert elapsed < 1.25


def test_capture_shutdown_does_not_wait_forever_for_blocked_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = MarketDataCaptureManager(tmp_path, async_writes=True)
    release = threading.Event()
    entered = threading.Event()

    def blocked_write(pending: PendingTradeCapture) -> Path:
        del pending
        entered.set()
        release.wait(5.0)
        return tmp_path / "blocked.zip"

    monkeypatch.setattr(manager, "_write_capture", blocked_write)
    manager._ensure_writer_thread()
    manager._write_queue.put(_pending_capture("blocked"))
    assert entered.wait(1.0)

    started = time.monotonic()
    manager.shutdown(timeout=0.1)
    elapsed = time.monotonic() - started
    release.set()
    if manager._writer_thread is not None:
        manager._writer_thread.join(timeout=1.0)

    assert elapsed < 0.35


def test_latest_active_cycle_tiebreak_is_deterministic(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "cycles.sqlite")
    settings = StrategySettings(ticker="AAPL")
    older = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    newer = StrategyEngine.start_cycle(settings, 2, "SIM", 100.0, 0.0)
    older.id = "cycle-a"
    newer.id = "cycle-b"
    older.updated_at = newer.updated_at = "2026-07-27T12:00:00+00:00"
    storage.upsert_cycle(older)
    storage.upsert_cycle(newer)

    assert storage.get_latest_active_cycle().id == "cycle-b"

    newer.cycle_number = older.cycle_number
    storage.upsert_cycle(newer)
    assert storage.get_latest_active_cycle().id == "cycle-b"


def _fill(*, timestamp: datetime) -> Any:
    execution = SimpleNamespace(
        side="SLD",
        shares=5,
        price=100.0,
        avgPrice=100.0,
        orderRef="IBKRBOT|AAPL|SELL",
        orderId=10,
        permId=20,
        execId="",
        time=timestamp,
        acctNumber="DU_TEST",
        exchange="SMART",
    )
    return SimpleNamespace(
        execution=execution,
        contract=SimpleNamespace(symbol="AAPL", conId=123, secType="STK", currency="USD"),
        order=SimpleNamespace(orderRef=execution.orderRef),
        commissionReport=SimpleNamespace(commission=0.0, currency="USD"),
        time=timestamp,
    )


def test_execution_fallback_dedup_key_includes_execution_timestamp() -> None:
    first = _fill(timestamp=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc))
    second = _fill(timestamp=datetime(2026, 7, 27, 12, 0, 1, tzinfo=timezone.utc))

    class FakeIB:
        def isConnected(self) -> bool:
            return True

        def fills(self) -> list[Any]:
            return [first, second]

        def reqExecutions(self) -> list[Any]:
            return [first, second]

        def sleep(self, seconds: float) -> None:
            del seconds

    adapter = IbAsyncTwsAdapter()
    adapter.ib = FakeIB()

    rows = adapter.recent_executions()

    assert len(rows) == 2
    assert rows[0]["executed_at"] != rows[1]["executed_at"]


def test_new_cycle_rounds_initial_drop_trigger_to_strategy_precision() -> None:
    settings = StrategySettings(ticker="AAPL", initial_drop_pct=1.2345)
    cycle = CycleState.new(settings, 1, "SIM", 123.456789, 0.0)
    expected = round(123.456789 * (1.0 - 1.2345 / 100.0), 4)
    assert cycle.drop_trigger_price == expected


def test_default_ib_platform_paths_use_single_windows_separators() -> None:
    assert ib_platform._COMMON_TWS_PATHS == (
        r"C:\Jts\tws.exe",
        r"C:\Jts\Trader Workstation\tws.exe",
    )
    assert ib_platform._COMMON_GATEWAY_PATTERNS == (
        r"C:\Jts\ibgateway\*\ibgateway.exe",
        r"C:\Jts\ibgateway\ibgateway.exe",
    )


def test_headless_signal_instance_has_only_one_runtime_definition() -> None:
    source = (ROOT / "app" / "controller.py").read_text(encoding="utf-8")
    assert source.count("class _HeadlessSignalInstance") == 1


def test_v340_release_metadata_and_documentation_are_current() -> None:
    gui = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    current_note = ROOT / "docs" / "V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md"
    archived_note = ROOT / "docs" / "legacy" / "V3_3_0_DARK_MODE_AUDIT_AND_WINDOWS_RELEASE.md"

    assert 'APP_VERSION = "3.6.0"' in gui
    assert 'version = "3.6.0"' in pyproject
    assert '$version = "3.6.0"' in build
    assert "**Current release: v3.6.0**" in readme
    assert "## v3.6.0" in changelog
    assert current_note.is_file()
    assert archived_note.is_file()
    assert not (ROOT / "docs" / archived_note.name).exists()
