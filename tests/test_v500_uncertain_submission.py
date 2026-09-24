"""Potential transmission must survive exceptions/restart without an automatic retry."""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ib_adapter import BrokerAdapterError, BrokerSubmissionUnknownError
from app.models import Stage
from app.storage import BotStorage
from app.strategy import StrategyAction, StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class SubmissionBroker(DeterministicBrokerAdapter):
    outcome = "unknown"

    def _place(self, **kwargs):
        if self.outcome == "preflight":
            raise BrokerAdapterError("rejected locally before transmit")
        handle = super()._place(**kwargs)
        if self.outcome == "unknown":
            raise BrokerSubmissionUnknownError(
                "connection lost after send", order_ref=handle.order_ref,
                order_id=handle.order_id, perm_id=handle.perm_id,
            )
        if self.outcome == "unexpected":
            raise RuntimeError("unexpected adapter failure after send")
        return handle


class UncertainSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            cls.controller_module = importlib.import_module("app.controller")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite"

    def prepare(self, side, *, trail=False, protective=False, outcome="unknown"):
        broker = SubmissionBroker()
        broker.outcome = outcome
        settings = permissive_strategy(auto_repeat=True)
        settings.max_spread_pct = 0
        settings.contract_con_id = broker.contract.con_id
        controller = make_controller(self.controller_module, self.db, broker, settings)
        controller.connection.account = "DU_TEST"
        controller.storage.backup_database = lambda *_args: None
        cycle = StrategyEngine.start_cycle(settings, 1, "DU_TEST", 100.0, 0.0)
        cycle.con_id = broker.contract.con_id
        cycle.quantity = 10
        cycle.buy_filled_qty = 10 if side == "SELL" else 0
        cycle.avg_buy_price = 90.0 if side == "SELL" else None
        if side == "SELL":
            broker.position = 10.0
        role = "PROTECTIVE_SELL" if protective else side
        ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, role + ("_TRAIL" if trail else "_MARKET"))
        prefix = "protective_sell" if protective else side.lower()
        setattr(cycle, prefix + "_order_ref", ref)
        cycle.protective_sell_enabled = protective
        cycle.stage = Stage.WAIT_RISE_TRIGGER if protective else Stage.BUY_TRAIL_ACTIVE if side == "BUY" else Stage.SELL_TRAIL_ACTIVE
        controller.active_cycle = cycle
        controller.storage.upsert_cycle(cycle)
        publish_fresh_price(controller, broker, 100.0)
        action = StrategyAction("PLACE_" + role + ("_TRAIL" if trail else "_MARKET"), {
            "ticker": "AAPL", "quantity": 10, "order_type": "TRAIL" if trail else "MKT",
            "order_ref": ref, "reference_price": 100.0,
            "trailing_percent": 1.0, "initial_stop_price": 101.0 if side == "BUY" else 99.0,
        })
        return controller, broker, cycle, action, prefix

    def test_unknown_buy_sell_and_protection_are_durable_and_stop_action_batch(self):
        for side, trail, protective in (("BUY", False, False), ("BUY", True, False), ("SELL", False, False), ("SELL", True, False), ("SELL", True, True)):
            with self.subTest(side=side, trail=trail, protective=protective):
                self.db = Path(self.tmp.name) / f"{side}-{trail}-{protective}.sqlite"
                controller, broker, cycle, action, prefix = self.prepare(side, trail=trail, protective=protective)
                controller._execute_actions([action, action], cycle)
                self.assertEqual(len(broker.placed_orders), 1)
                saved = BotStorage(self.db).get_cycle(cycle.id)
                self.assertEqual(saved.stage, Stage.MANUAL_REVIEW)
                self.assertTrue(saved.recovery_required)
                self.assertEqual(getattr(saved, prefix + "_order_ref"), action.payload["order_ref"])
                self.assertEqual(getattr(saved, prefix + "_status"), "SUBMISSION_UNKNOWN")
                with controller.storage.connect() as con:
                    row = con.execute("SELECT status,order_id FROM orders WHERE order_ref=?", (action.payload["order_ref"],)).fetchone()
                self.assertEqual(row["status"], "SUBMISSION_UNKNOWN")
                self.assertIsNotNone(row["order_id"])
                restarted = make_controller(self.controller_module, self.db, broker, controller.strategy)
                restarted._recover_after_connect()
                self.assertTrue(restarted._recovery_required)
                self.assertEqual(restarted.active_cycle.stage, Stage.MANUAL_REVIEW)
                self.assertEqual(len(broker.placed_orders), 1)

    def test_unexpected_adapter_exception_is_also_uncertain(self):
        controller, broker, cycle, action, _ = self.prepare("BUY", outcome="unexpected")
        controller._execute_actions([action], cycle)
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(controller.active_cycle.buy_order_ref, action.payload["order_ref"])

    def test_definite_pretransmit_failure_still_rolls_back(self):
        controller, broker, cycle, action, _ = self.prepare("BUY", outcome="preflight")
        controller._execute_actions([action], cycle)
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(controller.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertFalse(controller.active_cycle.buy_order_ref)
        with controller.storage.connect() as con:
            row = con.execute("SELECT status FROM orders WHERE order_ref=?", (action.payload["order_ref"],)).fetchone()
        self.assertEqual(row["status"], "SUBMIT_FAILED")

    def test_successful_submission_still_uses_normal_active_stage(self):
        controller, broker, cycle, action, _ = self.prepare("BUY", outcome="success")
        controller._execute_actions([action], cycle)
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(controller.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertFalse(controller.active_cycle.recovery_required)
        self.assertEqual(controller.active_cycle.buy_status, "Submitted")


if __name__ == "__main__":
    unittest.main()
