"""Replay the supplied partial BUY and quotes through the controller and SQLite.

Only the ten-share execution and cancellation are observed facts. Tests that
finish the other five shares, restart, or change the native order to MKT are
explicit controlled alternatives; they do not assert those events happened.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.ib_adapter import PolledOrderState
from app.models import CycleState, Stage, StrategySettings
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "v570_amd_partial_buy.json").read_text())


class AmdPartialBuyReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v570_amd_replay_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db_path = Path(folder.name) / "replay.sqlite"
        self.broker = DeterministicBrokerAdapter(ticker="SIMSTK")
        self.broker.accounts = ["SIM"]
        self.settings = StrategySettings(**deepcopy(_FIXTURE["strategy_at_fill"]))
        self.c = self.new_controller()
        self.cycle = CycleState.from_dict(deepcopy(_FIXTURE["cycle_at_fill"]))
        self.order_type = "MKT" if "market_order_variant" in self._testMethodName else "TRAIL"
        if self.order_type == "MKT":
            self.cycle.buy_order_ref = self.cycle.buy_order_ref.replace("BUY_TRAIL", "BUY_MARKET")
        self.cycle.buy_filled_qty = 0
        self.cycle.buy_filled_at = None
        self.cycle.avg_buy_price = None
        self.cycle.buy_commission = 0.0
        self.c.active_cycle = self.cycle
        self.c.storage.upsert_cycle(self.cycle)
        order = _FIXTURE["observed_order"]
        self.c.storage.add_order(
            cycle=self.cycle, action="BUY", order_type=self.order_type, order_id=self.cycle.buy_order_id,
            perm_id=self.cycle.buy_perm_id, order_ref=self.cycle.buy_order_ref,
            quantity=order["quantity"], trailing_percent=order["trailing_percent"] if self.order_type == "TRAIL" else None,
            initial_stop_price=order["initial_stop_price"] if self.order_type == "TRAIL" else None, status="PreSubmitted",
        )
        self.broker.orders[self.cycle.buy_order_ref] = self.state(filled=0, remaining=15, executions=[])
        self.monotonic = self.enterContext(patch.object(self.module.time, "monotonic"))
        self.now = datetime.fromisoformat("2026-10-06T18:22:47.265+00:00")
        test = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return test.now.astimezone(tz) if tz else test.now.replace(tzinfo=None)

        self.enterContext(patch.object(self.module, "datetime", Clock))
        self.snapshot(_FIXTURE["snapshot_at_fill"])

    def new_controller(self):
        c = make_controller(self.module, self.db_path, self.broker, self.settings)
        self.addCleanup(c._market_capture.shutdown)
        c._start_trade_market_data_capture = Mock()
        c._queue_database_backup = Mock()
        c.connection.account = "SIM"
        c.connection.trading_mode = "live"
        c._api_data_invalidated = False
        return c

    def snapshot(self, row):
        self.c.price_snapshot = deepcopy(row)
        self.monotonic.return_value = float(row["observation_monotonic"])
        self.c._latest_rth_status = deepcopy(row["rth_status"])

    def execution(self, *, continuation=False):
        row = deepcopy(_FIXTURE["observed_execution"])
        if continuation:
            row.update({k: v for k, v in _FIXTURE["controlled_continuation"].items() if k != "description"})
            row["avg_price"] = row["price"]
        row["order_ref"] = self.cycle.buy_order_ref
        return row

    def state(self, *, status="PreSubmitted", filled=10, remaining=5, executions=None):
        rows = [self.execution()] if executions is None else deepcopy(executions)
        quantity = sum(row["shares"] for row in rows)
        value = sum(row["shares"] * row["price"] for row in rows)
        return PolledOrderState(
            order_ref=self.cycle.buy_order_ref, order_id=self.cycle.buy_order_id,
            perm_id=self.cycle.buy_perm_id, status=status, filled=filled, remaining=remaining,
            avg_fill_price=value / quantity if quantity else 0.0,
            commission=sum(row["commission"] for row in rows), executions=rows,
            raw={"account": "SIM", "con_id": 123, "action": "BUY", "orderType": self.order_type, "totalQuantity": 15},
        )

    def poll(self, state=None):
        state = state or self.state()
        self.broker.orders[self.cycle.buy_order_ref] = deepcopy(state)
        self.broker.executions = deepcopy(state.executions)
        self.broker.external_position = float(state.filled)
        self.c._handle_buy_order_poll(self.c.active_cycle, state)
        return self.c.storage.get_cycle(self.cycle.id)

    def callback(self, *, fee=False):
        row = deepcopy(_FIXTURE["observed_execution_callback"])
        row["order_ref"] = self.cycle.buy_order_ref
        if fee:
            row["commission"] = _FIXTURE["observed_execution"]["commission"]
        self.c._apply_execution_callback_event("COMMISSION_REPORT" if fee else "EXEC_DETAILS", row, self.c.active_cycle)

    def assert_waiting(self, *, filled=10):
        cycle = self.c.storage.get_cycle(self.cycle.id)
        self.assertEqual(cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(cycle.quantity, 15)
        self.assertEqual(cycle.buy_filled_qty, filled)
        self.assertFalse(cycle.buy_remainder_cancel_requested)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "BUY")["shares"], filled)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)
        self.assertEqual(self.broker.cancelled_orders, [])
        self.assertEqual(self.broker.placed_orders, [])
        return cycle

    def complete_controlled_remainder(self):
        rows = [self.execution(), self.execution(continuation=True)]
        return self.poll(self.state(status="Filled", filled=15, remaining=0, executions=rows))

    def assert_complete_buy(self, cycle):
        self.assertEqual(cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(cycle.quantity, 15)
        self.assertEqual(cycle.buy_filled_qty, 15)
        self.assertAlmostEqual(cycle.avg_buy_price, (10 * 655.72 + 5 * 655.80) / 15)
        self.assertAlmostEqual(cycle.buy_commission, 0.382287 + 0.191144)
        totals = self.c.storage.get_execution_totals(cycle.id, "BUY")
        self.assertEqual(totals["shares"], 15)
        self.assertAlmostEqual(totals["commission"], 0.382287 + 0.191144)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_observed_stale_bid_and_live_ask_partial_remains_working(self):
        self.callback()
        self.callback(fee=True)
        message = self.c._stale_data_guard_message_for_buy(self.c.active_cycle, for_working_order=True)
        self.assertEqual(message, _FIXTURE["observed_outcome"]["cancel_reason"])
        self.poll()
        self.assert_waiting()

    def test_controlled_market_order_variant_also_remains_working(self):
        self.assertEqual(self.state().raw["orderType"], "MKT")
        self.poll()
        self.assert_waiting()

    def test_observed_capture_ticks_do_not_cancel_the_partial(self):
        self.callback()
        for row in _FIXTURE["captured_market_rows"]:
            with self.subTest(capture_line=row["line_number_1_based"]):
                self.snapshot(row["snapshot"])
                self.poll()
                self.assert_waiting()

    def test_three_second_timeout_does_not_cancel_when_quotes_recover(self):
        self.callback()
        # Ledger reconciliation uses the model clock; restore the observed
        # first-fill time so this tests expiration, not a future-time fallback.
        self.c.active_cycle.buy_filled_at = _FIXTURE["cycle_at_fill"]["buy_filled_at"]
        self.c.storage.upsert_cycle(self.c.active_cycle)
        self.snapshot(_FIXTURE["captured_market_rows"][2]["snapshot"])
        self.now += timedelta(seconds=30)
        self.poll()
        self.assert_waiting()

    def test_controlled_later_five_share_fill_settles_weighted_price_and_fees(self):
        self.callback()
        self.poll()
        self.now += timedelta(seconds=5)
        self.assert_complete_buy(self.complete_controlled_remainder())

    def test_full_quantity_without_terminal_status_does_not_start_exit(self):
        self.poll()
        rows = [self.execution(), self.execution(continuation=True)]
        self.poll(self.state(status="Submitted", filled=15, remaining=0, executions=rows))
        self.assert_waiting(filled=15)
        self.assert_complete_buy(self.complete_controlled_remainder())

    def test_controlled_multi_print_remainder_waits_for_all_fifteen(self):
        self.poll()
        second = self.execution(continuation=True)
        second["shares"] = 2
        second["commission"] = 0.05
        self.poll(self.state(status="Submitted", filled=12, remaining=3, executions=[self.execution(), second]))
        self.assert_waiting(filled=12)
        third = {**self.execution(continuation=True), "shares": 3, "execution_id": "SIM-BUY-3", "commission": 0.141144}
        settled = self.poll(self.state(status="Filled", filled=15, remaining=0, executions=[self.execution(), second, third]))
        self.assert_complete_buy(settled)

    def test_repeated_callbacks_and_polls_keep_one_ten_share_execution(self):
        for _ in range(4):
            self.callback()
            self.callback(fee=True)
            self.poll()
            self.assert_waiting()
        ledger = self.c.storage.get_cycle_audit_bundle(self.cycle.id)["executions"]
        self.assertEqual(len(ledger), 1)
        self.assertAlmostEqual(ledger[0]["commission"], 0.382287)

    def test_controlled_restart_keeps_original_partial_order(self):
        self.callback()
        self.poll()
        self.c = self.new_controller()
        self.snapshot(_FIXTURE["snapshot_at_fill"])
        self.assertTrue(self.c._recover_after_connect())
        self.poll()
        self.assert_waiting()
        self.assert_complete_buy(self.complete_controlled_remainder())

    def test_controlled_reconnect_with_open_order_preserves_partial(self):
        self.poll()
        for _ in range(3):
            self.assertTrue(self.c._recover_after_connect())
            self.poll()
            self.assert_waiting()

    def test_controlled_reconnect_poll_partial_missing_from_open_snapshot_waits(self):
        self.poll()
        self.broker.recovery_open_app_orders = Mock(return_value=[])
        self.assertTrue(self.c._recover_after_connect())
        self.assert_waiting()

    def test_actual_broker_cancelled_partial_settles_ten_without_rebuy(self):
        self.callback()
        self.callback(fee=True)
        terminal = self.poll(self.state(status=_FIXTURE["observed_outcome"]["terminal_status"]))
        self.assertEqual(terminal.stage.value, _FIXTURE["observed_outcome"]["final_cycle_stage"])
        self.assertEqual(terminal.buy_filled_qty, 10)
        self.assertAlmostEqual(terminal.buy_commission, 0.382287)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])
        self.assertEqual(self.c.storage.get_app_owned_unsold_position("SIMSTK")["quantity"], 10)

    def test_existing_pending_cancellation_is_not_replaced_or_rebought(self):
        self.callback()
        self.c.active_cycle.buy_remainder_cancel_requested = True
        self.c.storage.upsert_cycle(self.c.active_cycle)
        pending = self.poll(self.state(status="PendingCancel"))
        self.assertEqual(pending.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertTrue(pending.buy_remainder_cancel_requested)
        terminal = self.poll(self.state(status="Cancelled"))
        self.assertEqual(terminal.buy_filled_qty, 10)
        self.assertEqual(terminal.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(terminal.buy_remainder_cancel_requested)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_controlled_fill_during_pending_cancel_accounts_for_all_fifteen(self):
        self.callback()
        self.c.active_cycle.buy_remainder_cancel_requested = True
        self.c.storage.upsert_cycle(self.c.active_cycle)
        self.poll(self.state(status="PendingCancel"))
        self.assert_complete_buy(self.complete_controlled_remainder())

    def test_actual_cancelled_ten_share_position_cannot_submit_fifteen_share_exit(self):
        settled = self.poll(self.state(status="Cancelled"))
        publish_fresh_price(self.c, self.broker, settled.rise_trigger_price * 1.01)
        prepared, actions = StrategyEngine.on_price_update(settled, settled.rise_trigger_price * 1.01, is_rth=True)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].payload["quantity"], 10)
        self.c.active_cycle = prepared
        self.c.storage.upsert_cycle(prepared)
        self.c._execute_actions(actions, prepared)
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.broker.placed_orders[0]["action"], "SELL")
        self.assertEqual(self.broker.placed_orders[0]["quantity"], 10)

    def test_delayed_authoritative_commission_does_not_cancel_or_duplicate(self):
        self.callback()
        row = self.execution()
        row["commission"] = 0.0
        self.poll(self.state(executions=[row]))
        self.assert_waiting()
        self.callback(fee=True)
        self.assertAlmostEqual(self.c.storage.get_cycle(self.cycle.id).buy_commission, 0.382287)
        self.assert_waiting()

    def test_controlled_repeated_terminal_polls_do_not_duplicate_final_fill(self):
        self.poll()
        for _ in range(3):
            self.assert_complete_buy(self.complete_controlled_remainder())
        self.assertEqual(len(self.c.storage.get_cycle_audit_bundle(self.cycle.id)["executions"]), 2)

    def test_controlled_protective_sell_waits_until_terminal_and_sizes_fifteen(self):
        self.cycle.protective_sell_enabled = True
        self.c.storage.upsert_cycle(self.cycle)
        self.poll()
        self.assert_waiting()
        publish_fresh_price(self.c, self.broker, 655.80)
        settled = self.complete_controlled_remainder()
        self.assertEqual(settled.buy_filled_qty, 15)
        self.assertEqual(settled.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.broker.placed_orders[0]["action"], "SELL")
        self.assertEqual(self.broker.placed_orders[0]["quantity"], 15)
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_new_entry_guard_still_rejects_the_observed_stale_bid(self):
        self.c.price_snapshot["strategy_price_usable"] = True
        message = self.c._stale_data_guard_message_for_buy(self.cycle)
        self.assertEqual(message, _FIXTURE["observed_outcome"]["cancel_reason"])
        self.assertIn("independent bid/ask", self.c._spread_guard_message_for_buy(self.cycle))

    def test_fixture_preserves_real_order_execution_and_quote_relationships(self):
        fixture = _FIXTURE
        self.assertEqual(fixture["observed_order"]["quantity"], 15)
        self.assertEqual(fixture["observed_execution"]["shares"], 10)
        self.assertEqual(fixture["observed_execution"]["price"], 655.72)
        self.assertEqual(fixture["observed_execution"]["commission"], 0.382287)
        ages = fixture["snapshot_at_fill"]["field_update_age_seconds"]
        self.assertAlmostEqual(ages["bid"], 9.43558380001923)
        self.assertAlmostEqual(ages["ask"], 0.19179310000617988)
        self.assertEqual(ages["last"], ages["ask"])
        self.assertNotIn(1, fixture["snapshot_at_fill"]["market_data_tick_types"])
        recovered = fixture["captured_market_rows"][2]["snapshot"]
        self.assertEqual(recovered["fields"]["bid"], 655.55)
        self.assertIn(1, recovered["market_data_tick_types"])
        self.assertEqual(len(fixture["sources"]), 2)
        self.assertTrue(all(len(source["sha256"]) == 64 for source in fixture["sources"]))


if __name__ == "__main__":
    unittest.main()
