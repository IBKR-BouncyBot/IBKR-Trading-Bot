"""Unavailable pre-send identity cannot become an uncertain transmitted order."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.ib_adapter import BrokerAdapterError
from app.models import Stage, StrategyAction
from tests import test_v500_uncertain_submission as submission_fixture
from tests.support.controller_harness import publish_fresh_price


class PretransmitOutageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v561_pretransmit_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def prepare(self, side="BUY", *, trail=False, protective=False, outcome="ok"):
        fixture = submission_fixture.UncertainSubmissionTests()
        fixture.controller_module = self.module
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        c, broker, cycle, action, prefix = fixture.prepare(side, trail=trail, protective=protective, outcome=outcome)
        self.addCleanup(c._market_capture.shutdown)
        broker.requires_exact_contract_selection = True
        return c, broker, cycle, action, prefix

    @staticmethod
    def lose_during_accounts(broker):
        def unavailable():
            broker.local_connected = broker.upstream_connected = False
            return []
        broker.managed_accounts = unavailable

    @staticmethod
    def restore(broker):
        broker.local_connected = broker.upstream_connected = True
        broker.managed_accounts = lambda: ["DU_TEST"]

    def assert_unsent_wait(self, c, broker, cycle, stage):
        saved = c.storage.get_cycle(cycle.id)
        self.assertEqual(saved.stage, stage)
        self.assertFalse(saved.recovery_required)
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(broker.cancelled_orders, [])
        self.assertTrue(c._upstream_recovery_pending)

    def waiting_position(self):
        c, broker, cycle, action, prefix = self.prepare("SELL")
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.sell_order_ref = None
        cycle.buy_status = "Filled"
        broker.external_position = 10.0
        c.storage.upsert_cycle(cycle)
        return c, broker, cycle

    def test_unsent_buy_and_sell_market_and_trailing_return_to_waiting(self):
        for side in ("BUY", "SELL"):
            for trail in (False, True):
                with self.subTest(side=side, trail=trail):
                    c, broker, cycle, action, _ = self.prepare(side, trail=trail)
                    self.lose_during_accounts(broker)
                    c._execute_actions([action], cycle)
                    self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP if side == "BUY" else Stage.WAIT_RISE_TRIGGER)
                    self.assertIsNone(c.active_cycle.buy_order_ref if side == "BUY" else c.active_cycle.sell_order_ref)

    def test_empty_ready_account_snapshot_defers(self):
        c, broker, cycle, action, _ = self.prepare()
        broker.managed_accounts = lambda: []
        c._execute_actions([action], cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)

    def test_wrong_confirmed_account_still_requires_review(self):
        c, broker, cycle, action, _ = self.prepare()
        broker.managed_accounts = lambda: ["WRONG"]
        c._execute_actions([action], cycle)
        self.assertEqual(c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(broker.placed_orders, [])

    def test_contract_lookup_timeout_is_known_unsent(self):
        c, broker, cycle, action, _ = self.prepare()
        c.contract = None
        with patch.object(broker, "qualify_stock", side_effect=TimeoutError("contract timeout")):
            c._execute_actions([action], cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)

    def test_protective_creation_still_reports_real_missing_protection(self):
        c, broker, cycle, action, _ = self.prepare("SELL", trail=True, protective=True)
        self.lose_during_accounts(broker)
        c._execute_actions([action], cycle)
        self.assertEqual(c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("protective SELL is not working", c.active_cycle.error_message)
        self.assertNotIn("not confirmed in the broker managed accounts", c.active_cycle.error_message)
        self.assertEqual(broker.placed_orders, [])

    def protected_position(self):
        c, broker, cycle, action, _ = self.prepare("SELL", trail=True, protective=True)
        cycle.buy_status = "Filled"
        broker.external_position = 10.0
        c.storage.upsert_cycle(cycle)
        c._execute_actions([action], cycle)
        cycle = c.active_cycle
        self.assertEqual(cycle.protective_sell_status, "Submitted")
        cancel = StrategyAction("CANCEL_ORDER", {
            "role": "protective_sell", "order_ref": cycle.protective_sell_order_ref,
            "order_id": cycle.protective_sell_order_id,
        })
        return c, broker, cycle, cancel

    def test_protective_cancel_preflight_outage_preserves_working_protection(self):
        c, broker, cycle, cancel = self.protected_position()
        ref = cycle.protective_sell_order_ref
        cycle.protective_sell_cancel_requested = True
        c.storage.upsert_cycle(cycle)
        self.lose_during_accounts(broker)
        c._execute_actions([cancel, cancel], cycle)
        saved = c.storage.get_cycle(cycle.id)
        self.assertEqual(saved.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(saved.recovery_required)
        self.assertFalse(saved.protective_sell_cancel_requested)
        self.assertEqual(saved.protective_sell_status, "Submitted")
        self.assertEqual(saved.protective_sell_order_ref, ref)
        self.assertEqual(broker.cancelled_orders, [])
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertTrue(c._upstream_recovery_pending)
        self.restore(broker)
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertFalse(c._upstream_recovery_pending)
        self.assertEqual(c.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(c.active_cycle.protective_sell_order_ref, ref)
        self.assertEqual(broker.cancelled_orders, [])
        self.assertEqual(len(broker.placed_orders), 1)

    def test_actual_uncertain_protective_cancel_keeps_manual_review(self):
        c, broker, cycle, cancel = self.protected_position()
        def uncertain_cancel(order_ref, order_id=None):
            broker.cancelled_orders.append(order_ref)
            raise BrokerAdapterError("connection lost after cancellation attempt")
        with (
            patch.object(c, "_protective_handoff_preflight_message", return_value=None),
            patch.object(broker, "cancel_order", side_effect=uncertain_cancel),
        ):
            c._execute_actions([cancel], cycle)
        self.assertEqual(len(broker.cancelled_orders), 1)
        self.assertEqual(c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(c.active_cycle.protective_sell_cancel_requested)
        self.assertTrue(c.active_cycle.recovery_required)
        self.assertEqual(len(broker.placed_orders), 1)

    def test_actual_post_send_uncertainty_is_unchanged(self):
        c, broker, cycle, action, _ = self.prepare(outcome="unknown")
        c._execute_actions([action, action], cycle)
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(c.active_cycle.buy_status, "SUBMISSION_UNKNOWN")

    def test_initial_manual_close_retries_existing_workflow_once_after_recovery(self):
        c, broker, cycle = self.waiting_position()
        self.lose_during_accounts(broker)
        c._request_market_close_for_app_position(cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(c._deferred_market_close_cycle_id, cycle.id)
        self.restore(broker)
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertIsNone(c._deferred_market_close_cycle_id)
        self.assertEqual(c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)

    def test_queued_close_survives_second_outage_at_connectivity_boundary(self):
        c, broker, cycle = self.waiting_position()
        self.lose_during_accounts(broker)
        c._request_market_close_for_app_position(cycle)
        self.restore(broker)
        def lose_on_pump(timeout=0):
            broker.local_connected = broker.upstream_connected = False
        with patch.object(broker, "process_events", side_effect=lose_on_pump):
            self.assertFalse(c._recover_after_connect())
        self.assertEqual(c._deferred_market_close_cycle_id, cycle.id)
        self.assertTrue(c._upstream_recovery_pending)
        self.assertFalse(c.active_cycle.close_position_market_requested)
        self.assertTrue(c.active_cycle.stop_after_current_cycle)
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(broker.cancelled_orders, [])
        self.restore(broker)
        self.assertTrue(c._recover_after_connect())
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertIsNone(c._deferred_market_close_cycle_id)

    def test_close_request_prevents_repeat_if_sell_finishes_during_outage(self):
        c, broker, cycle, action, _ = self.prepare("SELL")
        cycle.buy_status = "Filled"
        broker.external_position = 10.0
        c.storage.upsert_cycle(cycle)
        c._execute_actions([action], cycle)
        cycle = c.active_cycle
        self.assertEqual(len(broker.placed_orders), 1)
        self.lose_during_accounts(broker)
        c._request_market_close_for_app_position(cycle)
        self.assertTrue(c.storage.get_cycle(cycle.id).stop_after_current_cycle)
        self.restore(broker)
        broker.fill_order(cycle.sell_order_ref, shares=10, price=100, execution_id="EXIT-DURING-OUTAGE")
        broker.events.clear()
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertEqual(c.storage.get_cycle(cycle.id).stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(c.active_cycle.id, cycle.id)
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertIsNone(c._deferred_market_close_cycle_id)

    def test_already_requested_manual_close_retains_request_through_outage(self):
        c, broker, cycle = self.waiting_position()
        cycle.close_position_market_requested = True
        c.storage.upsert_cycle(cycle)
        self.lose_during_accounts(broker)
        self.assertFalse(c._submit_requested_market_close(cycle))
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_RISE_TRIGGER)
        self.assertTrue(c.storage.get_cycle(cycle.id).close_position_market_requested)
        self.restore(broker)
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertEqual(len(broker.placed_orders), 1)

    def test_initial_queued_close_rechecks_rth_after_recovery(self):
        c, broker, cycle = self.waiting_position()
        self.lose_during_accounts(broker)
        c._request_market_close_for_app_position(cycle)
        self.restore(broker)
        broker.rth_open = False
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(c.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)

    def test_autorepeat_is_not_permanently_stopped_by_missing_account_response(self):
        c, broker, cycle, _, _ = self.prepare("SELL")
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.sell_order_ref = None
        cycle.sell_status = cycle.buy_status = "Filled"
        cycle.sell_filled_qty = cycle.buy_filled_qty
        c.storage.upsert_cycle(cycle)
        self.lose_during_accounts(broker)
        c._maybe_start_next_cycle()
        self.assert_unsent_wait(c, broker, cycle, Stage.CYCLE_COMPLETE)
        self.assertFalse(c.storage.get_cycle(cycle.id).stop_after_current_cycle)
        self.restore(broker)
        c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(c._recover_upstream_session_if_needed())
        self.assertEqual(c.active_cycle.id, cycle.id)
        publish_fresh_price(c, broker, 100.0)
        c._maybe_start_next_cycle()
        self.assertNotEqual(c.active_cycle.id, cycle.id)
        self.assertEqual(c.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertEqual(broker.placed_orders, [])

    def test_legacy_stage1_account_read_outage_does_not_create_a_hold(self):
        c, broker, cycle, _, _ = self.prepare()
        cycle.stage = Stage.WAIT_INITIAL_DROP
        cycle.account = ""
        cycle.buy_order_ref = None
        c.connection.account = ""
        c.storage.upsert_cycle(cycle)
        self.lose_during_accounts(broker)
        _, actions = c._advance_waiting_cycle_from_price(cycle, 100.0, is_rth=True, rth_message="test")
        self.assertEqual(actions, [])
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)

    def test_legacy_account_evidence_read_disconnect_remains_known_unsent(self):
        c, broker, cycle, action, _ = self.prepare()
        cycle.account = ""
        c.connection.account = ""
        c.storage.upsert_cycle(cycle)
        def disconnected_order(_ref):
            broker.local_connected = broker.upstream_connected = False
            return None
        with patch.object(broker, "poll_order", side_effect=disconnected_order):
            c._execute_actions([action], cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)

    def test_legacy_account_evidence_request_timeout_remains_known_unsent(self):
        c, broker, cycle, action, _ = self.prepare()
        cycle.account = ""
        c.connection.account = ""
        c.storage.upsert_cycle(cycle)
        with patch.object(broker, "poll_order", side_effect=TimeoutError("legacy evidence request timeout")):
            c._execute_actions([action], cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)

    def test_legacy_execution_evidence_timeout_is_not_treated_as_empty(self):
        c, broker, cycle, action, _ = self.prepare()
        cycle.account = ""
        c.connection.account = ""
        c.storage.upsert_cycle(cycle)
        broker.poll_order = Mock(return_value=None)
        broker.recovery_recent_executions = Mock(side_effect=TimeoutError("execution evidence timeout"))
        broker.recent_executions = Mock(side_effect=AssertionError("ordinary execution read used"))
        c._execute_actions([action], cycle)
        self.assert_unsent_wait(c, broker, cycle, Stage.WAIT_INITIAL_DROP)
        broker.recovery_recent_executions.assert_called_once_with()
        broker.recent_executions.assert_not_called()

    def test_identity_deferral_stops_remaining_action_batch(self):
        c, broker, cycle, action, _ = self.prepare()
        broker.managed_accounts = Mock(side_effect=[[], ["DU_TEST"]])
        c._execute_actions([action, action], cycle)
        self.assertEqual(broker.managed_accounts.call_count, 1)
        self.assertEqual(broker.placed_orders, [])

    def test_disconnect_clears_queued_preflight_requests(self):
        c, _, cycle, _, _ = self.prepare()
        c._deferred_market_close_cycle_id = cycle.id
        c._deferred_repeat_cycle_id = cycle.id
        c._disconnect()
        self.assertIsNone(c._deferred_market_close_cycle_id)
        self.assertIsNone(c._deferred_repeat_cycle_id)


if __name__ == "__main__":
    unittest.main()
