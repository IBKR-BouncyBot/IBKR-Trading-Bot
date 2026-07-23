"""Regression coverage for Windows-controlled shutdown resume checkpoints."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import ConnectionSettings, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_controller_headless import _install_qt_stub


def _settings(*, ticker: str = "AAPL", investment: float = 1_000.0) -> StrategySettings:
    return StrategySettings(
        ticker=ticker,
        investment_amount=investment,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        auto_repeat=False,
    )


def test_storage_resume_checkpoint_is_atomic_and_idempotent(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    connection = ConnectionSettings(client_id=42, account="DU123")
    strategy = _settings(investment=2_500.0)
    cycle = StrategyEngine.start_cycle(strategy, 1, connection.account, 100.0, 0.0)

    checkpoint = storage.save_resume_checkpoint(
        connection,
        strategy,
        cycle,
        reason="windows_session_shutdown",
        checkpoint_id="checkpoint-1",
    )
    duplicate = storage.save_resume_checkpoint(
        connection,
        strategy,
        cycle,
        reason="windows_session_shutdown",
        checkpoint_id="checkpoint-1",
    )

    assert duplicate == checkpoint
    assert checkpoint["resume_required"] is True
    assert storage.load_connection_settings().client_id == 42
    assert storage.load_strategy_settings().investment_amount == 2_500.0
    assert storage.get_latest_active_cycle().id == cycle.id  # type: ignore[union-attr]
    assert storage.get_json("last_resume_checkpoint")["checkpoint_id"] == "checkpoint-1"
    matching_events = [
        event
        for event in storage.get_recent_events(20)
        if "Resume checkpoint saved before windows session shutdown" in event["message"]
    ]
    assert len(matching_events) == 1


def test_storage_resume_checkpoint_rolls_back_all_fields_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = BotStorage(tmp_path / "rollback.sqlite")
    storage.save_connection_settings(ConnectionSettings(client_id=1))
    storage.save_strategy_settings(_settings(investment=1_000.0))
    cycle = StrategyEngine.start_cycle(_settings(investment=9_000.0), 1, "", 100.0, 0.0)

    def fail_cycle_write(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(storage, "_upsert_cycle_in_connection", fail_cycle_write)

    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        storage.save_resume_checkpoint(
            ConnectionSettings(client_id=99),
            _settings(investment=9_000.0),
            cycle,
            reason="windows_session_shutdown",
            checkpoint_id="rollback-checkpoint",
        )

    assert storage.load_connection_settings().client_id == 1
    assert storage.load_strategy_settings().investment_amount == 1_000.0
    assert storage.get_json("last_resume_checkpoint") is None
    assert storage.get_latest_active_cycle() is None


def test_controller_checkpoints_through_worker_and_direct_fallback(tmp_path: Path, monkeypatch) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    strategy = _settings()

    worker_storage = BotStorage(tmp_path / "worker.sqlite")
    worker = controller_module.TradingController(storage=worker_storage)
    worker.strategy = strategy
    worker.active_cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)
    worker.start_thread()
    try:
        assert worker.checkpoint_for_resume_later(
            ConnectionSettings(client_id=7),
            strategy,
            reason="windows_session_shutdown",
            timeout=3.0,
        ) is True
    finally:
        worker.shutdown()
    assert worker_storage.get_json("last_resume_checkpoint")["reason"] == "windows_session_shutdown"

    fallback_storage = BotStorage(tmp_path / "fallback.sqlite")
    fallback = controller_module.TradingController(storage=fallback_storage)
    fallback.strategy = strategy
    fallback.active_cycle = StrategyEngine.start_cycle(strategy, 1, "", 101.0, 0.0)
    assert fallback.checkpoint_for_resume_later(
        ConnectionSettings(client_id=8),
        strategy,
        reason="operator_exit_resume_later",
    ) is True
    assert fallback_storage.get_json("last_resume_checkpoint")["reason"] == "operator_exit_resume_later"


def test_worker_acknowledges_durable_checkpoint_before_slow_backup(
    tmp_path: Path, monkeypatch
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "backup-order.sqlite")
    controller = controller_module.TradingController(storage=storage)
    strategy = _settings()
    controller.active_cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)
    backup_started = threading.Event()
    release_backup = threading.Event()

    def slow_backup(reason: str) -> None:
        del reason
        backup_started.set()
        release_backup.wait(3.0)

    monkeypatch.setattr(storage, "backup_database", slow_backup)
    acknowledged = threading.Event()
    result: dict[str, Any] = {}
    controller._commands.put(
        (
            "CHECKPOINT_RESUME_STATE",
            {
                "connection": ConnectionSettings(client_id=17),
                "strategy": strategy,
                "reason": "windows_session_shutdown",
                "checkpoint_id": "backup-order",
                "_checkpoint_result": result,
                "_ack_event": acknowledged,
            },
        )
    )
    worker = threading.Thread(target=controller._drain_commands)
    worker.start()
    try:
        assert acknowledged.wait(1.0) is True
        assert result["ok"] is True
        assert storage.get_json("last_resume_checkpoint")["checkpoint_id"] == "backup-order"
        assert backup_started.wait(1.0) is True
        assert worker.is_alive() is True
    finally:
        release_backup.set()
        worker.join(timeout=2.0)
    assert worker.is_alive() is False


def test_checkpoint_persists_safe_edits_without_market_re_evaluation(
    tmp_path: Path, monkeypatch
) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "no-side-effect.sqlite")
    controller = controller_module.TradingController(storage=storage)
    strategy = _settings()
    cycle = StrategyEngine.start_cycle(strategy, 1, "", 100.0, 0.0)
    cycle.last_price = 94.0
    controller.active_cycle = cycle
    controller.connected = True
    controller.contract = object()
    controller._update_rth_status = lambda contract: {"is_open": True, "message": "open"}
    controller._advance_waiting_cycle_from_price = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("shutdown checkpoint must not re-evaluate price state")
    )
    controller._execute_actions = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("shutdown checkpoint must not submit broker actions")
    )
    monkeypatch.setattr(storage, "backup_database", lambda reason: None)
    result: dict[str, Any] = {}

    controller._handle_command(
        "CHECKPOINT_RESUME_STATE",
        {
            "connection": ConnectionSettings(client_id=31),
            "strategy": strategy,
            "reason": "windows_session_shutdown",
            "checkpoint_id": "no-side-effect",
            "_checkpoint_result": result,
        },
    )

    assert result["ok"] is True
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage.value == "1_WAIT_INITIAL_DROP"
    assert storage.get_json("last_resume_checkpoint")["checkpoint_id"] == "no-side-effect"


def test_main_window_system_shutdown_saves_once_and_accepts_close(
    tmp_path: Path, monkeypatch
) -> None:
    del tmp_path
    with imported_gui_with_stubs(Path.cwd()) as gui:
        calls: list[tuple[str, Any]] = []
        controller = SimpleNamespace(
            connection=ConnectionSettings(),
            strategy=_settings(),
            checkpoint_for_resume_later=lambda connection, strategy, **kwargs: calls.append(
                ("checkpoint", (connection, strategy, kwargs))
            )
            or True,
            save_draft_settings=lambda *args: calls.append(("draft", args)),
            shutdown=lambda: calls.append(("shutdown", None)),
        )
        window = gui.MainWindow.__new__(gui.MainWindow)
        window.controller = controller
        window._autosave_timer = SimpleNamespace(stop=lambda: calls.append(("timer_stop", None)))
        window._connection_from_ui = lambda: ConnectionSettings(client_id=19)
        window._strategy_from_ui = lambda: _settings(investment=3_000.0)
        window._system_shutdown_in_progress = False
        window._last_system_shutdown_session_key = ""
        window._stop_dialog_exit_requested = False
        application = SimpleNamespace(isSavingSession=lambda: True)
        monkeypatch.setattr(gui.QApplication, "instance", staticmethod(lambda: application), raising=False)
        session_manager = SimpleNamespace(sessionKey=lambda: "session-key-1")

        assert window._save_resume_checkpoint("operator_exit_resume_later") is True
        window.handle_system_shutdown(session_manager)
        window.handle_system_shutdown(session_manager)

        accepted: list[bool] = []
        ignored: list[bool] = []
        window.closeEvent(
            SimpleNamespace(
                accept=lambda: accepted.append(True),
                ignore=lambda: ignored.append(True),
            )
        )

    checkpoint_reasons = [entry[1][2]["reason"] for entry in calls if entry[0] == "checkpoint"]
    assert checkpoint_reasons == ["operator_exit_resume_later", "windows_session_shutdown"]
    assert [entry[0] for entry in calls].count("shutdown") == 0
    assert [entry[0] for entry in calls].count("timer_stop") == 0
    assert accepted == [True]
    assert ignored == []


def test_cancelled_system_shutdown_rearms_the_normal_close_path(monkeypatch) -> None:
    with imported_gui_with_stubs(Path.cwd()) as gui:
        calls: list[str] = []
        controller = SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
        window = gui.MainWindow.__new__(gui.MainWindow)
        window.controller = controller
        window.current_snapshot = {"active_cycle": None}
        window._system_shutdown_in_progress = True
        window._stop_dialog_exit_requested = False
        window._visible_tws_open_app_orders = lambda: []
        window._persisted_app_unsold_quantity = lambda cycle: 0.0
        window._save_resume_checkpoint = lambda reason: calls.append(reason) or True
        application = SimpleNamespace(isSavingSession=lambda: False)
        monkeypatch.setattr(gui.QApplication, "instance", staticmethod(lambda: application), raising=False)

        class AcceptedExitDialog:
            selected_action = None
            exit_app_after_action = True

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs

            def exec(self) -> Any:
                return gui.QDialog.Accepted

        monkeypatch.setattr(gui, "StopDialog", AcceptedExitDialog)
        accepted: list[bool] = []
        window.closeEvent(
            SimpleNamespace(
                accept=lambda: accepted.append(True),
                ignore=lambda: calls.append("ignored"),
            )
        )

    assert window._system_shutdown_in_progress is False
    assert calls == ["operator_exit", "shutdown"]
    assert accepted == [True]


def test_reported_ruff_import_boundaries_use_canonical_spacing() -> None:
    boundaries = {
        Path("tests/support/deterministic_broker.py"): (
            "from app.models import APP_ORDER_PREFIX, utc_now_iso",
            "_WORKING_STATUSES",
        ),
        Path("tests/test_accelerated_soak_bounds.py"): (
            "from tests.test_controller_headless import _install_qt_stub",
            "pytestmark",
        ),
        Path("tests/test_crash_restart_and_migration_matrix.py"): (
            "from app.storage import BotStorage",
            "_OLD_CYCLES_SCHEMA",
        ),
    }
    for path, (last_import, first_statement) in boundaries.items():
        source = path.read_text(encoding="utf-8")
        assert f"{last_import}\n\n{first_statement}" in source
        assert f"{last_import}\n\n\n{first_statement}" not in source


def test_v3012_version_and_shutdown_contract_are_documented() -> None:
    gui = Path("app/gui.py").read_text(encoding="utf-8")
    main = Path("main.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in gui
    assert "# BouncyBot - an IBKR Portable Trading Bot " in readme
    assert 'version = "3.1.1"' in pyproject
    assert "commitDataRequest" in main
    assert "handle_system_shutdown" in main
    assert "windows_session_shutdown" in gui
