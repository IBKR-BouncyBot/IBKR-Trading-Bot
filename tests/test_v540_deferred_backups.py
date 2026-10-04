"""Deferred copies preserve committed order/fill state and full validation."""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import Stage
from app.strategy import StrategyAction, StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class DeferredBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            cls.module = importlib.import_module("app.controller")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.instance_count = 0

    def prepare(self, side="BUY", *, trail=False, protective=False):
        self.instance_count += 1
        broker = DeterministicBrokerAdapter()
        settings = permissive_strategy()
        settings.contract_con_id = broker.contract.con_id
        controller = make_controller(
            self.module, self.root / f"state-{self.instance_count}.sqlite", broker, settings,
        )
        self.addCleanup(controller._market_capture.shutdown)
        controller._start_trade_market_data_capture = lambda *args, **kwargs: None
        controller.connection.account = "SIM"
        cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
        cycle.quantity = 10
        cycle.con_id = broker.contract.con_id
        if side == "SELL":
            cycle.buy_filled_qty = 10
            cycle.avg_buy_price = 90.0
            cycle.buy_status = "Filled"
            broker.position = 10.0
        role = "PROTECTIVE_SELL" if protective else side
        ref = make_order_ref(cycle.ticker, 1, cycle.id, role + ("_TRAIL" if trail else "_MARKET"))
        prefix = "protective_sell" if protective else side.lower()
        setattr(cycle, prefix + "_order_ref", ref)
        cycle.protective_sell_enabled = protective
        cycle.stage = (
            Stage.WAIT_RISE_TRIGGER if protective
            else Stage.BUY_TRAIL_ACTIVE if side == "BUY" else Stage.SELL_TRAIL_ACTIVE
        )
        controller.active_cycle = cycle
        controller.storage.upsert_cycle(cycle)
        publish_fresh_price(controller, broker, 100.0)
        action = StrategyAction("PLACE_" + role + ("_TRAIL" if trail else "_MARKET"), {
            "ticker": cycle.ticker, "quantity": 10, "order_type": "TRAIL" if trail else "MKT",
            "order_ref": ref, "reference_price": 100.0,
            "trailing_percent": 1.0, "initial_stop_price": 101.0 if side == "BUY" else 99.0,
        })
        return controller, broker, cycle, action

    def test_all_normal_order_types_commit_intent_and_submit_before_copy(self):
        for side, trail, protective in (
            ("BUY", False, False), ("BUY", True, False),
            ("SELL", False, False), ("SELL", True, False), ("SELL", True, True),
        ):
            with self.subTest(side=side, trail=trail, protective=protective):
                controller, broker, cycle, action = self.prepare(side, trail=trail, protective=protective)
                events = []
                place = broker._place

                def checked_place(**kwargs):
                    # A separate connection must see the committed intent before
                    # the fake broker accepts the order, and no copy has run.
                    with closing(sqlite3.connect(controller.storage.db_path)) as connection:
                        row = connection.execute(
                            "SELECT status FROM orders WHERE order_ref=?", (action.payload["order_ref"],),
                        ).fetchone()
                    self.assertEqual(row, ("INTENT_CREATED",))
                    self.assertEqual(events, [])
                    events.append("submit")
                    return place(**kwargs)

                broker._place = checked_place

                def checked_backup(reason):
                    self.assertEqual(reason, "order_submission")
                    row = controller.storage.get_order_for_cycle_ref(cycle.id, action.payload["order_ref"])
                    self.assertEqual(row["status"], "Submitted")
                    self.assertIsNotNone(row["order_id"])
                    events.append("backup")

                controller.storage.backup_database = checked_backup
                controller._execute_actions([action], cycle)
                self.assertEqual(events, ["submit"])
                self.assertEqual(len(broker.placed_orders), 1)
                controller._drain_commands()
                self.assertEqual(events, ["submit", "backup"])

    def test_intent_write_failure_still_prevents_broker_submission(self):
        for side, trail in (("BUY", False), ("BUY", True), ("SELL", False), ("SELL", True)):
            with self.subTest(side=side, trail=trail):
                controller, broker, cycle, action = self.prepare(side, trail=trail)
                with patch.object(controller.storage, "create_order_intent", side_effect=sqlite3.OperationalError("disk full")):
                    with self.assertRaises(sqlite3.OperationalError):
                        controller._execute_actions([action], cycle)
                self.assertEqual(broker.placed_orders, [])
                self.assertIsNone(controller.storage.get_order_for_cycle_ref(cycle.id, action.payload["order_ref"]))

    def test_backup_failure_does_not_change_submitted_order_or_storage_fault_state(self):
        for failure in (OSError("backup destination unavailable"), sqlite3.OperationalError("backup I/O error")):
            with self.subTest(failure=type(failure).__name__):
                controller, broker, cycle, action = self.prepare()
                controller.storage.backup_database = Mock(side_effect=failure)
                controller._execute_actions([action], cycle)
                before = controller.storage.get_cycle(cycle.id).to_dict()
                controller._drain_commands()
                self.assertEqual(len(broker.placed_orders), 1)
                self.assertEqual(controller.storage.get_cycle(cycle.id).to_dict(), before)
                self.assertEqual(
                    controller.storage.get_order_for_cycle_ref(cycle.id, action.payload["order_ref"])["status"],
                    "Submitted",
                )
                self.assertFalse(controller._storage_fault_active)
                controller.storage.backup_database.assert_called_once_with("order_submission")

    def test_partial_and_terminal_buy_fills_are_committed_before_copy(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                controller, broker, cycle, action = self.prepare(trail=True)
                backups = []
                controller.storage.backup_database = lambda reason: backups.append(reason)
                controller._execute_actions([action], cycle)
                controller._drain_commands()
                backups.clear()
                shares = controller.active_cycle.quantity if terminal else 4
                polled = broker.fill_order(action.payload["order_ref"], shares=shares, price=100.0)
                broker.events.clear()
                controller._handle_buy_order_poll(controller.active_cycle, polled)
                saved = controller.storage.get_cycle(cycle.id)
                self.assertEqual(saved.buy_filled_qty, shares)
                self.assertEqual(saved.stage, Stage.WAIT_RISE_TRIGGER if terminal else Stage.BUY_TRAIL_ACTIVE)
                self.assertEqual(controller.storage.get_execution_totals(cycle.id, "BUY")["shares"], shares)
                self.assertEqual(backups, [])
                controller._drain_commands()
                self.assertEqual(backups, ["after_buy_fill" if terminal else "after_buy_partial_fill"])

    def test_normal_and_protective_sell_completion_is_committed_before_copy(self):
        for protective in (False, True):
            with self.subTest(protective=protective):
                controller, broker, cycle, action = self.prepare("SELL", trail=True, protective=protective)
                backups = []
                controller.storage.backup_database = lambda reason: backups.append(reason)
                controller._execute_actions([action], cycle)
                controller._drain_commands()
                backups.clear()
                polled = broker.fill_order(action.payload["order_ref"], shares=10, price=100.0)
                broker.events.clear()
                handler = controller._handle_protective_sell_order_poll if protective else controller._handle_sell_order_poll
                handler(controller.active_cycle, polled)
                saved = controller.storage.get_cycle(cycle.id)
                self.assertEqual(saved.stage, Stage.CYCLE_COMPLETE)
                self.assertEqual(saved.sell_filled_qty, 10)
                role = "PROTECTIVE_SELL" if protective else "SELL"
                self.assertEqual(controller.storage.get_execution_totals(cycle.id, role)["shares"], 10)
                self.assertEqual(backups, [])
                controller._drain_commands()
                self.assertEqual(backups, ["after_protective_sell_fill" if protective else "after_sell_fill"])

    def test_buy_fill_places_protection_before_either_queued_copy(self):
        controller, broker, cycle, action = self.prepare(trail=True)
        cycle.protective_sell_enabled = True
        cycle.protective_sell_trailing_stop_pct = 4.0
        controller.storage.upsert_cycle(cycle)
        backups = []
        controller.storage.backup_database = lambda reason: backups.append(reason)
        controller._execute_actions([action], cycle)
        controller._drain_commands()
        backups.clear()
        polled = broker.fill_order(
            action.payload["order_ref"], shares=controller.active_cycle.quantity, price=100.0,
        )
        broker.events.clear()
        controller._handle_buy_order_poll(controller.active_cycle, polled)
        saved = controller.storage.get_cycle(cycle.id)
        self.assertEqual(saved.buy_status, "Filled")
        self.assertEqual(saved.protective_sell_status, "Submitted")
        self.assertEqual([order["action"] for order in broker.placed_orders], ["BUY", "SELL"])
        self.assertEqual(backups, [])
        controller._drain_commands()
        self.assertEqual(backups, ["after_buy_fill", "order_submission"])

    def test_close_before_rth_submission_and_completion_precede_copies(self):
        controller, broker, cycle, _action = self.prepare("SELL")
        cycle.close_before_rth_liquidation_requested = True
        cycle.sell_status = "Cancelled"
        controller.storage.upsert_cycle(cycle)
        controller._session_minutes_from_rth_status = lambda: {
            "available": True, "minutes_since_open": 100.0, "minutes_to_close": 5.0,
        }
        backups = []
        controller.storage.backup_database = lambda reason: backups.append(reason)
        self.assertTrue(controller._submit_close_before_rth_market_sell(cycle))
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0]["tif"], "DAY")
        self.assertFalse(broker.placed_orders[0]["outside_rth"])
        self.assertEqual(backups, [])
        saved = controller.storage.get_cycle(cycle.id)
        self.assertEqual(saved.sell_status, "Submitted")
        controller._drain_commands()
        self.assertEqual(backups, ["order_submission"])
        polled = broker.fill_order(saved.sell_order_ref, shares=10, price=100.0)
        broker.events.clear()
        controller._handle_sell_order_poll(controller.active_cycle, polled)
        self.assertEqual(controller.storage.get_cycle(cycle.id).stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(backups, ["order_submission"])
        controller._drain_commands()
        self.assertEqual(backups, ["order_submission", "after_sell_fill"])

    def test_queued_copy_keeps_full_restore_validation(self):
        controller, _broker, _cycle, _action = self.prepare()
        with patch.object(
            controller.storage, "validate_restore_candidate", wraps=controller.storage.validate_restore_candidate,
        ) as validation:
            controller._queue_database_backup("order_submission")
            validation.assert_not_called()
            self.assertEqual(controller.storage.list_database_backups(), [])
            controller._drain_commands()
            validation.assert_called_once()
        backups = controller.storage.list_database_backups()
        self.assertEqual(len(backups), 1)
        self.assertTrue(controller.storage.validate_restore_candidate(backups[0])["ok"])


if __name__ == "__main__":
    unittest.main()
