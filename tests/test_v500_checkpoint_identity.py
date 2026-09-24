"""Resume checkpoints preserve accepted identity without bypassing rejections."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import ConnectionSettings, CycleState, Stage, StrategySettings
from app.storage import BotStorage


class CheckpointIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._checkpoint_identity_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = BotStorage(Path(self.folder.name) / "checkpoint.sqlite")
        self.controller = self.controller_type(self.storage)
        self.connection = ConnectionSettings(account="ACCOUNT_A")
        self.strategy = StrategySettings(ticker="AAA", contract_con_id=111)
        self.controller.connection = self.connection
        self.controller.strategy = self.strategy
        self.controller.emit_snapshot = lambda **kwargs: None
        self.cycle = CycleState.new(self.strategy, 39, "ACCOUNT_A", 100, 0)
        self.storage.upsert_cycle(self.cycle)
        self.controller.active_cycle = self.cycle

    def tearDown(self):
        self.controller._market_capture.shutdown()
        self.folder.cleanup()

    def add_unresolved_history(self):
        historical = CycleState.new(self.strategy, 23, "ACCOUNT_A", 100, 0)
        historical.stage = Stage.CYCLE_COMPLETE
        historical.quantity = 56
        historical.buy_filled_qty = 28
        historical.sell_filled_qty = 28
        historical.buy_order_ref = "IBKRBOT|AAA|CYCLE-000023|history|BUY_TRAIL"
        historical.buy_status = "CancelRequested"
        self.storage.upsert_cycle(historical)

    def run_worker_commands_synchronously(self):
        self.controller._thread_started = True
        self.controller._thread = SimpleNamespace(is_alive=lambda: True)

        def dispatch(command):
            name, payload = command
            if name == "CHECKPOINT_RESUME_STATE":
                self.controller._process_queued_command(name, payload)

        return patch.object(self.controller._commands, "put", side_effect=dispatch)

    def test_worker_saves_unchanged_identity_with_multiple_unresolved_cycles(self):
        self.add_unresolved_history()
        result = {}
        with patch.object(self.controller, "_execute_actions") as execute:
            self.controller._handle_command("CHECKPOINT_RESUME_STATE", {
                "connection": replace(self.connection),
                "strategy": replace(self.strategy, investment_amount=12000),
                "reason": "operator_exit_resume_later",
                "checkpoint_id": "multiple-unchanged",
                "_checkpoint_result": result,
            })
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.storage.get_json("last_resume_checkpoint")["checkpoint_id"], "multiple-unchanged")
        self.assertEqual(len(self.storage.get_unresolved_cycles()), 2)
        blocked = self.controller._identity_command_message(self.connection, self.strategy)
        self.assertIn("#23 AAA (5_CYCLE_COMPLETE)", blocked)
        self.assertIn("#39 AAA (1_WAIT_INITIAL_DROP)", blocked)
        execute.assert_not_called()

    def test_direct_fallback_saves_unchanged_identity_with_multiple_cycles(self):
        self.add_unresolved_history()
        self.assertTrue(self.controller.checkpoint_for_resume_later(
            replace(self.connection), replace(self.strategy), reason="direct-fallback",
        ))
        self.assertEqual(self.storage.get_json("last_resume_checkpoint")["reason"], "direct-fallback")
        self.assertEqual(len(self.storage.get_unresolved_cycles()), 2)

    def test_worker_rejected_identity_change_cannot_fall_back_to_write(self):
        self.add_unresolved_history()
        with self.run_worker_commands_synchronously(), patch.object(
            self.storage, "save_resume_checkpoint", wraps=self.storage.save_resume_checkpoint,
        ) as save:
            saved = self.controller.checkpoint_for_resume_later(
                replace(self.connection, client_id=99), self.strategy, reason="rejected-worker",
            )
        self.assertFalse(saved)
        save.assert_not_called()
        self.assertIsNone(self.storage.get_json("last_resume_checkpoint"))
        self.assertEqual(self.controller.connection.client_id, self.connection.client_id)

    def test_direct_fallback_rejects_execution_identity_changes(self):
        for multiple in (False, True):
            if multiple:
                self.add_unresolved_history()
            for label, connection, strategy in (
                ("client", replace(self.connection, client_id=99), self.strategy),
                ("account", replace(self.connection, account="ACCOUNT_B"), self.strategy),
                ("contract", self.connection, replace(self.strategy, ticker="BBB", contract_con_id=222)),
            ):
                with self.subTest(multiple=multiple, changed=label):
                    with patch.object(self.storage, "save_resume_checkpoint") as save:
                        saved = self.controller.checkpoint_for_resume_later(
                            connection, strategy, reason="rejected-fallback",
                        )
                    self.assertFalse(saved)
                    save.assert_not_called()

    def test_acknowledged_checkpoint_failure_is_not_retried_as_fallback(self):
        with self.run_worker_commands_synchronously(), patch.object(
            self.storage, "save_resume_checkpoint", side_effect=RuntimeError("injected write failure"),
        ) as save:
            saved = self.controller.checkpoint_for_resume_later(
                self.connection, self.strategy, reason="failed-worker",
            )
        self.assertFalse(saved)
        self.assertEqual(save.call_count, 1)

    def test_worker_timeout_fallback_retains_identity_guard(self):
        self.add_unresolved_history()
        self.controller._thread_started = True
        self.controller._thread = SimpleNamespace(is_alive=lambda: True)
        with patch.object(self.storage, "save_resume_checkpoint") as save:
            saved = self.controller.checkpoint_for_resume_later(
                self.connection, replace(self.strategy, contract_con_id=222),
                reason="blocked-worker", timeout=0.1,
            )
        self.assertFalse(saved)
        save.assert_not_called()

    def test_new_order_block_reports_historical_and_current_cycles(self):
        self.add_unresolved_history()
        message = self.controller._execution_identity_message(self.cycle)
        self.assertIn("Multiple unresolved cycles", message)
        self.assertIn("#23 AAA (5_CYCLE_COMPLETE)", message)
        self.assertIn("#39 AAA (1_WAIT_INITIAL_DROP)", message)


if __name__ == "__main__":
    unittest.main()
