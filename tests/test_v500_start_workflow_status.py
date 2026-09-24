"""Started holding cycles must not appear to need a second Start action."""

from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.models import Stage
from tests.support.qt_stubs import imported_gui_with_stubs


class StartWorkflowStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def snapshot(self, stage=Stage.WAIT_RISE_TRIGGER, message=""):
        return {
            "connected": True,
            "status": "Connected",
            "broker_connectivity": {
                "local_connected": True,
                "upstream_connected": True,
            },
            "startup_resume_required": False,
            "active_cycle": {
                "id": "restored-cycle",
                "ticker": "TEST",
                "stage": stage.value,
                "last_price": 100.0,
                "error_message": message,
            },
            "price_snapshot": {"price": 100.0},
        }

    def render(self, snapshot, *, locked=False):
        window = SimpleNamespace(
            current_snapshot=snapshot,
            command_steps={
                key: Mock() for key in ("connect", "ticker", "confirm", "start", "stop")
            },
            _manual_input_lock_enabled=locked,
        )
        self.gui.MainWindow._update_command_bar_states(window, snapshot)
        return window.command_steps

    def test_stage_three_old_sell_rth_error_does_not_reclassify_start(self):
        snapshot = self.snapshot(
            message="RTH guard blocked SELL market order submission: Outside contract liquidHours."
        )
        snapshot["active_cycle"]["close_position_market_requested"] = True
        before = deepcopy(snapshot)
        cards = self.render(snapshot)
        cards["start"].set_state.assert_called_once_with("Done", False, "Strategy running")
        self.assertEqual(snapshot, before)

    def test_stage_three_prior_buy_session_block_does_not_reclassify_start(self):
        cards = self.render(self.snapshot(
            message="Session timing guard blocked BUY: within first 30 minutes after session open."
        ))
        cards["start"].set_state.assert_called_once_with("Done", False, "Strategy running")

    def test_working_native_order_does_not_appear_to_need_start(self):
        for stage in (Stage.BUY_TRAIL_ACTIVE, Stage.SELL_TRAIL_ACTIVE):
            with self.subTest(stage=stage):
                cards = self.render(self.snapshot(stage, "Earlier trade guard blocked submission."))
                cards["start"].set_state.assert_called_once_with("Done", False, "Strategy running")

    def test_stage_one_buy_guard_classification_remains_blocked(self):
        cards = self.render(self.snapshot(
            Stage.WAIT_INITIAL_DROP, "BUY blocked: not enough ATR data is available yet."
        ))
        cards["start"].set_state.assert_called_once_with(
            "Blocked", False, "Trade guard is blocking BUY"
        )

    def test_startup_resume_still_requires_explicit_enabled_start(self):
        snapshot = self.snapshot(message="Earlier RTH guard blocked SELL submission.")
        snapshot["startup_resume_required"] = True
        cards = self.render(snapshot)
        cards["start"].set_state.assert_called_once_with(
            "Ready", True, "Click to resume stored cycle"
        )

    def test_disconnected_and_pending_reconciliation_still_block_start(self):
        for overrides in ({"connected": False}, {"upstream_recovery_pending": True}):
            with self.subTest(overrides=overrides):
                snapshot = self.snapshot()
                snapshot.update(overrides)
                cards = self.render(snapshot)
                state, enabled, _detail = cards["start"].set_state.call_args.args
                self.assertEqual(state, "Blocked")
                self.assertFalse(enabled)

    def test_recovery_error_stages_still_require_resolution(self):
        for stage in (Stage.ERROR, Stage.MANUAL_REVIEW):
            with self.subTest(stage=stage):
                cards = self.render(self.snapshot(stage, "Manual review required."))
                cards["start"].set_state.assert_called_once_with(
                    "Error", False, "Resolve recovery state"
                )

    def test_manual_input_lock_still_disables_all_workflow_actions(self):
        cards = self.render(self.snapshot(), locked=True)
        for card in cards.values():
            state, enabled, _detail = card.set_state.call_args.args
            self.assertEqual(state, "Locked")
            self.assertFalse(enabled)


if __name__ == "__main__":
    unittest.main()
