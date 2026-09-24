"""Deferred table refitting contracts, runnable with unittest or pytest.

Qt doubles exercise scheduling and signal wiring; native geometry is covered
by the separate real-PySide6 smoke script.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.qt_stubs import EventStub, SignalStub, imported_gui_with_stubs


class ContentFitTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.pending = []
        self.fit = Mock()
        pending = self.pending

        class TimerProbe:
            def __init__(self, owner):
                self.owner = owner
                self.timeout = SignalStub()
                self.single_shot = False

            def setSingleShot(self, value):
                self.single_shot = value

            def start(self, delay):
                self.stop()
                pending.append((delay, self))

            def stop(self):
                pending[:] = [(delay, timer) for delay, timer in pending if timer is not self]

        for target, name, value in (
            (self.gui, "QTimer", TimerProbe),
            (self.gui, "_fit_table_height_to_all_rows", self.fit),
            (self.gui, "QEvent", SimpleNamespace(FontChange=101, ApplicationFontChange=102, StyleChange=103)),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

        class TableProbe(self.gui.ContentFitTable):
            def __init__(self):
                self.h_header = SimpleNamespace(sectionResized=SignalStub())
                self.v_header = SimpleNamespace(sectionResized=SignalStub())
                self.table_model = SimpleNamespace(dataChanged=SignalStub(), rowsInserted=SignalStub(), rowsRemoved=SignalStub())
                super().__init__(8, 4, min_height=130, max_height=230)

            def horizontalHeader(self):
                return self.h_header

            def verticalHeader(self):
                return self.v_header

            def model(self):
                return self.table_model

        self.table = TableProbe()

    def flush_one(self):
        delay, timer = self.pending.pop(0)
        self.assertEqual(delay, 0)
        timer.timeout.emit()

    def test_headers_and_content_changes_coalesce_until_the_deferred_fit(self):
        self.assertIs(self.table._fit_timer.owner, self.table)
        self.assertTrue(self.table._fit_timer.single_shot)
        self.assertEqual(len(self.table._fit_timer.timeout.callbacks), 1)
        signals = (
            self.table.h_header.sectionResized, self.table.v_header.sectionResized,
            self.table.table_model.dataChanged, self.table.table_model.rowsInserted, self.table.table_model.rowsRemoved,
        )
        for signal in signals:
            self.assertEqual(len(signal.callbacks), 1)
            signal.emit(0, 1, 2)
        self.assertEqual(len(self.pending), 1)
        self.fit.assert_not_called()
        self.flush_one()
        self.fit.assert_called_once_with(self.table, min_height=130, max_height=230)
        self.assertFalse(self.table._fit_pending)
        self.table.table_model.dataChanged.emit()
        self.assertEqual(len(self.pending), 1)

    def test_direct_fit_cancels_the_queued_owned_timer(self):
        self.table._schedule_fit()
        self.assertEqual(len(self.pending), 1)
        self.table.fit_rows()
        self.assertFalse(self.pending)
        self.assertFalse(self.table._fit_pending)
        self.fit.assert_called_once_with(self.table, min_height=130, max_height=230)
        self.table._schedule_fit()
        self.assertEqual(len(self.pending), 1)

    def test_width_font_and_style_changes_request_a_refit(self):
        self.table.resizeEvent(EventStub())
        self.assertEqual(len(self.pending), 1)
        self.flush_one()
        for kind in (101, 102, 103):
            with self.subTest(event_type=kind):
                self.table.changeEvent(EventStub(kind))
                self.assertEqual(len(self.pending), 1)
                self.flush_one()
        self.table.changeEvent(EventStub(999))
        self.assertFalse(self.pending)
        self.assertEqual(self.fit.call_count, 4)

    def test_fit_signals_do_not_queue_an_endless_refit(self):
        self.fit.side_effect = lambda *_args, **_kwargs: self.table.v_header.sectionResized.emit(0, 20, 40)
        self.table._schedule_fit()
        self.flush_one()
        self.assertFalse(self.pending)
        self.assertFalse(self.table._fit_pending)

    def test_failed_fit_releases_guard_for_the_next_change(self):
        self.fit.side_effect = RuntimeError("geometry unavailable")
        with self.assertRaisesRegex(RuntimeError, "geometry unavailable"):
            self.table.fit_rows()
        self.assertFalse(self.table._fit_pending)
        self.table._schedule_fit()
        self.assertEqual(len(self.pending), 1)

    def test_early_resize_before_initialization_does_not_queue_a_fit(self):
        table = object.__new__(self.gui.ContentFitTable)
        table._schedule_fit()
        self.assertFalse(self.pending)


if __name__ == "__main__":
    unittest.main()
