"""Exit fallback preserves committed orders and refuses conflicting recovery."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ib_adapter import PolledOrderState, QualifiedContract
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings
from app.storage import BotStorage


class ResumeCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v510_checkpoint_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_module = module

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = BotStorage(Path(self.folder.name) / "state.sqlite")
        with patch.object(self.controller_module, "debug_captures_dir", return_value=Path(self.folder.name) / "captures"):
            self.controller = self.controller_module.TradingController(self.storage)
        self.addCleanup(self.controller._market_capture.shutdown)
        self.controller.emit_snapshot = lambda **kwargs: None
        self.connection = ConnectionSettings(account="TEST_ACCOUNT")
        self.strategy = StrategySettings(ticker="TEST", contract_con_id=123, investment_amount=1000)
        self.controller.connection = self.connection
        self.controller.strategy = self.strategy
        self.cycle = CycleState.new(self.strategy, 1, "TEST_ACCOUNT", 100, 0)
        self.controller.active_cycle = self.cycle
        self.storage.upsert_cycle(self.cycle)
        self.storage.save_connection_settings(self.connection)
        self.storage.save_strategy_settings(self.strategy)
        self.order_ref = "IBKRBOT|TEST|CYCLE-000001|checkpoint|BUY_TRAIL"

    def submit_buy(self):
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.buy_order_ref = self.order_ref
        self.cycle.buy_order_id = 7001
        self.cycle.buy_perm_id = 8001
        self.cycle.buy_status = "Submitted"
        self.cycle.touch()
        self.storage.create_order_intent(
            cycle=self.cycle, action="BUY", order_type="TRAIL", order_ref=self.order_ref,
            quantity=10, trailing_percent=1, initial_stop_price=95,
        )
        self.storage.record_order_submission(
            cycle=self.cycle, order_ref=self.order_ref, order_id=7001,
            perm_id=8001, status="Submitted",
        )

    def synchronous_worker(self):
        self.controller._thread_started = True
        self.controller._thread = SimpleNamespace(is_alive=lambda: True)

        def dispatch(command):
            name, payload = command
            if name == "CHECKPOINT_RESUME_STATE":
                self.controller._process_queued_command(name, payload)

        return patch.object(self.controller._commands, "put", side_effect=dispatch)

    def recovery_adapter(self, orders):
        return SimpleNamespace(
            requires_exact_contract_selection=True,
            open_app_orders=lambda: orders,
            qualify_stock=lambda *args, **kwargs: QualifiedContract("TEST", 123, object()),
            managed_accounts=lambda: ["TEST_ACCOUNT"],
            position_size=lambda *args, **kwargs: 0,
            recent_executions=lambda: [],
            cancel_order=Mock(),
            place_trailing_stop=Mock(),
            place_market_order=Mock(),
        )

    def working_buy(self, *, order_ref=None, status="Submitted"):
        return PolledOrderState(
            order_ref or self.order_ref, 7001, 8001, status,
            0, 10 if status == "Submitted" else 0, 0, 0, [], {},
        )

    def test_timeout_fallback_preserves_worker_order_committed_after_copy(self):
        copied = threading.Event()
        committed = threading.Event()
        release_worker = threading.Event()
        failures = []
        saved_before_fallback = []
        original_save = self.storage.save_resume_checkpoint

        def worker_transition():
            try:
                if not copied.wait(5):
                    raise AssertionError("Fallback did not capture a cycle")
                self.submit_buy()
                saved_before_fallback.append(self.storage.get_cycle(self.cycle.id).to_dict())
            except BaseException as exc:
                failures.append(exc)
            finally:
                committed.set()
                release_worker.wait(5)

        def save_after_transition(connection, strategy, cycle, **kwargs):
            self.assertEqual(cycle.stage, Stage.WAIT_INITIAL_DROP)
            copied.set()
            self.assertTrue(committed.wait(5))
            return original_save(connection, strategy, cycle, **kwargs)

        worker = threading.Thread(target=worker_transition)
        self.controller._thread = worker
        self.controller._thread_started = True
        worker.start()
        try:
            with patch.object(self.storage, "save_resume_checkpoint", side_effect=save_after_transition):
                self.assertTrue(self.controller.checkpoint_for_resume_later(
                    self.connection, self.strategy, reason="operator_exit", timeout=0.1,
                ))
        finally:
            release_worker.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.storage.get_cycle(self.cycle.id).to_dict(), saved_before_fallback[0])
        self.assertEqual(self.storage.get_order_for_cycle_ref(self.cycle.id, self.order_ref)["order_id"], 7001)
        self.assertEqual(self.storage.get_json("last_resume_checkpoint")["active_cycle_stage"], Stage.BUY_TRAIL_ACTIVE.value)

    def test_fallback_saves_gui_settings_without_rewriting_persisted_cycle(self):
        stored = self.storage.get_cycle(self.cycle.id).to_dict()
        self.cycle.last_price = 200  # In-memory value is not a durable transition.
        settings = replace(self.strategy, investment_amount=2000)
        connection = replace(self.connection, market_data_type=2)
        self.assertTrue(self.controller.checkpoint_for_resume_later(connection, settings, reason="operator_exit"))
        self.assertEqual(self.storage.load_strategy_settings().investment_amount, 2000)
        self.assertEqual(self.storage.load_connection_settings().market_data_type, 2)
        self.assertEqual(self.storage.get_cycle(self.cycle.id).to_dict(), stored)

    def test_fallback_marker_uses_committed_completion(self):
        stale = CycleState.from_dict(self.cycle.to_dict())
        self.cycle.stage = Stage.CYCLE_COMPLETE
        self.cycle.buy_filled_qty = 10
        self.cycle.sell_filled_qty = 10
        self.storage.upsert_cycle(self.cycle)
        checkpoint = self.storage.save_resume_checkpoint(
            self.connection, self.strategy, stale, reason="operator_exit",
            checkpoint_id="complete", preserve_persisted_cycle=True,
        )
        self.assertEqual(checkpoint["active_cycle_stage"], Stage.CYCLE_COMPLETE.value)
        self.assertFalse(checkpoint["resume_required"])
        self.assertEqual(self.storage.get_cycle(self.cycle.id).sell_filled_qty, 10)

    def test_normal_worker_checkpoint_applies_and_persists_safe_settings(self):
        with self.synchronous_worker():
            self.assertTrue(self.controller.checkpoint_for_resume_later(
                self.connection, replace(self.strategy, investment_amount=2000), reason="operator_exit",
            ))
        self.assertEqual(self.storage.load_strategy_settings().investment_amount, 2000)
        self.assertEqual(self.storage.get_cycle(self.cycle.id).investment_amount, 2000)

    def test_acknowledged_worker_storage_failure_is_not_retried_as_fallback(self):
        with self.synchronous_worker(), patch.object(
            self.storage, "save_resume_checkpoint", side_effect=sqlite3.OperationalError("injected write failure"),
        ) as save:
            self.assertFalse(self.controller.checkpoint_for_resume_later(
                self.connection, self.strategy, reason="operator_exit",
            ))
        self.assertEqual(save.call_count, 1)
        self.assertTrue(self.controller._storage_fault_active)
        self.assertIsNone(self.storage.get_json("last_resume_checkpoint"))

    def test_fallback_storage_failure_reports_failure_without_changing_cycle(self):
        before = self.storage.get_cycle(self.cycle.id).to_dict()
        with patch.object(self.storage, "save_resume_checkpoint", side_effect=sqlite3.OperationalError("injected write failure")):
            self.assertFalse(self.controller.checkpoint_for_resume_later(
                self.connection, self.strategy, reason="operator_exit",
            ))
        self.assertEqual(self.storage.get_cycle(self.cycle.id).to_dict(), before)
        self.assertIsNone(self.storage.get_json("last_resume_checkpoint"))

    def test_fallback_transaction_failure_rolls_back_settings_and_marker(self):
        self.submit_buy()
        before = self.storage.get_cycle(self.cycle.id).to_dict()
        with self.storage.connect() as con:
            con.execute(
                "CREATE TRIGGER fail_checkpoint_event BEFORE INSERT ON events "
                "WHEN NEW.message LIKE 'Resume checkpoint saved%' "
                "BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END"
            )
        self.assertFalse(self.controller.checkpoint_for_resume_later(
            self.connection, replace(self.strategy, investment_amount=2000), reason="operator_exit",
        ))
        self.assertEqual(self.storage.get_cycle(self.cycle.id).to_dict(), before)
        self.assertEqual(self.storage.load_strategy_settings().investment_amount, 1000)
        self.assertIsNone(self.storage.get_json("last_resume_checkpoint"))

    def test_fallback_idempotence_does_not_replace_committed_order(self):
        stale = CycleState.from_dict(self.cycle.to_dict())
        self.submit_buy()
        checkpoint = self.storage.save_resume_checkpoint(
            self.connection, self.strategy, stale, reason="operator_exit",
            checkpoint_id="repeat", preserve_persisted_cycle=True,
        )
        duplicate = self.storage.save_resume_checkpoint(
            self.connection, self.strategy, stale, reason="operator_exit",
            checkpoint_id="repeat", preserve_persisted_cycle=True,
        )
        self.assertEqual(duplicate, checkpoint)
        self.assertEqual(self.storage.get_cycle(self.cycle.id).buy_order_id, 7001)
        with self.storage.connect() as con:
            count = con.execute("SELECT COUNT(*) FROM events WHERE message LIKE 'Resume checkpoint saved%'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_restart_refuses_stage1_with_exact_known_working_order(self):
        self.submit_buy()
        # Reproduce an older damaged checkpoint while retaining its durable order.
        self.cycle.stage = Stage.WAIT_INITIAL_DROP
        self.cycle.buy_order_ref = None
        self.cycle.buy_order_id = None
        self.cycle.buy_perm_id = None
        self.storage.upsert_cycle(self.cycle)
        adapter = self.recovery_adapter([self.working_buy()])
        self.controller.adapter = adapter
        self.controller.connected = True
        self.controller._recover_after_connect()
        self.assertTrue(self.controller._recovery_required)
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("exact-known app order", self.controller.active_cycle.error_message)
        self.assertTrue(self.storage.get_cycle(self.cycle.id).recovery_required)
        adapter.cancel_order.assert_not_called()
        adapter.place_trailing_stop.assert_not_called()
        adapter.place_market_order.assert_not_called()

    def test_recovery_ignores_another_installations_order(self):
        self.controller.adapter = self.recovery_adapter([self.working_buy(order_ref="IBKRBOT|TEST|OTHER|BUY_TRAIL")])
        self.controller.connected = True
        self.controller._recover_after_connect()
        self.assertFalse(self.controller._recovery_required)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_INITIAL_DROP)

    def test_recovery_does_not_treat_terminal_known_order_as_working(self):
        self.submit_buy()
        self.cycle.stage = Stage.WAIT_INITIAL_DROP
        self.cycle.buy_status = "Cancelled"
        self.storage.upsert_cycle(self.cycle)
        self.controller.adapter = self.recovery_adapter([self.working_buy(status="Cancelled")])
        self.controller.connected = True
        self.controller._recover_after_connect()
        self.assertFalse(self.controller._recovery_required)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_INITIAL_DROP)

    def test_recovery_retains_normal_stage2_working_order(self):
        self.submit_buy()
        self.controller.adapter = self.recovery_adapter([self.working_buy()])
        self.controller.connected = True
        self.controller._recover_after_connect()
        self.assertFalse(self.controller._recovery_required)
        self.assertEqual(self.controller.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(self.controller.active_cycle.buy_order_id, 7001)


if __name__ == "__main__":
    unittest.main()
