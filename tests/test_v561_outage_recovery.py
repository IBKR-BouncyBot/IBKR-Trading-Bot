"""Recovery reads defer transient outages without promoting or losing exposure."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.ib_adapter import BrokerAdapterError
from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class ExactRecoveryBroker(DeterministicBrokerAdapter):
    requires_exact_contract_selection = True


class OutageRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v561_outage_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.broker = ExactRecoveryBroker()
        self.broker.accounts = ["SIM"]
        self.settings = permissive_strategy()
        self.settings.contract_con_id = self.broker.contract.con_id
        self.c = make_controller(self.module, Path(folder.name) / "state.sqlite", self.broker, self.settings)
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._start_trade_market_data_capture = lambda *args, **kwargs: None
        self.cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100.0, 0.0)
        self.cycle.stage = Stage.WAIT_RISE_TRIGGER
        self.cycle.buy_filled_qty = self.cycle.quantity = 10
        self.cycle.buy_status = "Filled"
        self.cycle.avg_buy_price = 100.0
        self.broker.external_position = 10.0
        self.save()

    def save(self):
        self.c.active_cycle = self.cycle
        self.c.storage.upsert_cycle(self.cycle)

    def assert_deferred(self):
        saved = self.c.storage.get_cycle(self.cycle.id)
        self.assertEqual(saved.stage, self.cycle.stage)
        self.assertFalse(saved.recovery_required)
        self.assertTrue(self.c._upstream_recovery_pending)
        self.assertIn("trading remains paused", self.c.status)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_stage3_qualification_disconnect_preserves_persisted_stage(self):
        before = self.c.storage.get_cycle(self.cycle.id).to_dict()
        def fail(*args, **kwargs):
            self.broker.local_connected = False
            raise BrokerAdapterError("Not connected to TWS.")
        with patch.object(self.broker, "qualify_stock", side_effect=fail):
            self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()
        self.assertEqual(self.c.storage.get_cycle(self.cycle.id).to_dict(), before)

    def test_qualification_timeout_defers_even_if_socket_still_open(self):
        with patch.object(self.broker, "qualify_stock", side_effect=TimeoutError()):
            self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_stage1_qualification_disconnect_preserves_stage(self):
        self.cycle.stage = Stage.WAIT_INITIAL_DROP
        self.cycle.buy_filled_qty = self.cycle.quantity = 0
        self.save()
        with patch.object(self.broker, "qualify_stock", side_effect=BrokerAdapterError("Not connected to TWS.")):
            self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_healthy_retry_recovers_same_cycle_without_order(self):
        with patch.object(self.broker, "qualify_stock", side_effect=TimeoutError()):
            self.assertFalse(self.c._recover_after_connect())
        self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.id, self.cycle.id)
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(self.c._upstream_recovery_pending)
        self.assertFalse(self.c.active_cycle.recovery_required)
        self.assertEqual(self.broker.placed_orders, [])

    def test_empty_managed_accounts_defers_pinned_account(self):
        self.broker.accounts = []
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_wrong_nonempty_managed_accounts_still_requires_manual_review(self):
        self.broker.accounts = ["OTHER"]
        self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("not confirmed", self.c.active_cycle.error_message)

    def test_managed_account_request_failure_defers(self):
        with patch.object(self.broker, "managed_accounts", side_effect=RuntimeError("request incomplete")):
            self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_open_order_request_failure_defers(self):
        self.broker.recovery_open_app_orders = Mock(side_effect=BrokerAdapterError("request incomplete"))
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_execution_request_failure_cannot_be_treated_as_empty(self):
        self.broker.recovery_recent_executions = Mock(side_effect=BrokerAdapterError("request incomplete"))
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_recovery_uses_strict_snapshots_once(self):
        self.broker.recovery_recent_executions = Mock(return_value=[])
        self.broker.recovery_open_app_orders = Mock(return_value=[])
        self.broker.recent_executions = Mock(side_effect=AssertionError("ordinary read used"))
        self.broker.open_app_orders = Mock(side_effect=AssertionError("ordinary read used"))
        self.assertTrue(self.c._recover_after_connect())
        self.broker.recovery_recent_executions.assert_called_once_with()
        self.broker.recovery_open_app_orders.assert_called_once_with()
        self.assertIsNone(self.c._recovery_execution_rows)
        self.assertFalse(self.c._recovery_read_active)

    def test_disconnect_after_successful_empty_order_response_defers(self):
        def response():
            self.broker.local_connected = False
            return []
        self.broker.recovery_open_app_orders = response
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_disconnect_after_successful_empty_execution_response_defers(self):
        def response():
            self.broker.upstream_connected = False
            return []
        self.broker.recovery_recent_executions = response
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_position_request_exception_defers_without_cached_fallback(self):
        self.broker.recovery_position_size = Mock(side_effect=TimeoutError("position timeout"))
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_disconnect_after_position_response_defers(self):
        def response(*args, **kwargs):
            self.broker.local_connected = False
            return 10.0
        self.broker.recovery_position_size = response
        self.assertFalse(self.c._recover_after_connect())
        self.assert_deferred()

    def test_connected_unknown_position_remains_manual_review(self):
        self.broker.recovery_position_size = Mock(return_value=None)
        self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("unavailable", self.c.active_cycle.error_message)

    def test_real_position_shortage_remains_manual_review(self):
        self.broker.recovery_position_size = Mock(return_value=9.0)
        self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("9 shares", self.c.active_cycle.error_message)

    def test_true_contract_identity_mismatch_remains_manual_review(self):
        with patch.object(self.broker, "qualify_stock", return_value=replace(self.broker.contract, con_id=999)):
            self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("identity", self.c.active_cycle.error_message)

    def test_semantic_qualification_failure_remains_manual_review(self):
        with patch.object(self.broker, "qualify_stock", side_effect=BrokerAdapterError("No matching stock contract")):
            self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_automatic_recovery_never_attempts_to_clear_saved_hold(self):
        self.cycle.stage = Stage.MANUAL_REVIEW
        self.cycle.recovery_required = True
        self.cycle.error_message = "Uncertain submission"
        self.save()
        self.c._restore_outage_waiting_cycle = Mock(return_value=True)
        self.assertTrue(self.c._recover_after_connect())
        self.c._restore_outage_waiting_cycle.assert_not_called()
        self.assertTrue(self.c.active_cycle.recovery_required)

    def test_explicit_reconcile_delegates_hold_clearance_only_to_narrow_helper(self):
        self.cycle.stage = Stage.MANUAL_REVIEW
        self.cycle.recovery_required = True
        self.cycle.error_message = "Uncertain submission"
        self.save()
        self.c._restore_outage_waiting_cycle = Mock(return_value=False)
        self.c._resume_recovery_monitoring()
        self.c._restore_outage_waiting_cycle.assert_called_once()
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("still requires manual review", self.c.status)

    def test_start_mid_qualification_disconnect_defers(self):
        with patch.object(self.broker, "qualify_stock", side_effect=BrokerAdapterError("Not connected to TWS.")):
            self.c._start_strategy(self.settings)
        self.assert_deferred()

    def test_start_on_saved_hold_does_not_log_resumed(self):
        self.cycle.stage = Stage.MANUAL_REVIEW
        self.cycle.recovery_required = True
        self.cycle.error_message = "Uncertain submission"
        self.save()
        self.c._log = Mock()
        self.c._start_strategy(self.settings)
        messages = [str(call.args[1]) for call in self.c._log.call_args_list]
        self.assertFalse(any("Resumed existing" in message for message in messages))
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_repeated_identical_failure_does_not_write_cycle_or_repeat_log(self):
        self.c._log = Mock()
        with patch.object(self.c.storage, "upsert_cycle", wraps=self.c.storage.upsert_cycle) as write:
            with patch.object(self.broker, "qualify_stock", side_effect=TimeoutError("timeout")):
                self.assertFalse(self.c._recover_after_connect())
                self.assertFalse(self.c._recover_after_connect())
            write.assert_not_called()
        self.assertEqual(self.c._log.call_count, 1)

    def test_repeated_position_timeout_does_not_rewrite_cycle_or_ledger(self):
        self.c._log = Mock()
        before = self.c.storage.get_cycle(self.cycle.id).to_dict()
        self.broker.recovery_position_size = Mock(side_effect=TimeoutError("position request timeout"))
        with (
            patch.object(self.c.storage, "upsert_cycle", wraps=self.c.storage.upsert_cycle) as cycle_write,
            patch.object(self.c.storage, "upsert_execution", wraps=self.c.storage.upsert_execution) as execution_write,
            patch.object(self.c.storage, "add_decision_event", wraps=self.c.storage.add_decision_event) as decision_write,
        ):
            self.assertFalse(self.c._recover_after_connect())
            self.assertFalse(self.c._recover_after_connect())
            cycle_write.assert_not_called()
            execution_write.assert_not_called()
            decision_write.assert_not_called()
        self.assertEqual(self.c.storage.get_cycle(self.cycle.id).to_dict(), before)
        self.assertEqual(self.c._log.call_count, 1)
        self.assert_deferred()

    def test_upstream_retry_keeps_existing_two_second_cadence(self):
        self.c._upstream_recovery_pending = True
        self.c._last_upstream_recovery_attempt_monotonic = 0.0
        with patch.object(self.broker, "qualify_stock", side_effect=TimeoutError("timeout")) as qualify:
            self.assertFalse(self.c._recover_upstream_session_if_needed())
            self.assertFalse(self.c._recover_upstream_session_if_needed())
        self.assertEqual(qualify.call_count, 1)
        self.assert_deferred()

    def held_cycle(self):
        self.cycle.stage = Stage.MANUAL_REVIEW
        self.cycle.recovery_required = True
        self.cycle.error_message = "Legacy outage hold"
        self.save()

    def test_explicit_reconcile_request_survives_transient_retry(self):
        self.held_cycle()
        helper = Mock(side_effect=[self.module._RecoveryReadDeferred("retry"), False])
        self.c._restore_outage_waiting_cycle = helper
        self.c._resume_recovery_monitoring()
        self.assertEqual(self.c._outage_recovery_requested_cycle_id, self.cycle.id)
        self.assertTrue(self.c._upstream_recovery_pending)
        self.c._last_upstream_recovery_attempt_monotonic = 0.0
        self.assertTrue(self.c._recover_upstream_session_if_needed())
        self.assertEqual(helper.call_count, 2)
        self.assertIsNone(self.c._outage_recovery_requested_cycle_id)

    def test_reconcile_request_cannot_transfer_to_another_cycle(self):
        self.held_cycle()
        self.c._outage_recovery_requested_cycle_id = "different-cycle-id"
        self.c._restore_outage_waiting_cycle = Mock(return_value=False)
        self.assertTrue(self.c._recover_after_connect())
        self.c._restore_outage_waiting_cycle.assert_not_called()
        self.assertIsNone(self.c._outage_recovery_requested_cycle_id)
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_genuine_review_block_finishes_explicit_request(self):
        self.held_cycle()
        self.c._restore_outage_waiting_cycle = Mock(return_value=False)
        self.c._resume_recovery_monitoring()
        self.assertIsNone(self.c._outage_recovery_requested_cycle_id)
        self.assertFalse(self.c._upstream_recovery_pending)
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_operator_disconnect_clears_pending_reconcile_request(self):
        self.c._outage_recovery_requested_cycle_id = self.cycle.id
        self.c._disconnect()
        self.assertIsNone(self.c._outage_recovery_requested_cycle_id)

    def test_start_during_transient_recovery_reports_waiting(self):
        self.c._upstream_recovery_pending = True
        with patch.object(self.c, "_recover_upstream_session_if_needed", return_value=False):
            self.c._handle_command("START_STRATEGY", {
                "connection": self.c.connection, "strategy": self.settings,
            })
        self.assertTrue(self.c.status.startswith("Waiting to start strategy:"))
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)

    def test_reconcile_reports_current_evidence_blocker(self):
        self.held_cycle()
        def blocked(*args):
            self.c._outage_reconcile_blocker = "Broker position is 9 shares; 10 shares are required."
            return False
        self.c._restore_outage_waiting_cycle = blocked
        self.c._resume_recovery_monitoring()
        self.assertIn("Broker position is 9 shares", self.c.status)
        self.assertNotIn("Legacy outage hold", self.c.status)

    def test_old_evidence_blocker_is_cleared_for_an_unrequested_hold(self):
        self.held_cycle()
        self.c._outage_reconcile_blocker = "An earlier cycle's position was unavailable."
        self.assertTrue(self.c._recover_after_connect())
        self.assertEqual(self.c.status, "Legacy outage hold")
        self.assertEqual(self.c._outage_reconcile_blocker, "")

    def test_manual_hold_event_records_original_stage(self):
        self.c._mark_recovery_required(self.cycle, "genuine mismatch")
        events = self.c.storage.get_cycle_audit_bundle(self.cycle.id)["decision_events"]
        matching = [row for row in events if row["event_type"] == "RECOVERY_REQUIRED"]
        self.assertEqual(matching[-1]["stage_before"], Stage.WAIT_RISE_TRIGGER.value)
        self.assertEqual(matching[-1]["stage_after"], Stage.MANUAL_REVIEW.value)


if __name__ == "__main__":
    unittest.main()
