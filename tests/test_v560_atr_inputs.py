"""ATR input ownership and protective-field locks with deterministic Qt doubles."""

from __future__ import annotations

import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import Stage
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub


class AtrInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        # Shared Dummy is deliberately falsey; real Qt widgets are truthy.
        # The lock helper checks widget existence before reading check state.
        class CheckBox(self.gui.QCheckBox):
            def __bool__(self):
                return True

        checkboxes = patch.object(self.gui, "QCheckBox", CheckBox)
        checkboxes.start()
        self.addCleanup(checkboxes.stop)
        self.controller = _ControllerStub()
        self.controller.strategy.atr_adapt_protective_sell_enabled = True
        self.controller.save_draft_settings = Mock()
        self.window = self.gui.MainWindow(self.controller)
        self.window._on_snapshot(self.snapshot())
        self.controller.save_draft_settings.reset_mock()

    def snapshot(self, *, stage=None, master=True, adapt_profit=True, adapt_protective=True):
        strategy = asdict(self.controller.strategy)
        strategy.update(
            atr_adaptive_enabled=master,
            atr_adapt_minimum_profit_enabled=adapt_profit,
            atr_adapt_protective_sell_enabled=adapt_protective,
        )
        return {
            "connected": False,
            "connection": asdict(self.controller.connection),
            "strategy": strategy,
            "active_cycle": {"id": "cycle-1", "ticker": "AAPL", "stage": stage} if stage else None,
            "price_snapshot": {
                "atr_ready": True,
                "atr_pct": 1.0,
                "atr_adaptive_percentages": {
                    "initial_drop_pct": 1.11,
                    "buy_rebound_trail_pct": 2.22,
                    "rise_trigger_pct": 9.99,
                    "sell_trailing_stop_pct": 4.44,
                    "protective_sell_trailing_stop_pct": 8.88,
                    "atr_adapt_minimum_profit_enabled": adapt_profit,
                    "atr_adapt_protective_sell_enabled": adapt_protective,
                },
            },
        }

    def saved_strategy(self):
        self.window._autosave_settings()
        return self.controller.save_draft_settings.call_args.args[1]

    def test_queued_snapshot_preserves_manual_profit_and_autosave_during_stage_three(self):
        window = self.window
        window.atr_min_profit_adaptive_check.setChecked(False)
        window.rise_trigger_spin.setValue(3.21)
        window._schedule_settings_autosave()
        window._on_snapshot(self.snapshot(stage=Stage.WAIT_RISE_TRIGGER.value))

        saved = self.saved_strategy()
        self.assertFalse(saved.atr_adapt_minimum_profit_enabled)
        self.assertEqual(saved.rise_trigger_pct, 3.21)
        self.assertTrue(window.rise_trigger_spin.isEnabled())
        self.assertFalse(window.protective_sell_trail_spin.isEnabled())
        self.assertEqual(saved.initial_drop_pct, 1.11)

    def test_queued_snapshot_preserves_manual_protective_percentage_and_autosave(self):
        window = self.window
        window.atr_protective_sell_adaptive_check.setChecked(False)
        window.protective_sell_trail_spin.setValue(3.21)
        window._schedule_settings_autosave()
        window._on_snapshot(self.snapshot(stage=Stage.WAIT_INITIAL_DROP.value))

        saved = self.saved_strategy()
        self.assertFalse(saved.atr_adapt_protective_sell_enabled)
        self.assertEqual(saved.protective_sell_trailing_stop_pct, 3.21)
        self.assertTrue(window.protective_sell_trail_spin.isEnabled())
        self.assertEqual(saved.rise_trigger_pct, 9.99)

    def test_queued_snapshot_cannot_overwrite_any_manual_percentage_after_master_off(self):
        window = self.window
        window.atr_adaptive_check.setChecked(False)
        fields = {
            "initial_drop_spin": ("initial_drop_pct", 1.23),
            "buy_rebound_spin": ("buy_rebound_trail_pct", 2.34),
            "rise_trigger_spin": ("rise_trigger_pct", 3.45),
            "sell_trail_spin": ("sell_trailing_stop_pct", 4.56),
            "protective_sell_trail_spin": ("protective_sell_trailing_stop_pct", 5.67),
        }
        for name, (_, value) in fields.items():
            getattr(window, name).setValue(value)
        window._schedule_settings_autosave()
        window._on_snapshot(self.snapshot())

        saved = self.saved_strategy()
        self.assertFalse(saved.atr_adaptive_enabled)
        for name, (key, expected) in fields.items():
            with self.subTest(field=key):
                self.assertEqual(getattr(saved, key), expected)
                self.assertTrue(getattr(window, name).isEnabled())
        self.assertIn("ATR adaptive OFF", window.atr_status_label.text())

    def test_startup_hydrates_persisted_manual_mode_before_adaptive_snapshot(self):
        controller = _ControllerStub()
        controller.strategy.atr_adaptive_enabled = False
        controller.strategy.rise_trigger_pct = 3.21
        controller.strategy.protective_sell_trailing_stop_pct = 4.56
        window = self.gui.MainWindow(controller)
        snapshot = self.snapshot(master=False)
        snapshot["strategy"].update(rise_trigger_pct=3.21, protective_sell_trailing_stop_pct=4.56)
        window._on_snapshot(snapshot)

        self.assertTrue(window._inputs_loaded_from_snapshot)
        self.assertFalse(window.atr_adaptive_check.isChecked())
        self.assertEqual(window.rise_trigger_spin.value(), 3.21)
        self.assertEqual(window.protective_sell_trail_spin.value(), 4.56)
        self.assertIn("ATR adaptive OFF", window.atr_status_label.text())

    def test_startup_hydrates_individual_manual_modes_without_overwriting_values(self):
        controller = _ControllerStub()
        controller.strategy.atr_adapt_minimum_profit_enabled = False
        controller.strategy.atr_adapt_protective_sell_enabled = False
        controller.strategy.rise_trigger_pct = 3.21
        controller.strategy.protective_sell_trailing_stop_pct = 4.56
        window = self.gui.MainWindow(controller)
        snapshot = self.snapshot(adapt_profit=False, adapt_protective=False)
        snapshot["strategy"].update(rise_trigger_pct=3.21, protective_sell_trailing_stop_pct=4.56)
        window._on_snapshot(snapshot)

        self.assertTrue(window.atr_adaptive_check.isChecked())
        self.assertFalse(window.atr_min_profit_adaptive_check.isChecked())
        self.assertFalse(window.atr_protective_sell_adaptive_check.isChecked())
        self.assertEqual(window.rise_trigger_spin.value(), 3.21)
        self.assertEqual(window.protective_sell_trail_spin.value(), 4.56)
        self.assertEqual(window.initial_drop_spin.value(), 1.11)

    def test_older_master_flag_does_not_disable_newly_enabled_atr_controls(self):
        window = self.window
        window.atr_adaptive_check.setChecked(True)
        window.atr_min_profit_adaptive_check.setChecked(True)
        window._on_snapshot(self.snapshot(master=False))

        self.assertTrue(window.atr_adaptive_check.isChecked())
        self.assertFalse(window.initial_drop_spin.isEnabled())
        self.assertFalse(window.rise_trigger_spin.isEnabled())
        self.assertIn("ATR adaptive ON", window.atr_status_label.text())

    def test_enabled_adaptive_values_update_without_scheduling_autosave(self):
        window = self.window
        window._autosave_timer.start = Mock()
        # The shared Qt doubles do not emit on setValue; emulate that signal
        # explicitly to test the real snapshot/autosave suppression path.
        for name in (
            "initial_drop_spin", "buy_rebound_spin", "rise_trigger_spin",
            "sell_trail_spin", "protective_sell_trail_spin",
        ):
            widget = getattr(window, name)
            original = widget.setValue

            def set_and_emit(value, original=original, widget=widget):
                original(value)
                widget.valueChanged.emit(value)

            widget.setValue = set_and_emit
        window._apply_atr_adaptive_snapshot_to_inputs(self.snapshot())

        self.assertEqual(window.initial_drop_spin.value(), 1.11)
        self.assertEqual(window.buy_rebound_spin.value(), 2.22)
        self.assertEqual(window.rise_trigger_spin.value(), 9.99)
        self.assertEqual(window.sell_trail_spin.value(), 4.44)
        self.assertEqual(window.protective_sell_trail_spin.value(), 8.88)
        self.assertFalse(window._applying_snapshot_to_inputs)
        window._autosave_timer.start.assert_not_called()
        self.controller.save_draft_settings.assert_not_called()

    def test_protective_manual_field_is_editable_in_safe_terminal_and_entry_stages(self):
        window = self.window
        for stage in (
            None, Stage.IDLE.value, Stage.WAIT_INITIAL_DROP.value,
            Stage.BUY_TRAIL_ACTIVE.value, Stage.CYCLE_COMPLETE.value,
            Stage.STOPPED.value, Stage.ERROR.value,
        ):
            for master, adapt_protective in ((False, True), (True, False)):
                with self.subTest(stage=stage, master=master, adapt_protective=adapt_protective):
                    window.current_snapshot = {"active_cycle": {"stage": stage}}
                    window.atr_adaptive_check.setChecked(master)
                    window.atr_protective_sell_adaptive_check.setChecked(adapt_protective)
                    window._update_input_locks(stage)
                    self.assertTrue(window.protective_sell_trail_spin.isEnabled())

    def test_protective_active_position_and_review_locks_are_preserved(self):
        window = self.window
        for stage in (Stage.WAIT_RISE_TRIGGER.value, Stage.SELL_TRAIL_ACTIVE.value, Stage.MANUAL_REVIEW.value):
            for master in (False, True):
                with self.subTest(stage=stage, master=master):
                    window.current_snapshot = {"active_cycle": {"stage": stage}}
                    window.atr_adaptive_check.setChecked(master)
                    window.atr_protective_sell_adaptive_check.setChecked(False)
                    window._update_input_locks(stage)
                    self.assertFalse(window.protective_sell_trail_spin.isEnabled())

    def test_protective_atr_and_operator_locks_still_override_terminal_editability(self):
        window = self.window
        for stage in (None, Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value, Stage.ERROR.value):
            with self.subTest(stage=stage):
                window.current_snapshot = {"active_cycle": {"stage": stage}}
                window.atr_adaptive_check.setChecked(True)
                window.atr_protective_sell_adaptive_check.setChecked(True)
                window._update_input_locks(stage)
                self.assertFalse(window.protective_sell_trail_spin.isEnabled())

                window.atr_adaptive_check.setChecked(False)
                window._manual_input_lock_enabled = True
                window._manual_input_lock_widget_ids = {id(window.protective_sell_trail_spin)}
                window._update_input_locks(stage)
                self.assertFalse(window.protective_sell_trail_spin.isEnabled())
                window._manual_input_lock_enabled = False


if __name__ == "__main__":
    unittest.main()
