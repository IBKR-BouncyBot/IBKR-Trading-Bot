"""Actual strategy/controller regressions for protection and threshold safety.

Run with unittest or pytest; no real broker or native Qt instance is used.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from math import nextafter
from pathlib import Path
from unittest.mock import patch

from app.ib_adapter import BrokerAdapterError, PolledOrderState
from app.models import Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class ProtectionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = "app._v500_protection_test_controller"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "app" / "controller.py")
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.module
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)
        cls.addClassCleanup(sys.modules.pop, name, None)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = BotStorage(Path(temp.name) / "state.sqlite")
        self.controller = self.module.TradingController(storage=self.storage)
        self.adapter = DeterministicBrokerAdapter(ticker="AAPL", con_id=123)
        self.controller.adapter = self.adapter
        self.controller.contract = self.adapter.contract
        self.controller.connected = True
        self.controller.connection.account = "DU_TEST"
        self.controller._broker_connectivity = {
            "local_connected": True, "upstream_connected": True,
            "state": "connected", "message": "Ready", "trading_ready": True,
        }
        self.controller._broker_connectivity_initialized = True

    def cycle(self, **changes):
        settings = StrategySettings(
            ticker="AAPL", contract_con_id=123, investment_amount=1000.0,
            initial_drop_pct=1.0, rise_trigger_pct=0.05,
            buy_rebound_trail_pct=0.0, sell_trailing_stop_pct=1.0,
            protective_sell_enabled=True, protective_sell_trailing_stop_pct=4.0,
            slippage_buffer_enabled=False, atr_adaptive_enabled=False,
            rth_only=False, reinvest_profits=False,
        )
        for name, value in changes.items():
            setattr(settings, name, value)
        self.controller.strategy = settings
        cycle = StrategyEngine.start_cycle(settings, 1, "DU_TEST", 100.0, 0.0)
        cycle, actions = StrategyEngine.on_buy_fill(cycle, 10, 100.0, "Filled")
        self.controller.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        return cycle, actions

    def working_cycle(self):
        cycle, _ = self.cycle()
        cycle = StrategyEngine.on_order_submitted(cycle, cycle.protective_sell_order_ref, 20, 120, "Submitted")
        self.adapter.orders[cycle.protective_sell_order_ref] = PolledOrderState(
            cycle.protective_sell_order_ref, 20, 120, "Submitted", 0, 10, 0.0, 0.0, [], {},
        )
        self.storage.upsert_cycle(cycle)
        self.controller.active_cycle = cycle
        return cycle

    def cancel_action(self, cycle, price):
        next_cycle, actions = StrategyEngine.on_price_update(cycle, price)
        self.assertEqual([a.action_type for a in actions], ["CANCEL_ORDER"])
        self.controller.active_cycle = next_cycle
        self.storage.upsert_cycle(next_cycle)
        self.controller.price_snapshot = {"price": price, "fields": {"bid": price, "ask": price}}
        return next_cycle, actions

    def test_valid_initial_protection_still_submits_once(self):
        cycle, actions = self.cycle()
        self.controller._execute_actions(actions, cycle)
        self.assertEqual(len(self.adapter.placed_orders), 1)
        self.assertEqual(self.adapter.placed_orders[0]["quantity"], 10)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(self.controller.active_cycle.protective_sell_status, "Submitted")
        self.assertFalse(self.controller.active_cycle.recovery_required)

    def test_blocked_initial_protection_uses_correct_role_and_persists_recovery(self):
        cycle, actions = self.cycle(rth_only=True)
        self.adapter.rth_open = False
        self.controller._execute_actions(actions, cycle)
        blocked = self.controller.active_cycle
        self.assertEqual(blocked.stage, Stage.MANUAL_REVIEW)
        self.assertIsNone(blocked.protective_sell_order_ref)
        self.assertTrue(blocked.recovery_required)
        self.assertEqual(blocked.buy_filled_qty, 10)
        self.assertFalse(self.adapter.placed_orders)
        reloaded = self.storage.get_cycle(cycle.id)
        self.assertEqual(reloaded.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(reloaded.recovery_required)
        self.assertFalse(StrategyEngine.on_price_update(reloaded, 110.0)[1])

    def test_initial_protection_normalization_failure_is_recovery(self):
        cycle, actions = self.cycle()
        with patch.object(self.controller, "_normalize_trailing_order_payload", return_value=(actions[0].payload, "Order-price validation blocked SELL submission: unavailable rule")):
            self.controller._execute_actions(actions, cycle)
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIsNone(self.controller.active_cycle.protective_sell_order_ref)
        self.assertFalse(self.adapter.placed_orders)

    def test_missing_configured_protection_cannot_silently_wait(self):
        cycle, _ = self.cycle()
        cycle.protective_sell_order_ref = None
        next_cycle, actions = StrategyEngine.on_price_update(cycle, 100.0)
        self.assertEqual(next_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(next_cycle.recovery_required)
        self.assertFalse(actions)

    def test_route_rounding_keeps_protection_working(self):
        cycle, actions = self.cancel_action(self.working_cycle(), 101.1)
        self.adapter.contract.min_tick = 0.1
        self.controller._execute_actions(actions, cycle)
        self.assertFalse(self.adapter.cancelled_orders)
        self.assertFalse(self.adapter.placed_orders)
        self.assertFalse(self.controller.active_cycle.protective_sell_cancel_requested)
        self.assertEqual(self.controller.active_cycle.protective_sell_status, "Submitted")
        self.assertIn("cancellation withheld", self.controller.active_cycle.error_message)

    def test_quantity_rule_mismatch_keeps_protection_working(self):
        cycle, actions = self.cancel_action(self.working_cycle(), 102.0)
        with patch.object(self.adapter, "normalize_order_quantity", return_value=9, create=True):
            self.controller._execute_actions(actions, cycle)
        self.assertFalse(self.adapter.cancelled_orders)
        self.assertFalse(self.controller.active_cycle.protective_sell_cancel_requested)
        self.assertIn("SELL quantity", self.controller.active_cycle.error_message)

    def test_changed_quote_before_cancellation_keeps_protection_working(self):
        cycle, actions = self.cancel_action(self.working_cycle(), 102.0)
        actions[0].payload["stage3_market_data_guard"] = {"sequence": 1}
        self.controller.price_snapshot["market_data_field_tracking"] = True
        self.controller._execute_actions(actions, cycle)
        self.assertFalse(self.adapter.cancelled_orders)
        self.assertFalse(self.controller.active_cycle.protective_sell_cancel_requested)
        self.assertIn("revalidation failed", self.controller.active_cycle.error_message)

    def test_valid_handoff_cancels_then_submits_single_final_sell(self):
        cycle, actions = self.cancel_action(self.working_cycle(), 102.0)
        self.controller._execute_actions(actions, cycle)
        self.assertEqual(self.adapter.cancelled_orders, [cycle.protective_sell_order_ref])
        self.assertFalse(self.adapter.placed_orders)
        polled = self.adapter.poll_order(cycle.protective_sell_order_ref)
        self.assertFalse(self.controller._handle_protective_sell_order_poll(cycle, polled))
        next_cycle, actions = StrategyEngine.on_price_update(self.controller.active_cycle, 102.0)
        self.controller.active_cycle = next_cycle
        self.controller._execute_actions(actions, next_cycle)
        self.assertEqual(len(self.adapter.placed_orders), 1)
        self.assertEqual(self.adapter.placed_orders[0]["quantity"], 10)
        self.assertEqual(self.controller.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)

    def test_tracked_quote_handoff_survives_repeated_cancel_polls(self):
        cycle = self.working_cycle()
        for sequence in range(1, 5):
            now = utc_now_iso()
            self.controller.price_snapshot = {
                "price": 102.0, "fields": {"bid": 102.0, "ask": 102.01},
                "source": "bidAskMidpoint", "selected_price_basis": "bid_ask",
                "selected_price_basis_update_sequence": sequence,
                "market_data_field_tracking": True,
                "api_data_received_in_latest_read": True,
                "upstream_connected": True, "subscription_market_data_type": 1,
                "market_data_update_sequence": sequence,
                "market_data_subscription_id": "AAPL|123|g1",
                "quote_update_sequence": sequence, "quote_update_received_at": now,
                "field_update_received_at": {"bid": now, "ask": now},
                "fields_updated_in_event": ["bid", "ask"],
            }
            with patch.object(self.controller, "_poll_price_if_due", return_value=(True, 102.0)):
                self.controller._run_strategy_cycle(price_timeout=0.0)
            if sequence == 1:
                self.assertFalse(self.adapter.cancelled_orders)
            if sequence in (2, 3):
                self.assertEqual(self.adapter.cancelled_orders, [cycle.protective_sell_order_ref])
                self.assertFalse(self.adapter.placed_orders)
                self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
            if sequence == 3:
                # The accepted cancellation must remain recognizable even when
                # the cycle is reconstructed from its durable SQLite state.
                self.controller.active_cycle = self.storage.get_cycle(cycle.id)
                self.assertFalse(self.controller.active_cycle.protective_sell_cancel_requested)
        self.assertEqual(len(self.adapter.placed_orders), 1)
        self.assertEqual(self.adapter.placed_orders[0]["quantity"], 10)
        self.assertEqual(self.controller.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        decisions = self.storage.get_cycle_audit_bundle(cycle.id)["decision_events"]
        confirmations = [event for event in decisions if event["event_type"] == "PROTECTIVE_HANDOFF_CANCEL_CONFIRMED"]
        self.assertEqual(len(confirmations), 1)

    def test_unsolicited_protection_cancellation_still_pauses(self):
        cycle = self.working_cycle()
        self.adapter.orders[cycle.protective_sell_order_ref].status = "Cancelled"
        handled = self.controller._handle_protective_sell_order_poll(
            cycle, self.adapter.poll_order(cycle.protective_sell_order_ref),
        )
        self.assertTrue(handled)
        self.assertEqual(self.controller.active_cycle.stage, Stage.ERROR)
        self.assertFalse(self.adapter.placed_orders)
        self.assertFalse(self.storage.has_decision_event_dedupe_key(
            cycle_id=cycle.id, event_type="PROTECTIVE_HANDOFF_CANCEL_CONFIRMED",
            dedupe_key=cycle.protective_sell_order_ref,
        ))

    def test_cancel_exception_reconciles_and_pauses_without_false_confirmation(self):
        cycle, actions = self.cancel_action(self.working_cycle(), 102.0)
        with patch.object(self.adapter, "cancel_order", side_effect=BrokerAdapterError("cancel acknowledgement lost")):
            self.controller._execute_actions(actions, cycle)
        paused = self.controller.active_cycle
        self.assertEqual(paused.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(paused.protective_sell_status, "Submitted")
        self.assertTrue(paused.protective_sell_cancel_requested)
        self.assertTrue(paused.recovery_required)
        self.assertFalse(self.adapter.placed_orders)
        self.assertIn("Submitted", paused.error_message)
        self.assertEqual(self.storage.get_cycle(cycle.id).stage, Stage.MANUAL_REVIEW)

    def test_cancel_exception_retains_partial_fill_exposure(self):
        cycle = self.working_cycle()
        order = self.adapter.orders[cycle.protective_sell_order_ref]
        order.filled, order.remaining, order.avg_fill_price = 4, 6, 99.0
        self.controller._pause_failed_protective_cancellation(cycle, RuntimeError("unknown cancel outcome"))
        self.assertEqual(cycle.protective_sell_filled_qty, 4)
        self.assertEqual(self.controller._app_unsold_quantity(cycle), 6)
        self.assertEqual(self.storage.get_execution_totals(cycle.id, "PROTECTIVE_SELL")["shares"], 4)
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)

    def test_changed_price_after_protection_cancel_requires_recovery(self):
        cycle = self.working_cycle()
        cycle.protective_sell_status = "Cancelled"
        next_cycle, actions = StrategyEngine.on_price_update(cycle, 99.0)
        self.assertEqual(next_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(next_cycle.recovery_required)
        self.assertFalse(actions)

    def test_final_sell_block_after_cancel_cannot_silently_roll_back(self):
        cycle = self.working_cycle()
        cycle.protective_sell_status = "Cancelled"
        cycle, actions = StrategyEngine.on_price_update(cycle, 102.0)
        self.controller._rollback_final_sell_market_data_block(cycle, "quote aged during submission")
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.controller.active_cycle.recovery_required)
        self.assertIsNone(self.controller.active_cycle.sell_order_ref)
        self.assertTrue(actions)

    def test_invalid_quote_after_cancel_is_persisted_recovery(self):
        cycle = self.working_cycle()
        cycle.protective_sell_status = "Cancelled"
        self.controller.price_snapshot = {"market_data_field_tracking": True}
        next_cycle, actions = self.controller._advance_waiting_cycle_from_price(cycle, 102.0, is_rth=True, rth_message="open")
        self.assertEqual(next_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertFalse(actions)
        self.assertTrue(self.storage.get_cycle(cycle.id).recovery_required)

    def test_exact_drop_boundary_accepts_only_one_ulp_arithmetic_difference(self):
        settings = StrategySettings(ticker="AAPL", initial_drop_pct=3.0, buy_rebound_trail_pct=0.0)
        cycle = StrategyEngine.start_cycle(settings, 1, "DU_TEST", 95.0, 0.0)
        raw = 95.0 * (1.0 - 3.0 / 100.0)
        self.assertEqual(nextafter(raw, float("inf")), 92.15)
        waiting, actions = StrategyEngine.on_price_update(cycle, nextafter(92.15, float("inf")))
        self.assertEqual(waiting.stage, Stage.WAIT_INITIAL_DROP)
        self.assertFalse(actions)
        triggered, actions = StrategyEngine.on_price_update(cycle, 92.15)
        self.assertEqual(triggered.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(len(actions), 1)

    def test_exact_profit_boundary_accepts_only_one_ulp_arithmetic_difference(self):
        cycle, _ = self.cycle(protective_sell_enabled=False, rise_trigger_pct=10.0, sell_trailing_stop_pct=0.0)
        cycle.avg_buy_price = 95.0
        raw = StrategyEngine.recalculate_rise_trigger_price(cycle)
        self.assertEqual(nextafter(raw, -float("inf")), 104.5)
        waiting, actions = StrategyEngine.on_price_update(cycle, nextafter(104.5, -float("inf")))
        self.assertEqual(waiting.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(actions)
        triggered, actions = StrategyEngine.on_price_update(cycle, 104.5)
        self.assertEqual(triggered.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertEqual(len(actions), 1)

    def test_tiny_drop_requires_actual_movement_even_when_display_rounds_to_anchor(self):
        settings = StrategySettings(ticker="AAPL", initial_drop_pct=0.01, buy_rebound_trail_pct=0.0)
        cycle = StrategyEngine.start_cycle(settings, 1, "DU_TEST", 0.1, 0.0)
        same, actions = StrategyEngine.on_price_update(cycle, 0.1)
        self.assertEqual(same.drop_trigger_price, 0.1)
        self.assertEqual(same.stage, Stage.WAIT_INITIAL_DROP)
        self.assertFalse(actions)
        risen, actions = StrategyEngine.on_price_update(same, 0.11)
        self.assertFalse(actions)
        self.assertEqual(risen.anchor_price, 0.11)
        triggered, actions = StrategyEngine.on_price_update(risen, 0.10998)
        self.assertEqual(triggered.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual(len(actions), 1)

    def test_tiny_profit_and_buffer_are_not_lost_to_four_decimal_rounding(self):
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                cycle, _ = self.cycle(protective_sell_enabled=False, rise_trigger_pct=0.01, sell_trailing_stop_pct=0.0, slippage_buffer_enabled=buffered, slippage_buffer_pct=0.01)
                cycle.avg_buy_price = 0.1
                cycle.rise_trigger_price = 0.1
                same, actions = StrategyEngine.on_price_update(cycle, 0.1)
                self.assertEqual(same.stage, Stage.WAIT_RISE_TRIGGER)
                self.assertFalse(actions)
                actual = StrategyEngine.recalculate_rise_trigger_price(cycle)
                self.assertGreater(actual, 0.1)
                if buffered:
                    self.assertGreater(actual, 0.10001)
                next_cycle, actions = StrategyEngine.on_price_update(cycle, actual)
                self.assertEqual(next_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
                self.assertEqual(len(actions), 1)

    def test_tiny_profit_with_trailing_sell_requires_raw_activation(self):
        cycle, _ = self.cycle(protective_sell_enabled=False, rise_trigger_pct=0.01, sell_trailing_stop_pct=0.01)
        cycle.avg_buy_price = 0.1
        same, actions = StrategyEngine.on_price_update(cycle, 0.1)
        self.assertEqual(same.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(actions)
        threshold = StrategyEngine.recalculate_rise_trigger_price(cycle)
        triggered, actions = StrategyEngine.on_price_update(cycle, threshold)
        self.assertEqual(triggered.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertGreaterEqual(actions[0].payload["initial_stop_price"], 0.10001)

    def test_missing_protection_is_detected_before_waiting_for_a_quote(self):
        cycle, _ = self.cycle()
        cycle.protective_sell_order_ref = None
        self.controller.price_snapshot = {"market_data_field_tracking": True}
        next_cycle, actions = self.controller._advance_waiting_cycle_from_price(cycle, 99.0, is_rth=True, rth_message="open")
        self.assertEqual(next_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertFalse(actions)
        self.assertTrue(self.storage.get_cycle(cycle.id).recovery_required)

    def test_stage3_evidence_recalculates_stale_persisted_trigger(self):
        cycle, _ = self.cycle(protective_sell_enabled=False)
        cycle.rise_trigger_price = 1.0
        now = utc_now_iso()
        self.controller.price_snapshot = {
            "market_data_field_tracking": True, "upstream_connected": True,
            "market_data_update_sequence": 1, "market_data_subscription_id": "test",
            "subscription_market_data_type": 1, "fields": {"bid": 100.0, "ask": 100.01},
            "quote_update_sequence": 1, "quote_update_received_at": now,
            "field_update_received_at": {"bid": now, "ask": now},
        }
        evidence, message = self.controller._stage3_sell_quote_evidence(cycle, require_latest_event=False)
        self.assertIsNone(evidence)
        self.assertIn("does not confirm", message)


if __name__ == "__main__":
    unittest.main()
