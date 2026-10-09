"""Explicit recovery of proven waiting cycles held by interrupted broker reads."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ib_adapter import QualifiedContract
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings
from app.storage import BotStorage


class LegacyOutageRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._legacy_outage_test_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController
        cls.module = module

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = BotStorage(Path(self.folder.name) / "outage.sqlite")
        self.controller = self.controller_type(self.storage)
        self.addCleanup(self.controller._market_capture.shutdown)
        self.settings = StrategySettings(ticker="AAA", contract_con_id=111, atr_adaptive_enabled=False, protective_sell_enabled=False)
        self.controller.strategy = self.settings
        self.controller.connection = ConnectionSettings(account="")
        self.controller.connected = True
        self.controller.emit_snapshot = Mock()
        self.contract = QualifiedContract("AAA", 111, SimpleNamespace(), currency="USD")
        self.accounts = ["ACCOUNT_A"]
        self.adapter = SimpleNamespace(
            connected=True,
            requires_exact_contract_selection=True,
            managed_accounts=Mock(side_effect=lambda: list(self.accounts)),
            is_connected=lambda: self.adapter.connected,
            qualify_stock=Mock(side_effect=lambda *args: self.contract),
            open_app_orders=Mock(return_value=[]),
            recent_executions=Mock(return_value=[]),
            position_size=Mock(return_value=0.0),
            poll_order=Mock(return_value=None),
            cancel_order=Mock(),
            place_order=Mock(),
            place_market_order=Mock(),
            place_trailing_order=Mock(),
        )
        self.controller.adapter = self.adapter
        self.cycle = CycleState.new(self.settings, 1, "ACCOUNT_A", 100.0, 0.0)
        self.cycle.quantity = 0
        self.ref = "IBKRBOT|AAA|CYCLE-000001|test-outage|BUY_TRAIL"

    def store_execution(self, **changes):
        values = {
            "cycle": self.cycle, "ticker": "AAA", "side": "BUY", "shares": 10,
            "price": 100.0, "avg_price": 100.0, "currency": "USD", "order_ref": self.ref,
            "order_id": 101, "perm_id": 201, "execution_id": "BUY-1",
            "raw": {"account": "ACCOUNT_A", "con_id": 111, "order_ref": self.ref},
        }
        values.update(changes)
        self.storage.add_execution(**values)

    def make_hold(self, stage=Stage.WAIT_INITIAL_DROP, *, reason=None, prior=True, legacy=False, ledger=True):
        self.cycle.stage = stage
        if stage == Stage.WAIT_RISE_TRIGGER:
            self.cycle.quantity = 10
            self.cycle.buy_filled_qty = 10
            self.cycle.avg_buy_price = 100.0
            self.cycle.buy_status = "Filled"
            self.cycle.buy_order_ref = self.ref
            self.cycle.buy_order_id = 101
            self.cycle.buy_perm_id = 201
            self.storage.upsert_cycle(self.cycle)
            self.storage.add_order(
                cycle=self.cycle, action="BUY", order_type="TRAIL", order_id=101,
                perm_id=201, order_ref=self.ref, quantity=10, trailing_percent=1,
                initial_stop_price=100, status="Filled",
            )
            if ledger:
                if legacy:
                    self.store_execution(raw={"raw_args": [
                        "Fill(contract=Stock(conId=111, symbol='AAA', currency='USD'), "
                        "execution=Execution(execId='BUY-1', acctNumber='ACCOUNT_A', "
                        f"orderRef={self.ref!r}, orderId=101, permId=201, side='BOT'))",
                    ]})
                else:
                    self.store_execution()
            self.adapter.position_size.return_value = 10.0
        self.storage.upsert_cycle(self.cycle)
        if prior:
            self.storage.add_decision_event(
                event_type="ATR_ADAPTIVE_UPDATE", cycle=self.cycle, message="Prior waiting-stage evidence.",
                stage_before=stage.value, stage_after=stage.value, decision_result="applied",
            )
        default_reason = (
            "Cannot qualify the stored position contract: Not connected to TWS."
            if stage == Stage.WAIT_RISE_TRIGGER else
            "Cycle account ACCOUNT_A is not confirmed in the broker managed accounts."
        )
        self.controller._mark_recovery_required(self.cycle, reason or default_reason)
        # Legacy releases did not retain the previous stage on the hold itself.
        with self.storage.connect() as con:
            con.execute("UPDATE decision_events SET stage_before=NULL WHERE cycle_id=? AND event_type='RECOVERY_REQUIRED'", (self.cycle.id,))
        self.controller.active_cycle = self.cycle
        return self.cycle

    def save_cycle(self):
        self.storage.upsert_cycle(self.cycle)

    def restore(self, open_orders=None):
        self.controller._recovery_read_active = True
        try:
            return self.controller._restore_outage_waiting_cycle(self.cycle, open_orders or [])
        finally:
            self.controller._recovery_read_active = False

    def assert_no_broker_mutations(self):
        for name in ("cancel_order", "place_order", "place_market_order", "place_trailing_order"):
            getattr(self.adapter, name).assert_not_called()

    def assert_restore_blocked(self, open_orders=None):
        before = self.storage.get_cycle_audit_bundle(self.cycle.id)
        try:
            self.restore(open_orders)
        except (self.module._RecoveryReadDeferred, TimeoutError):
            pass
        self.assertEqual(self.storage.get_cycle_audit_bundle(self.cycle.id), before)
        self.assertEqual(self.cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.cycle.recovery_required)
        self.assert_no_broker_mutations()

    def test_unexposed_stage_one_restores_only_after_authoritative_checks(self):
        self.make_hold()
        self.restore()
        saved = self.storage.get_cycle(self.cycle.id)
        self.assertEqual(saved.stage, Stage.WAIT_INITIAL_DROP)
        self.assertFalse(saved.recovery_required)
        self.assertFalse(saved.error_message)
        self.assertEqual(saved.account, "ACCOUNT_A")
        self.assertEqual(saved.con_id, 111)
        self.adapter.position_size.assert_called_with(self.contract, account="ACCOUNT_A", refresh=True)
        decisions = self.storage.get_cycle_audit_bundle(self.cycle.id)["decision_events"]
        self.assertEqual(decisions[-1]["stage_before"], Stage.MANUAL_REVIEW.value)
        self.assertEqual(decisions[-1]["stage_after"], Stage.WAIT_INITIAL_DROP.value)
        self.assert_no_broker_mutations()

    def test_settled_stage_three_restores_without_resubmitting_buy(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        before = self.storage.get_cycle_audit_bundle(self.cycle.id)
        self.restore()
        after = self.storage.get_cycle_audit_bundle(self.cycle.id)
        self.assertEqual(self.storage.get_cycle(self.cycle.id).stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(after["orders"], before["orders"])
        self.assertEqual(after["executions"], before["executions"])
        self.assertEqual(self.controller.active_cycle.buy_filled_qty, 10)
        self.assert_no_broker_mutations()

    def test_legacy_archived_fill_ownership_can_prove_settled_buy(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER, legacy=True)
        self.restore()
        self.assertEqual(self.storage.get_cycle(self.cycle.id).stage, Stage.WAIT_RISE_TRIGGER)
        self.assert_no_broker_mutations()

    def test_external_additional_account_position_is_not_adopted(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.adapter.position_size.return_value = 25
        self.restore()
        saved = self.storage.get_cycle(self.cycle.id)
        self.assertEqual(saved.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(saved.buy_filled_qty, 10)
        self.assertEqual(saved.quantity, 10)
        self.assert_no_broker_mutations()

    def test_unknown_submission_reason_stays_manual_review(self):
        self.make_hold(reason="Broker submission outcome is unknown; verify exact order.")
        self.assert_restore_blocked()

    def test_other_qualification_error_cannot_use_disconnect_repair(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER, reason="Cannot qualify the stored position contract: Ambiguous contract.")
        self.assert_restore_blocked()

    def test_missing_prior_stage_does_not_infer_stage_from_zero_position(self):
        self.make_hold(prior=False)
        self.assert_restore_blocked()

    def test_latest_conflicting_stage_does_not_skip_back_to_old_waiting_stage(self):
        self.make_hold()
        with self.storage.connect() as con:
            hold_id = con.execute("SELECT MAX(id) FROM decision_events WHERE cycle_id=?", (self.cycle.id,)).fetchone()[0]
            con.execute("UPDATE decision_events SET stage_after=? WHERE id=?", (Stage.SELL_TRAIL_ACTIVE.value, hold_id - 1))
        self.assert_restore_blocked()

    def test_hold_audit_reason_must_match_saved_reason(self):
        self.make_hold()
        with self.storage.connect() as con:
            con.execute("UPDATE decision_events SET message=? WHERE event_type='RECOVERY_REQUIRED'", ("Uncertain submission",))
        self.assert_restore_blocked()

    def test_multiple_unresolved_cycles_block_restore(self):
        self.make_hold()
        other = CycleState.new(self.settings, 2, "ACCOUNT_A", 100, 0)
        self.storage.upsert_cycle(other)
        self.assert_restore_blocked()

    def test_missing_or_conflicting_identity_blocks_restore(self):
        self.make_hold()
        for field, value in (("account", ""), ("con_id", 0), ("con_id", -1)):
            with self.subTest(field=field, value=value):
                previous = getattr(self.cycle, field)
                setattr(self.cycle, field, value)
                self.save_cycle()
                self.assert_restore_blocked()
                setattr(self.cycle, field, previous)
        self.save_cycle()

    def test_missing_or_different_managed_account_blocks_restore(self):
        self.make_hold()
        for accounts in ([], ["ACCOUNT_B"]):
            with self.subTest(accounts=accounts):
                self.accounts[:] = accounts
                self.assert_restore_blocked()

    def test_different_explicit_configured_account_blocks_restore(self):
        self.make_hold()
        self.controller.connection.account = "ACCOUNT_B"
        self.assert_restore_blocked()

    def test_working_exact_known_order_blocks_restore(self):
        self.make_hold()
        order = SimpleNamespace(order_ref=self.ref, status="Submitted", filled=0, remaining=10)
        self.assert_restore_blocked([order])

    def test_unsubmitted_order_intent_blocks_stage_one_restore(self):
        self.make_hold()
        self.storage.create_order_intent(
            cycle=self.cycle, action="BUY", order_type="TRAIL", order_ref=self.ref,
            quantity=10, trailing_percent=1, initial_stop_price=100,
        )
        self.assert_restore_blocked()

    def test_stage_one_cannot_hide_buy_reference_or_fill(self):
        self.make_hold()
        for field, value in (("buy_order_ref", self.ref), ("buy_order_id", 101), ("buy_perm_id", 201), ("buy_status", "Filled"), ("buy_filled_qty", 10)):
            with self.subTest(field=field):
                previous = getattr(self.cycle, field)
                setattr(self.cycle, field, value)
                self.save_cycle()
                self.assert_restore_blocked()
                setattr(self.cycle, field, previous)
        self.save_cycle()

    def test_exit_and_manual_close_state_cannot_be_cleared(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for field, value in (
            ("sell_order_ref", "IBKRBOT|AAA|EXIT"), ("sell_order_id", 301),
            ("sell_perm_id", 401), ("sell_status", "Cancelled"), ("sell_filled_qty", 1),
            ("protective_sell_order_ref", "IBKRBOT|AAA|PROTECTION"),
            ("close_position_market_requested", True), ("close_before_rth_liquidation_requested", True),
            ("close_before_rth_cancel_requested", True), ("buy_remainder_cancel_requested", True),
        ):
            with self.subTest(field=field):
                previous = getattr(self.cycle, field)
                setattr(self.cycle, field, value)
                self.save_cycle()
                self.assert_restore_blocked()
                setattr(self.cycle, field, previous)
        self.save_cycle()

    def test_enabled_protection_requires_separate_reconciliation(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.cycle.protective_sell_enabled = True
        self.save_cycle()
        self.assert_restore_blocked()

    def test_stage_three_requires_stored_fully_settled_buy(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.storage.update_order_status(self.ref, "Submitted")
        self.assert_restore_blocked()

    def test_stage_three_requires_persisted_execution_ledger(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER, ledger=False)
        self.assert_restore_blocked()

    def test_inconsistent_execution_identity_or_quantity_blocks_restore(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for changes in (
            {"shares": 9}, {"order_ref": "IBKRBOT|AAA|OTHER"}, {"order_id": 102},
            {"perm_id": 202}, {"ticker": "BBB"}, {"currency": "EUR"}, {"side": "SELL"},
            {"raw": {"account": "ACCOUNT_B", "con_id": 111}},
            {"raw": {"account": "ACCOUNT_A", "con_id": 222}}, {"raw": {}},
        ):
            with self.subTest(changes=changes):
                with self.storage.connect() as con:
                    con.execute("DELETE FROM executions WHERE cycle_id=?", (self.cycle.id,))
                self.store_execution(**changes)
                self.assert_restore_blocked()

    def test_inconsistent_persisted_buy_order_blocks_restore(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for column, value in (("quantity", 9), ("order_id", 102), ("perm_id", 202), ("action", "SELL"), ("order_ref", "IBKRBOT|AAA|OTHER")):
            with self.subTest(column=column):
                with self.storage.connect() as con:
                    old = con.execute(f"SELECT {column} FROM orders WHERE cycle_id=?", (self.cycle.id,)).fetchone()[0]
                    con.execute(f"UPDATE orders SET {column}=? WHERE cycle_id=?", (value, self.cycle.id))
                self.assert_restore_blocked()
                with self.storage.connect() as con:
                    con.execute(f"UPDATE orders SET {column}=? WHERE cycle_id=?", (old, self.cycle.id))

    def test_qualified_contract_identity_must_remain_exact(self):
        self.make_hold()
        original = self.contract
        for changes in ({"con_id": 222}, {"ticker": "BBB"}, {"currency": "EUR"}, {"sec_type": "OPT"}):
            with self.subTest(changes=changes):
                self.contract = replace(original, **changes)
                self.assert_restore_blocked()

    def test_unknown_nonfinite_or_short_position_blocks_restore(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for position in (None, float("nan"), float("inf"), -1, 9):
            with self.subTest(position=position):
                self.adapter.position_size.return_value = position
                self.assert_restore_blocked()

    def test_stage_one_also_requires_known_nonnegative_position(self):
        self.make_hold()
        for position in (None, float("nan"), -1):
            with self.subTest(position=position):
                self.adapter.position_size.return_value = position
                self.assert_restore_blocked()

    def test_fresh_read_failures_never_persist_a_restored_cycle(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for name in ("managed_accounts", "qualify_stock", "position_size"):
            with self.subTest(name=name):
                method = getattr(self.adapter, name)
                original = method.side_effect
                method.side_effect = TimeoutError("Interrupted broker read")
                try:
                    self.assert_restore_blocked()
                finally:
                    method.side_effect = original

    def test_disconnect_during_final_position_read_preserves_hold(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)

        def disconnected_position(*args, **kwargs):
            self.adapter.connected = False
            return 10

        self.adapter.position_size.side_effect = disconnected_position
        self.assert_restore_blocked()

    def test_fresh_new_execution_for_known_buy_blocks_restore(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.adapter.recent_executions.return_value = [{
            "execution_id": "NEW-BUY", "order_ref": self.ref, "side": "BUY", "shares": 1,
            "price": 100, "account": "ACCOUNT_A", "con_id": 111, "ticker": "AAA",
            "order_id": 101, "perm_id": 201,
        }]
        self.assert_restore_blocked()

    def test_fresh_conflicting_existing_execution_blocks_restore(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.adapter.recent_executions.return_value = [{
            "execution_id": "BUY-1", "order_ref": self.ref, "side": "BUY", "shares": 10,
            "price": 101, "account": "ACCOUNT_A", "con_id": 111, "ticker": "AAA",
            "order_id": 101, "perm_id": 201,
        }]
        self.assert_restore_blocked()

    def test_already_persisted_fresh_execution_does_not_double_count(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.adapter.recent_executions.return_value = [{
            "execution_id": "BUY-1", "order_ref": self.ref, "side": "BUY", "shares": 10,
            "price": 100, "account": "ACCOUNT_A", "con_id": 111, "ticker": "AAA",
            "order_id": 101, "perm_id": 201,
        }]
        self.restore()
        self.assertEqual(self.storage.get_cycle(self.cycle.id).stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(len(self.storage.get_cycle_audit_bundle(self.cycle.id)["executions"]), 1)
        self.assert_no_broker_mutations()

    def test_automatic_reconnect_does_not_clear_existing_manual_review(self):
        self.make_hold()
        self.controller._recover_after_connect()
        self.assertEqual(self.storage.get_cycle(self.cycle.id).stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.storage.get_cycle(self.cycle.id).recovery_required)
        self.assert_no_broker_mutations()

    def test_explicit_resume_recovers_proven_waiting_cycle(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.controller._resume_recovery_monitoring()
        self.assertEqual(self.storage.get_cycle(self.cycle.id).stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(self.controller._recovery_required)
        self.assertFalse(self.controller._upstream_recovery_pending)
        self.assertIn("Recovery resumed", self.controller.status)
        self.assert_no_broker_mutations()

    def test_explicit_resume_failed_fresh_reads_leave_original_hold_saved(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for name in ("managed_accounts", "qualify_stock", "recent_executions", "position_size", "open_app_orders"):
            with self.subTest(name=name):
                before = self.storage.get_cycle(self.cycle.id).to_dict()
                method = getattr(self.adapter, name)
                original = method.side_effect
                method.side_effect = TimeoutError("Interrupted broker read")
                try:
                    self.controller._resume_recovery_monitoring()
                    self.assertEqual(self.storage.get_cycle(self.cycle.id).to_dict(), before)
                    self.assertTrue(self.controller._upstream_recovery_pending)
                    self.assert_no_broker_mutations()
                finally:
                    method.side_effect = original

    def test_conflicting_structured_account_cannot_fall_back_to_matching_legacy_fill(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.store_execution(raw={
            "account": "ACCOUNT_B", "con_id": 111,
            "raw_args": [
                "Fill(contract=Stock(conId=111, symbol='AAA', currency='USD'), "
                "execution=Execution(execId='BUY-1', acctNumber='ACCOUNT_A', "
                f"orderRef={self.ref!r}, orderId=101, permId=201, side='BOT'))",
            ],
        })
        self.assert_restore_blocked()

    def test_matching_structured_identity_cannot_hide_conflicting_legacy_account(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.store_execution(raw={
            "account": "ACCOUNT_A", "con_id": 111,
            "raw_args": [
                "Fill(contract=Stock(conId=111, symbol='AAA', currency='USD'), "
                "execution=Execution(execId='BUY-1', acctNumber='ACCOUNT_B', "
                f"orderRef={self.ref!r}, orderId=101, permId=201, side='BOT'))",
            ],
        })
        self.assert_restore_blocked()

    def test_structured_identity_cannot_hide_conflicting_or_malformed_archived_fill(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        good = (
            "Fill(contract=Stock(conId=111, symbol='AAA', currency='USD'), "
            "execution=Execution(execId='BUY-1', acctNumber='ACCOUNT_A', "
            f"orderRef={self.ref!r}, orderId=101, permId=201, side='BOT'))"
        )
        for texts in ([good.replace("conId=111", "conId=222")],
                      [good, good.replace("conId=111", "conId=222")], ["Fill(broken"]):
            with self.subTest(texts=texts):
                self.store_execution(raw={"account": "ACCOUNT_A", "con_id": 111, "raw_args": texts})
                self.assert_restore_blocked()

    def test_conflicting_structured_contract_cannot_fall_back_to_matching_legacy_fill(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        self.store_execution(raw={
            "account": "ACCOUNT_A", "con_id": 222,
            "raw_args": [
                "Fill(contract=Stock(conId=111, symbol='AAA', currency='USD'), "
                "execution=Execution(execId='BUY-1', acctNumber='ACCOUNT_A', "
                f"orderRef={self.ref!r}, orderId=101, permId=201, side='BOT'))",
            ],
        })
        self.assert_restore_blocked()

    def test_conflicting_account_or_contract_aliases_cannot_release_hold(self):
        self.make_hold(Stage.WAIT_RISE_TRIGGER)
        for raw in (
            {"account": "ACCOUNT_A", "acctNumber": "ACCOUNT_B", "con_id": 111},
            {"account": "ACCOUNT_B", "acctNumber": "ACCOUNT_A", "con_id": 111},
            {"account": "ACCOUNT_A", "con_id": 111, "conId": 222},
            {"account": "ACCOUNT_A", "con_id": 222, "conId": 111},
        ):
            with self.subTest(raw=raw):
                self.store_execution(raw=raw)
                self.assert_restore_blocked()
