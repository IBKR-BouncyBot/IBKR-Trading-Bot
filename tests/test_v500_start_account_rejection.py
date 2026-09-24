"""Reject unconfirmed new-cycle routing without locking an unstarted cycle."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.models import ConnectionSettings, CycleState, Stage
from app.storage import BotStorage
from tests.support.controller_harness import permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class ExactAccountBroker(DeterministicBrokerAdapter):
    requires_exact_contract_selection = True


class StartAccountRejectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v500_start_account_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = BotStorage(Path(self.folder.name) / "state.sqlite")
        self.settings = replace(
            permissive_strategy(), contract_con_id=123, primary_exchange="NASDAQ",
        )
        self.connection = ConnectionSettings(account="", market_data_type=1)
        self.storage.save_strategy_settings(self.settings)
        self.storage.save_connection_settings(self.connection)
        self.controller = self.controller_type(self.storage)
        self.addCleanup(self.controller._market_capture.shutdown)
        self.controller.emit_snapshot = lambda *args, **kwargs: None
        self.broker = ExactAccountBroker()
        self.broker.accounts = ["ACCOUNT_A", "ACCOUNT_B"]
        self.controller.adapter = self.broker

    def start(self, connection=None):
        self.broker.publish_price(100)
        self.controller._handle_command("START_STRATEGY", {
            "connection": connection or self.connection,
            "strategy": self.settings,
        })

    def cycle_count(self):
        with self.storage.connect() as con:
            return con.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]

    def assert_unstarted(self):
        self.assertIsNone(self.controller.active_cycle)
        self.assertIsNone(self.storage.get_latest_active_cycle())
        self.assertEqual(self.storage.get_unresolved_cycles(), [])
        self.assertEqual(self.cycle_count(), 0)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])
        self.assertFalse(self.controller._recovery_required)
        self.assertIn("Start blocked:", self.controller.status)

    def test_ambiguous_auto_account_does_not_create_or_lock_cycle(self):
        self.start()
        self.assert_unstarted()
        self.assertIn("unambiguous managed account", self.controller.status)

    def test_invalid_explicit_account_does_not_create_or_lock_cycle(self):
        self.start(replace(self.connection, account="UNKNOWN_ACCOUNT"))
        self.assert_unstarted()
        self.assertIn("not confirmed", self.controller.status)

    def test_unavailable_managed_accounts_do_not_create_cycle(self):
        with patch.object(self.broker, "managed_accounts", side_effect=RuntimeError("request failed")):
            self.start()
        self.assert_unstarted()
        self.assertIn("could not be verified", self.controller.status)

    def test_select_valid_account_and_retry_after_rejected_start(self):
        self.start()
        self.assert_unstarted()
        selected = replace(self.connection, account="ACCOUNT_A")
        self.controller._handle_command("SAVE_DRAFT_SETTINGS", {
            "connection": selected, "strategy": self.settings,
        })
        self.assertEqual(self.storage.load_connection_settings().account, "ACCOUNT_A")
        self.start(selected)
        cycle = self.controller.active_cycle
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertEqual(cycle.account, "ACCOUNT_A")
        self.assertEqual(cycle.cycle_number, 1)
        self.assertEqual(self.cycle_count(), 1)
        self.assertFalse(self.controller._recovery_required)
        self.assertEqual(self.broker.placed_orders, [])

    def test_single_managed_account_auto_start_still_pins_exact_account(self):
        self.broker.accounts = ["ACCOUNT_A"]
        self.start()
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertEqual(self.controller.active_cycle.account, "ACCOUNT_A")
        self.assertEqual(self.cycle_count(), 1)
        self.assertFalse(self.controller._recovery_required)

    def test_rejected_new_cycle_preserves_completed_history(self):
        completed = CycleState.new(self.settings, 1, "ACCOUNT_A", 100, 0)
        completed.stage = Stage.CYCLE_COMPLETE
        self.storage.upsert_cycle(completed)
        self.controller.active_cycle = completed
        self.controller.connected = True
        self.controller._broker_connectivity = self.broker.connectivity_status().to_dict()
        self.controller._broker_connectivity_initialized = True
        self.start()
        self.assertEqual(self.controller.active_cycle.id, completed.id)
        self.assertEqual(self.controller.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(self.cycle_count(), 1)
        self.assertEqual(self.storage.get_unresolved_cycles(), [])
        self.assertEqual(self.broker.placed_orders, [])
        self.assertIn("Start blocked:", self.controller.status)

    def test_existing_exposure_with_unverified_account_still_requires_recovery(self):
        cycle = CycleState.new(self.settings, 1, "ACCOUNT_A", 100, 0)
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.quantity = 10
        cycle.buy_filled_qty = 10
        cycle.avg_buy_price = 100
        self.storage.upsert_cycle(cycle)
        self.controller.active_cycle = cycle
        self.broker.accounts = ["ACCOUNT_B"]
        self.start()
        persisted = self.storage.get_cycle(cycle.id)
        self.assertEqual(persisted.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(persisted.recovery_required)
        self.assertTrue(self.controller._recovery_required)
        self.assertEqual(persisted.buy_filled_qty, 10)
        self.assertEqual(self.cycle_count(), 1)
        self.assertEqual(self.broker.placed_orders, [])


    def restore_cycle(self, stage):
        self.broker.accounts = ["ACCOUNT_A"]
        cycle = CycleState.new(self.settings, 1, "ACCOUNT_A", 100, 0)
        cycle.stage = stage
        if stage in {Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE}:
            cycle.quantity = 10
            cycle.buy_filled_qty = 10
            cycle.avg_buy_price = 100
            cycle.buy_status = "Filled"
            self.broker.external_position = 10
        if stage in {Stage.BUY_TRAIL_ACTIVE, Stage.SELL_TRAIL_ACTIVE}:
            side = "BUY" if stage == Stage.BUY_TRAIL_ACTIVE else "SELL"
            ref = f"IBKRBOT|AAPL|CYCLE-000001|{cycle.id[:8]}|{side}"
            handle = self.broker.place_market_order(
                contract=self.broker.contract, action=side, quantity=10,
                order_ref=ref, tif="GTC", account="ACCOUNT_A", outside_rth=False,
            )
            for field, value in (
                ("order_ref", ref), ("order_id", handle.order_id),
                ("perm_id", handle.perm_id), ("status", handle.status),
            ):
                setattr(cycle, f"{side.lower()}_{field}", value)
        self.storage.upsert_cycle(cycle)
        self.controller.active_cycle = cycle
        self.controller._startup_resume_required = True
        self.broker.publish_price(100)
        self.controller._handle_command("CONNECT", {"settings": self.connection})
        self.assertIn("click 4. Start strategy", self.controller.status)
        return cycle

    def assert_resume_status(self, stage):
        original = self.restore_cycle(stage)
        previous_order_count = len(self.broker.placed_orders)
        self.start()
        self.assertEqual(self.controller.active_cycle.id, original.id)
        self.assertEqual(self.controller.active_cycle.stage, stage)
        self.assertFalse(self.controller._startup_resume_required)
        self.assertFalse(self.controller._recovery_required)
        self.assertEqual(self.controller.status, f"Monitoring AAPL at {stage.value}.")
        self.assertEqual(len(self.broker.placed_orders), previous_order_count)
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_successful_stage1_resume_replaces_old_start_instruction(self):
        self.assert_resume_status(Stage.WAIT_INITIAL_DROP)

    def test_successful_stage2_resume_replaces_old_start_instruction(self):
        self.assert_resume_status(Stage.BUY_TRAIL_ACTIVE)

    def test_successful_stage3_resume_replaces_old_start_instruction(self):
        self.assert_resume_status(Stage.WAIT_RISE_TRIGGER)

    def test_successful_stage4_resume_replaces_old_start_instruction(self):
        self.assert_resume_status(Stage.SELL_TRAIL_ACTIVE)

    def test_resume_replaces_start_instruction_after_upstream_restore(self):
        self.restore_cycle(Stage.WAIT_INITIAL_DROP)
        self.controller.status = "IBKR server connection restored. Stored cycle still requires an explicit Start action."
        self.start()
        self.assertEqual(self.controller.status, f"Monitoring AAPL at {Stage.WAIT_INITIAL_DROP.value}.")

    def test_resume_preserves_new_status_from_recovery(self):
        self.restore_cycle(Stage.WAIT_INITIAL_DROP)
        recover = self.controller._recover_after_connect

        def recover_with_new_status():
            recover()
            self.controller.status = "Specific recovery result"

        with patch.object(self.controller, "_recover_after_connect", side_effect=recover_with_new_status):
            self.start()
        self.assertEqual(self.controller.status, "Specific recovery result")

    def test_resume_does_not_replace_unrelated_prior_status(self):
        self.restore_cycle(Stage.WAIT_INITIAL_DROP)
        self.controller.status = "Existing diagnostic"
        self.start()
        self.assertEqual(self.controller.status, "Existing diagnostic")

    def test_resume_does_not_claim_monitoring_when_recovery_fails(self):
        self.restore_cycle(Stage.WAIT_RISE_TRIGGER)
        self.broker.accounts = ["ACCOUNT_B"]
        self.start()
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.controller._recovery_required)
        self.assertNotIn("Monitoring AAPL", self.controller.status)
        self.assertIn("not confirmed", self.controller.active_cycle.error_message)


if __name__ == "__main__":
    unittest.main()
