"""Restored account binding and edit boundaries before explicit Start."""

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


class ExactBroker(DeterministicBrokerAdapter):
    requires_exact_contract_selection = True


class LegacyStage1StartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._legacy_stage1_start_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.module = module

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.storage = BotStorage(self.root / "state.sqlite")
        self.connection = ConnectionSettings(account="", market_data_type=1)
        self.settings = replace(
            permissive_strategy(), contract_con_id=123, primary_exchange="NASDAQ",
            buy_rebound_trail_pct=0, sell_trailing_stop_pct=0,
        )
        self.storage.save_connection_settings(self.connection)
        self.storage.save_strategy_settings(self.settings)
        self.broker = ExactBroker()
        self.broker.accounts = ["ACCOUNT_A"]

    def restore(self, stage=Stage.WAIT_INITIAL_DROP, account=""):
        cycle = CycleState.new(self.settings, 1, account, 100, 0)
        cycle.stage = stage
        cycle.last_price = 99.5
        if stage == Stage.WAIT_RISE_TRIGGER:
            cycle.quantity = 10
            cycle.buy_filled_qty = 10
            cycle.avg_buy_price = 97
            cycle.buy_status = "Filled"
            self.broker.external_position = 10
        self.storage.upsert_cycle(cycle)
        with patch.object(self.module, "debug_captures_dir", return_value=self.root / "captures"):
            self.controller = self.module.TradingController(self.storage)
        self.addCleanup(self.controller._market_capture.shutdown)
        self.controller.emit_snapshot = lambda *args, **kwargs: None
        self.controller.adapter = self.broker
        self.broker.publish_price(99.5)
        self.controller._handle_command("CONNECT", {"settings": self.connection})
        self.assertTrue(self.controller._startup_resume_required)
        self.assertEqual(self.broker.placed_orders, [])
        return cycle

    def start(self):
        self.controller._handle_command("START_STRATEGY", {
            "connection": self.connection, "strategy": self.settings,
        })

    def tick(self, price):
        self.broker.publish_price(price)
        self.controller._run_broker_cycle()
        self.controller._run_strategy_cycle()

    def edit(self, **changes):
        self.controller._handle_command("SAVE_DRAFT_SETTINGS", {
            "connection": self.connection,
            "strategy": replace(self.settings, **changes),
        })

    def test_restored_blank_account_binds_before_first_buy_reference(self):
        self.settings = replace(self.settings, buy_rebound_trail_pct=0.5)
        original = self.restore()
        self.start()
        current = self.storage.get_cycle(original.id)
        self.assertEqual(current.account, "ACCOUNT_A")
        self.assertIsNone(current.buy_order_ref)
        self.assertFalse(current.recovery_required)
        self.tick(97)
        self.assertEqual(self.controller.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.broker.placed_orders[0]["account"], "ACCOUNT_A")
        self.assertEqual(self.broker.placed_orders[0]["order_type"], "TRAIL")
        self.assertFalse(self.controller._recovery_required)

    def test_legacy_start_and_full_lifecycle_still_complete(self):
        self.restore()
        self.start()
        self.tick(97)
        buy = self.controller.active_cycle
        self.assertEqual(buy.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(self.broker.placed_orders[0]["order_type"], "MKT")
        self.broker.fill_order(buy.buy_order_ref, shares=buy.quantity, price=97, terminal=True)
        self.tick(98)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.tick(105)
        self.tick(105)
        sell = self.controller.active_cycle
        self.assertEqual(sell.stage, Stage.SELL_TRAIL_ACTIVE)
        self.broker.fill_order(sell.sell_order_ref, shares=sell.buy_filled_qty, price=105, terminal=True)
        self.tick(105)
        self.assertEqual(self.controller.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(len(self.broker.placed_orders), 2)

    def test_ambiguous_restored_account_blocks_before_planning_buy(self):
        original = self.restore()
        self.broker.accounts = ["ACCOUNT_A", "ACCOUNT_B"]
        self.start()
        current = self.storage.get_cycle(original.id)
        self.assertEqual(current.stage, Stage.MANUAL_REVIEW)
        self.assertIsNone(current.buy_order_ref)
        self.assertEqual(self.broker.placed_orders, [])

    def test_direct_waiting_stage_entry_binds_before_generating_buy(self):
        self.restore()
        self.controller._startup_resume_required = False
        cycle, actions = self.controller._advance_waiting_cycle_from_price(
            self.controller.active_cycle, 97, is_rth=True, rth_message="open",
        )
        self.assertEqual(cycle.account, "ACCOUNT_A")
        self.assertEqual(cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(len(actions), 1)
        self.controller.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        self.controller._execute_actions(actions, cycle)
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.broker.placed_orders[0]["account"], "ACCOUNT_A")

    def test_stage1_edit_before_start_saves_settings_without_buy(self):
        original = self.restore(account="ACCOUNT_A")
        self.edit(initial_drop_pct=0.25)
        current = self.storage.get_cycle(original.id)
        self.assertEqual(self.storage.load_strategy_settings().initial_drop_pct, 0.25)
        self.assertEqual(current.initial_drop_pct, 0.25)
        self.assertEqual(current.stage, Stage.WAIT_INITIAL_DROP)
        self.assertIsNone(current.buy_order_ref)
        self.assertTrue(self.controller._startup_resume_required)
        self.assertEqual(self.broker.placed_orders, [])

    def test_stage3_edits_before_start_do_not_collect_sell_confirmations(self):
        original = self.restore(stage=Stage.WAIT_RISE_TRIGGER, account="ACCOUNT_A")
        for _ in range(2):
            self.broker.publish_price(105)
            self.controller._run_strategy_cycle()
            self.edit(rise_trigger_pct=0.1)
        current = self.storage.get_cycle(original.id)
        self.assertEqual(current.rise_trigger_pct, 0.1)
        self.assertEqual(current.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertIsNone(current.sell_order_ref)
        self.assertEqual(self.controller._stage3_sell_confirmation, {})
        self.assertTrue(self.controller._startup_resume_required)
        self.assertEqual(self.broker.placed_orders, [])

    def test_running_stage1_edit_still_can_submit_buy(self):
        self.restore(account="ACCOUNT_A")
        self.start()
        self.tick(99.5)
        self.edit(initial_drop_pct=0.25)
        self.assertFalse(self.controller._startup_resume_required)
        self.assertEqual(self.controller.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(len(self.broker.placed_orders), 1)

    def test_existing_prepared_or_uncertain_ref_does_not_gain_inferred_account(self):
        for status in (None, "SUBMISSION_UNKNOWN"):
            with self.subTest(status=status):
                cycle = CycleState.new(self.settings, 1, "", 100, 0)
                cycle.buy_order_ref = f"IBKRBOT|AAPL|CYCLE-000001|{cycle.id[:8]}|BUY_TRAIL"
                cycle.buy_status = status
                self.storage.upsert_cycle(cycle)
                with patch.object(self.module, "debug_captures_dir", return_value=self.root / "captures"):
                    controller = self.module.TradingController(self.storage)
                self.addCleanup(controller._market_capture.shutdown)
                controller.adapter = self.broker
                controller.emit_snapshot = lambda *args, **kwargs: None
                message = controller._bind_cycle_account(cycle)
                self.assertIn("no unambiguous exact-order account evidence", message)
                self.assertEqual(cycle.account, "")
                self.assertEqual(self.broker.placed_orders, [])


if __name__ == "__main__":
    unittest.main()
