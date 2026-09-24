"""Explicit historical recovery targeting with Qt doubles and no broker access."""
from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models import Stage
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub


class HistoricalRecoveryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.controller = _ControllerStub()
        self.controller.mark_historical_cycle_manually_handled = Mock()
        self.controller.mark_recovery_manually_handled = Mock()
        self.controller.start_strategy = Mock()
        self.window = self.gui.MainWindow(self.controller)
        self.historical = {
            "id": "historical-23", "cycle_number": 23, "ticker": "TEST",
            "stage": Stage.CYCLE_COMPLETE.value,
            "buy_filled_qty": 28, "sell_filled_qty": 28,
            "buy_status": "CancelRequested", "buy_order_ref": "old-buy",
            "sell_status": "Filled", "sell_order_ref": "old-sell",
        }
        self.active = {"id": "active-39", "cycle_number": 39, "stage": Stage.WAIT_RISE_TRIGGER.value, "buy_filled_qty": 42}
        self.window.current_snapshot = {
            "active_cycle": deepcopy(self.active),
            "historical_unresolved_cycles": [deepcopy(self.historical)],
            "startup_resume_required": True,
        }
        self.messages = SimpleNamespace(Yes=1, No=2, question=Mock(return_value=2), warning=Mock())
        self.patch_messages = patch.object(self.gui, "QMessageBox", self.messages)
        self.patch_messages.start()
        self.addCleanup(self.patch_messages.stop)

    def assert_no_mutation(self):
        self.controller.mark_historical_cycle_manually_handled.assert_not_called()
        self.controller.mark_recovery_manually_handled.assert_not_called()
        self.controller.start_strategy.assert_not_called()
        self.assertEqual(self.window.current_snapshot.get("active_cycle"), self.active)

    def test_single_historical_cycle_requires_default_no_confirmation(self):
        self.window._recovery_historical_clicked()
        args = self.messages.question.call_args.args
        self.assertEqual(args[-1], self.messages.No)
        for expected in ("#23", "historical-23", "TEST", "BUY 28", "SELL 28", "CancelRequested", "old-buy", "incomplete", "independently verified", "NOT cancelled", "NOT sold", "active cycle is preserved"):
            self.assertIn(expected, args[2])
        self.assert_no_mutation()

    def test_confirmation_targets_historical_id_and_preserves_active_cycle(self):
        self.messages.question.return_value = self.messages.Yes
        before = deepcopy(self.window.current_snapshot)
        self.window._recovery_historical_clicked()
        call = self.controller.mark_historical_cycle_manually_handled.call_args
        cycle_id, note = call.args
        expected_cycle = call.kwargs["expected_cycle"]
        self.assertEqual(cycle_id, "historical-23")
        self.assertIn("independent IBKR verification", note)
        self.assertEqual(expected_cycle, self.historical)
        self.assertIsNot(expected_cycle, self.window.current_snapshot["historical_unresolved_cycles"][0])
        self.assertEqual(self.window.current_snapshot, before)
        self.controller.mark_recovery_manually_handled.assert_not_called()
        self.controller.start_strategy.assert_not_called()

    def test_multiple_cycles_require_explicit_selection_by_id(self):
        second = dict(self.historical, id="historical-24", cycle_number=24)
        self.window.current_snapshot["historical_unresolved_cycles"].append(second)
        self.messages.question.return_value = self.messages.Yes
        with patch.object(self.window, "_select_historical_recovery_cycle", return_value="historical-24") as select:
            self.window._recovery_historical_clicked()
        self.assertEqual([row["id"] for row in select.call_args.args[0]], ["historical-23", "historical-24"])
        self.assertEqual(self.controller.mark_historical_cycle_manually_handled.call_args.args[0], "historical-24")

    def test_cancelled_or_invalid_selection_does_nothing(self):
        self.window.current_snapshot["historical_unresolved_cycles"].append(dict(self.historical, id="historical-24"))
        for selected in ("", "unknown", "active-39"):
            with self.subTest(selected=selected), patch.object(self.window, "_select_historical_recovery_cycle", return_value=selected):
                self.window._recovery_historical_clicked()
        self.messages.question.assert_not_called()
        self.assert_no_mutation()

    def test_candidate_filter_rejects_active_nonterminal_and_missing_payloads(self):
        for rows in (None, {}, [], [None], [{}], [dict(self.historical, id="")], [dict(self.historical, id="active-39")], [dict(self.historical, stage=Stage.MANUAL_REVIEW.value)]):
            with self.subTest(rows=rows):
                self.window.current_snapshot["historical_unresolved_cycles"] = rows
                self.window._recovery_historical_clicked()
        self.messages.question.assert_not_called()
        self.assert_no_mutation()

    def test_changed_or_removed_payload_after_confirmation_blocks_action(self):
        for change in ("remove", "quantity", "active"):
            with self.subTest(change=change):
                self.setUp_snapshot()

                def confirm(*_args):
                    if change == "remove":
                        self.window.current_snapshot["historical_unresolved_cycles"] = []
                    elif change == "quantity":
                        self.window.current_snapshot["historical_unresolved_cycles"][0]["buy_filled_qty"] = 56
                    else:
                        self.window.current_snapshot["active_cycle"] = deepcopy(self.historical)
                    return self.messages.Yes

                self.messages.question.side_effect = confirm
                self.window._recovery_historical_clicked()
                self.controller.mark_historical_cycle_manually_handled.assert_not_called()
        self.assertEqual(self.messages.warning.call_count, 3)

    def setUp_snapshot(self):
        self.window.current_snapshot = {
            "active_cycle": deepcopy(self.active),
            "historical_unresolved_cycles": [deepcopy(self.historical)],
            "startup_resume_required": True,
        }

    def test_lock_storage_fault_and_watchdog_block_handler_and_button(self):
        for blocker in ("manual", "storage_fault", "watchdog_override"):
            with self.subTest(blocker=blocker):
                self.setUp_snapshot()
                self.window._manual_input_lock_enabled = blocker == "manual"
                if blocker != "manual":
                    self.window.current_snapshot[blocker] = {"active": True}
                self.window._update_historical_recovery_controls(self.window.current_snapshot)
                self.assertFalse(self.window.recovery_historical_btn.isEnabled())
                self.window._recovery_historical_clicked()
        self.messages.question.assert_not_called()
        self.assert_no_mutation()

    def test_lock_changed_during_confirmation_prevents_queued_command(self):
        def confirm(*_args):
            self.window._manual_input_lock_enabled = True
            return self.messages.Yes

        self.messages.question.side_effect = confirm
        self.window._recovery_historical_clicked()
        self.messages.warning.assert_called_once()
        self.assert_no_mutation()

    def test_failed_historical_database_refresh_blocks_cached_target(self):
        self.window.current_snapshot["database_snapshot"] = {
            "errors": {"historical_unresolved_cycles": "database read failed"},
        }
        self.window._update_historical_recovery_controls(self.window.current_snapshot)
        self.assertFalse(self.window.recovery_historical_btn.isEnabled())
        self.window._recovery_historical_clicked()
        self.messages.question.assert_not_called()
        self.assert_no_mutation()

    def test_startup_pause_does_not_hide_explicit_historical_action(self):
        self.window._update_historical_recovery_controls(self.window.current_snapshot)
        self.assertTrue(self.window.recovery_historical_btn.isEnabled())
        self.assertTrue(self.window.recovery_historical_btn.isVisible())
        self.assertIn("#23 TEST", self.window.recovery_historical_label.text())
        self.window.current_snapshot["historical_unresolved_cycles"] = []
        self.window._update_historical_recovery_controls(self.window.current_snapshot)
        self.assertFalse(self.window.recovery_historical_btn.isEnabled())
        self.assertFalse(self.window.recovery_historical_btn.isVisible())

    def test_selection_dialog_defaults_to_cancel_and_has_no_default_target(self):
        with patch.object(self.gui, "QDialog") as dialog, patch.object(self.gui, "QDialogButtonBox") as buttons:
            dialog.Accepted = 1
            dialog.return_value.exec.return_value = 0
            self.assertEqual(self.window._select_historical_recovery_cycle([self.historical]), "")
            buttons.return_value.button.assert_called_once_with(buttons.Cancel)
            buttons.return_value.button.return_value.setDefault.assert_called_once_with(True)
            dialog.return_value.exec.return_value = 1
            self.assertEqual(self.window._select_historical_recovery_cycle([self.historical]), "")


if __name__ == "__main__":
    unittest.main()
