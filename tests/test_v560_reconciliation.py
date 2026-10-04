"""Reconciliation confirmations and quantity/order guidance use current facts."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import Stage, StopAction, recovery_cycle_signature
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc).timestamp()
CHECKED_AT = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


class ReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(ROOT)
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        self.controller = _ControllerStub()
        self.controller.request_stop = Mock()
        self.controller.app_owned_unsold_position = Mock(return_value={})
        self.window = self.gui.MainWindow(self.controller)
        self.window.recovery_status_label.setObjectName = Mock()
        self.clock = self.enterContext(patch.object(self.gui.time, "time", return_value=NOW))
        self.enterContext(patch.object(self.gui.QMessageBox, "Ok", 1, create=True))
        self.enterContext(patch.object(self.gui.QMessageBox, "Cancel", 2, create=True))
        self.question = self.enterContext(patch.object(self.gui.QMessageBox, "question", return_value=2))
        self.warning = self.enterContext(patch.object(self.gui.QMessageBox, "warning"))
        self.snapshot = self.make_snapshot()
        self.window.current_snapshot = self.snapshot

    @staticmethod
    def make_snapshot(*, position=100.0, **cycle_values):
        cycle = {
            "id": "cycle-one", "cycle_number": 1, "ticker": "TEST", "account": "TEST-ACCOUNT",
            "con_id": 123, "stage": Stage.WAIT_RISE_TRIGGER.value,
            "buy_filled_qty": 100, "buy_status": "Filled",
            "sell_filled_qty": 0, "protective_sell_filled_qty": 0,
            "updated_at": CHECKED_AT, "error_message": "",
        }
        cycle.update(cycle_values)
        return {
            "connected": True,
            "active_cycle": cycle,
            "broker_recovery": {
                "connected": True, "checked_at": CHECKED_AT,
                "cycle_id": cycle["id"], "local_cycle_signature": recovery_cycle_signature(cycle),
                "position_size": position, "open_app_orders": [],
            },
        }

    def panel_text(self, snapshot):
        self.window._update_recovery_panel(snapshot)
        return self.window.recovery_compare_table.item(7, 1).text()

    def test_both_sell_entry_points_use_same_quantity_warning_and_cancel_default(self):
        self.window._recovery_sell_market_clicked()
        recovery_call = self.question.call_args.args
        self.assertIn("100 app-bought unsold", recovery_call[2])
        self.assertIn("may realize a loss", recovery_call[2])
        self.assertEqual(recovery_call[3:], (3, 2))
        self.controller.request_stop.assert_not_called()

        dialog = self.gui.StopDialog(show_position_close_action=True, unsold_quantity=100)
        dialog._confirm_sell_market()
        self.assertEqual(self.question.call_args.args[1:], recovery_call[1:])
        self.assertIsNone(dialog.selected_action)

    def test_accepted_current_confirmation_sends_only_existing_stop_command(self):
        self.question.return_value = 1
        self.window._recovery_sell_market_clicked()
        self.controller.request_stop.assert_called_once_with(StopAction.SELL_APP_POSITION_MARKET)
        self.warning.assert_not_called()

    def test_zero_app_quantity_does_not_offer_sell_or_use_unrelated_broker_position(self):
        self.window.current_snapshot = self.make_snapshot(position=500, buy_filled_qty=0)
        self.window._recovery_sell_market_clicked()
        self.question.assert_not_called()
        self.controller.request_stop.assert_not_called()

    def test_stale_probe_blocks_before_confirmation(self):
        self.clock.return_value = NOW + 61
        self.window._recovery_sell_market_clicked()
        self.question.assert_not_called()
        self.controller.request_stop.assert_not_called()

    def test_probe_can_expire_while_modal_is_open(self):
        def confirm(*_args):
            self.clock.return_value = NOW + 61
            return 1

        self.question.side_effect = confirm
        self.window._recovery_sell_market_clicked()
        self.controller.request_stop.assert_not_called()
        self.assertIn("Refresh", self.warning.call_args.args[1])

    def test_disconnection_while_modal_is_open_blocks_sell(self):
        def confirm(*_args):
            self.snapshot["connected"] = False
            return 1

        self.question.side_effect = confirm
        self.window._recovery_sell_market_clicked()
        self.controller.request_stop.assert_not_called()

    def test_replaced_cycle_or_fill_requires_new_confirmation_even_with_fresh_probe(self):
        for changed in (
            {"id": "other-cycle"}, {"ticker": "OTHER"}, {"account": "OTHER-ACCOUNT"},
            {"con_id": 456}, {"stage": Stage.SELL_TRAIL_ACTIVE.value},
            {"buy_filled_qty": 120}, {"sell_filled_qty": 20},
            {"protective_sell_filled_qty": 20},
        ):
            with self.subTest(changed=changed):
                self.window.current_snapshot = self.make_snapshot()
                self.warning.reset_mock()

                def confirm(*_args, changed=changed):
                    self.window.current_snapshot = self.make_snapshot(**changed)
                    return 1

                self.question.side_effect = confirm
                self.window._recovery_sell_market_clicked()
                self.controller.request_stop.assert_not_called()
                self.assertEqual(self.warning.call_args.args[1], "Market SELL state changed")

    def test_latest_persisted_quantity_is_rechecked_after_confirmation(self):
        self.controller.app_owned_unsold_position.side_effect = [{"quantity": 100}, {"quantity": 80}]
        self.question.return_value = 1
        self.window._recovery_sell_market_clicked()
        self.controller.request_stop.assert_not_called()
        self.assertEqual(self.warning.call_args.args[1], "Market SELL state changed")

    def test_price_only_snapshot_update_does_not_invalidate_confirmation(self):
        def confirm(*_args):
            self.snapshot["active_cycle"]["last_price"] = 123.45
            self.snapshot["active_cycle"]["updated_at"] = "2026-10-04T10:00:01+00:00"
            return 1

        self.question.side_effect = confirm
        self.window._recovery_sell_market_clicked()
        self.controller.request_stop.assert_called_once_with(StopAction.SELL_APP_POSITION_MARKET)

    def test_full_app_position_shortfall_is_reported(self):
        for position in (0, 1, 99, 99.999999):
            with self.subTest(position=position):
                text = self.panel_text(self.make_snapshot(position=position))
                self.assertIn("broker position is lower", text)
                self.window.recovery_status_label.setObjectName.assert_called_with("PriceStatusBad")

    def test_equal_position_extra_holdings_and_rounding_tolerance_are_allowed(self):
        for position in (100, 150, 100 - 0.5e-9):
            with self.subTest(position=position):
                text = self.panel_text(self.make_snapshot(position=position))
                self.assertIn("No inconsistency", text)
                self.window.recovery_status_label.setObjectName.assert_called_with("PriceStatusGood")

    def test_partial_sell_is_not_counted_twice_in_position_comparison(self):
        snapshot = self.make_snapshot(position=80, sell_filled_qty=20, protective_sell_filled_qty=20)
        self.assertIn("No inconsistency", self.panel_text(snapshot))
        snapshot["broker_recovery"]["position_size"] = 79
        self.assertIn("broker position is lower", self.panel_text(snapshot))

    def test_unknown_position_is_not_reported_as_consistent(self):
        for position in (None, "bad", float("nan"), float("inf")):
            with self.subTest(position=position):
                self.assertIn("position is unavailable", self.panel_text(self.make_snapshot(position=position)))
                self.window.recovery_status_label.setObjectName.assert_called_with("PriceStatusWarning")

    def test_stale_position_is_not_used_for_either_consistency_or_shortfall(self):
        self.clock.return_value = NOW + 61
        for position in (100, 1):
            with self.subTest(position=position):
                self.assertIn("position is not current", self.panel_text(self.make_snapshot(position=position)))
                self.assertIn("not current", self.window.recovery_compare_table.item(5, 2).text())
                self.window.recovery_status_label.setObjectName.assert_called_with("PriceStatusWarning")
                self.assertFalse(self.window.recovery_sell_market_btn.isEnabled())

    def test_failed_position_probe_is_not_reported_as_consistent(self):
        snapshot = self.make_snapshot()
        snapshot["broker_recovery"]["position_error"] = "Position refresh failed"
        self.assertIn("position is not current", self.panel_text(snapshot))
        self.assertFalse(self.window.recovery_sell_market_btn.isEnabled())

    def test_missing_partial_protective_order_warns_and_preserves_cancel_action(self):
        for status in ("Submitted", "PreSubmitted", "PendingCancel", "PendingSubmit", ""):
            with self.subTest(status=status):
                snapshot = self.make_snapshot(
                    position=80, protective_sell_filled_qty=20,
                    protective_sell_order_ref="APP-PROTECTIVE", protective_sell_status=status,
                )
                self.assertIn("protective SELL order", self.panel_text(snapshot))
                self.window.recovery_status_label.setObjectName.assert_called_with("PriceStatusBad")
                self.assertTrue(self.window.recovery_cancel_app_order_btn.isEnabled())

    def test_partial_terminal_protective_orders_are_not_treated_as_working(self):
        for status in ("Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected", "cancelled"):
            with self.subTest(status=status):
                snapshot = self.make_snapshot(
                    position=80, protective_sell_filled_qty=20,
                    protective_sell_order_ref="APP-PROTECTIVE", protective_sell_status=status,
                )
                self.assertIn("No inconsistency", self.panel_text(snapshot))
                self.assertFalse(self.window.recovery_cancel_app_order_btn.isEnabled())

    def test_final_sell_partial_remainder_remains_cancellable_until_terminal(self):
        for status, expected in (("Submitted", True), ("PendingCancel", True), ("Cancelled", False)):
            with self.subTest(status=status):
                self.panel_text(self.make_snapshot(
                    position=80, stage=Stage.SELL_TRAIL_ACTIVE.value,
                    sell_filled_qty=20, sell_order_ref="APP-SELL", sell_status=status,
                ))
                self.assertEqual(self.window.recovery_cancel_app_order_btn.isEnabled(), expected)


if __name__ == "__main__":
    unittest.main()
