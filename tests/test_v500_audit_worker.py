"""Audit-reader lifecycle and GUI batching contracts using deterministic Qt doubles.

These tests do not claim native Qt rendering or event-loop timing coverage. One
explicitly joined Python thread checks the plain-data worker boundary; all GUI
lifecycle tests defer worker execution and drive timer callbacks themselves.
"""
from __future__ import annotations

import queue
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.support.qt_stubs import Dummy, SignalStub, imported_gui_with_stubs

_REAL_THREAD = threading.Thread


class TimerProbe(Dummy):
    pending = []

    def __init__(self, *args):
        super().__init__(*args)
        self.active = False
        self.interval = None
        self.timeout = SignalStub()

    @classmethod
    def singleShot(cls, delay, callback):
        cls.pending.append((delay, callback))

    def setInterval(self, interval):
        self.interval = interval

    def start(self):
        self.active = True

    def stop(self):
        self.active = False


class HeaderProbe(Dummy):
    def __init__(self):
        super().__init__()
        self.precisions = []
        self.modes = []
        self.sectionResized = SignalStub()

    def setResizeContentsPrecision(self, precision):
        self.precisions.append(precision)

    def setSectionResizeMode(self, *args):
        self.modes.append(args)


class TableProbe(Dummy):
    def __init__(self, rows=0, columns=0, *args):
        super().__init__(*args)
        self.setRowCount(rows)
        self.setColumnCount(columns)
        self.horizontal = HeaderProbe()
        self.vertical = HeaderProbe()
        self.view = Dummy()
        self.update_states = []
        self.resized_rows = []

    def horizontalHeader(self):
        return self.horizontal

    def verticalHeader(self):
        return self.vertical

    def viewport(self):
        return self.view

    def setUpdatesEnabled(self, enabled):
        self.update_states.append(enabled)

    def resizeRowToContents(self, row):
        self.resized_rows.append(row)


def cycle_row():
    return {"id": "cycle-test", "ticker": "TEST", "cycle_number": 1}


def decisions(count):
    return [{"created_at": f"2026-08-14T12:00:{index:02d}+00:00",
             "event_type": "AUDIT_EVENT", "stage_before": "BEFORE",
             "stage_after": "AFTER", "decision_result": "OBSERVED",
             "broker_order_id": index, "perm_id": 1000 + index,
             "message": f"Complete decision message {index}",
             "raw_json": '{"detail":"complete raw audit record"}'}
            for index in range(count)]


class AuditWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        TimerProbe.pending = []
        self.jobs = []
        jobs = self.jobs

        class DeferredThread:
            def __init__(self, *, target, args, name, daemon):
                self.target, self.args = target, args
                self.name, self.daemon = name, daemon
                self.starts = 0
                jobs.append(self)

            def start(self):
                self.starts += 1

            def run(self):
                self.target(*self.args)

        self.stack.enter_context(patch.object(self.gui.threading, "Thread", DeferredThread))
        self.stack.enter_context(patch.object(self.gui, "QTimer", TimerProbe))
        self.stack.enter_context(patch.object(self.gui, "QTableWidget", TableProbe))
        for name in ("showEvent", "done", "reject", "closeEvent"):
            self.stack.enter_context(patch.object(Dummy, name, lambda *_args: None, create=True))
        self.loader = Mock(return_value=([{"price": 101.5}], ["capture.zip"]))
        self.stack.enter_context(patch.object(self.gui.CycleAuditDialog, "_load_market_capture_rows", self.loader))
        self.autosize = self.stack.enter_context(patch.object(self.gui, "_auto_size_table_columns"))

    def dialog(self, count=3, *, loaded=False):
        details = {"decision_events": decisions(count), "orders": [], "events": [], "executions": []}
        if loaded:
            details.update(market_capture_rows=[], market_capture_files=[])
        dialog = self.gui.CycleAuditDialog(cycle_row(), details)
        self.addCleanup(dialog._close_audit_loading)
        return dialog

    def drain_callbacks(self):
        callbacks, TimerProbe.pending = TimerProbe.pending, []
        for _delay, callback in callbacks:
            callback()

    def finish(self, dialog):
        for _ in range(100):
            dialog._poll_audit_loading()
            if not dialog._audit_timer.active:
                return
        self.fail("Audit timer did not finish within bounded test callbacks")

    def test_show_defers_one_job_until_summary_can_paint(self):
        dialog = self.dialog()
        self.assertEqual(self.jobs, [])
        dialog.showEvent(None)
        dialog.showEvent(None)
        self.assertEqual(self.jobs, [])
        self.assertEqual([delay for delay, _ in TimerProbe.pending], [0, 0])
        self.drain_callbacks()
        dialog._start_audit_preload()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].starts, 1)
        self.assertTrue(self.jobs[0].daemon)
        self.assertEqual(dialog._audit_timer.interval, 16)
        self.loader.assert_not_called()

    def test_early_tabs_share_job_and_materialize_once_on_gui_poll(self):
        dialog = self.dialog()
        built = []
        for index in (dialog._timeline_tab_index, dialog._market_capture_tab_index):
            layout, _builder, status = dialog._lazy_tabs[index]
            dialog._lazy_tabs[index] = (layout, lambda selected=index: built.append(selected) or Dummy(), status)
        for _ in range(2):
            for index in (dialog._timeline_tab_index, dialog._market_capture_tab_index, dialog._decision_events_tab_index):
                dialog._materialize_tab(index)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(built, [])
        self.assertIsNone(dialog._decision_table)
        self.jobs[0].run()
        self.assertEqual(built, [])
        self.assertIsNone(dialog._decision_table)
        self.finish(dialog)
        self.assertCountEqual(built, [dialog._timeline_tab_index, dialog._market_capture_tab_index])
        self.assertIsNotNone(dialog._decision_table)
        self.assertEqual(dialog._waiting_audit_tabs, set())
        dialog._materialize_tab(dialog._timeline_tab_index)
        self.assertEqual(len(built), 2)
        self.loader.assert_called_once()

    def test_worker_receives_detached_nested_data_without_dialog_reference(self):
        dialog = self.dialog()
        dialog.row["nested"] = {"value": [1]}
        dialog._start_audit_preload()
        row, details, cancel, results, loader = self.jobs[0].args
        dialog.row["nested"]["value"].append(2)
        dialog.details["decision_events"][0]["message"] = "changed on GUI side"
        self.assertEqual(row["nested"]["value"], [1])
        self.assertEqual(details["decision_events"][0]["message"], "Complete decision message 0")
        self.assertNotIn("market_capture_rows", details)
        self.assertIs(cancel, dialog._audit_cancel)
        self.assertIs(results, dialog._audit_results)
        self.assertIs(loader, self.loader)
        self.assertIsNone(getattr(self.jobs[0].target, "__self__", None))
        self.assertTrue(all(arg is not dialog for arg in self.jobs[0].args))

    def test_plain_worker_can_run_in_joined_thread_without_widget_construction(self):
        main_thread = threading.get_ident()
        loader_threads = []
        results = queue.Queue()

        def capture_loader(row, details, *, should_cancel):
            self.assertFalse(should_cancel())
            loader_threads.append(threading.get_ident())
            return [{"price": 102}], ["completed.zip"]

        with patch.object(self.gui, "QTableWidgetItem", side_effect=AssertionError("Qt item built on worker")):
            worker = _REAL_THREAD(target=self.gui.CycleAuditDialog._prepare_audit_data,
                                  args=(cycle_row(), {"decision_events": decisions(1)}, threading.Event(), results, capture_loader))
            worker.start()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(loader_threads), 1)
        self.assertNotEqual(loader_threads[0], main_thread)
        first, second, third = results.get_nowait(), results.get_nowait(), results.get_nowait()
        self.assertEqual([first[0], second[0], third[0]], ["decisions", "captures", "finished"])
        self.assertEqual(len(first[1][0][0]), 8)
        self.assertTrue(all(isinstance(value, str) for value in first[1][0][0]))
        self.assertEqual(first[1][0][1], decisions(1)[0]["raw_json"])

    def test_pre_cancelled_worker_publishes_nothing_and_skips_capture(self):
        cancel, results = threading.Event(), queue.Queue()
        cancel.set()
        self.gui.CycleAuditDialog._prepare_audit_data(cycle_row(), {"decision_events": decisions(1)}, cancel, results, self.loader)
        self.loader.assert_not_called()
        self.assertTrue(results.empty())

    def test_cancel_during_capture_discards_late_payload(self):
        cancel, results = threading.Event(), queue.Queue()

        def cancelled_loader(_row, _details, *, should_cancel):
            cancel.set()
            self.assertTrue(should_cancel())
            return [{"price": 200}], ["late.zip"]

        self.gui.CycleAuditDialog._prepare_audit_data(cycle_row(), {"decision_events": []}, cancel, results, cancelled_loader)
        self.assertEqual(results.get_nowait(), ("decisions", []))
        self.assertTrue(results.empty())

    def test_known_capture_snapshot_does_not_read_archives_again(self):
        dialog = self.dialog(loaded=True)
        dialog._start_audit_preload()
        self.assertIsNone(self.jobs[0].args[-1])
        self.jobs[0].run()
        self.finish(dialog)
        self.loader.assert_not_called()
        self.assertEqual(dialog._decision_next_row, 3)

    def test_capture_error_keeps_decisions_available_and_reports_waiting_tabs(self):
        dialog = self.dialog()
        status = dialog._lazy_tabs[dialog._timeline_tab_index][2]
        self.loader.side_effect = OSError("archive unavailable")
        dialog._materialize_tab(dialog._timeline_tab_index)
        self.jobs[0].run()
        self.finish(dialog)
        self.assertIn("archive unavailable", status.text())
        self.assertEqual(dialog._decision_next_row, 3)
        self.assertIsNotNone(dialog._decision_table.item(2, 7))
        self.assertFalse(dialog._market_capture_loaded)
        dialog._materialize_tab(dialog._timeline_tab_index)
        self.assertEqual(len(self.jobs), 1)

    def test_thread_start_error_is_displayed_and_stops_polling(self):
        dialog = self.dialog()
        with patch.object(self.gui.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            dialog._start_audit_preload()
        self.finish(dialog)
        self.assertTrue(dialog._audit_finished)
        self.assertIn("thread unavailable", dialog._lazy_tabs[dialog._decision_events_tab_index][2].text())

    def test_thread_construction_error_is_displayed_and_stops_polling(self):
        dialog = self.dialog()
        with patch.object(self.gui.threading, "Thread", side_effect=RuntimeError("thread construction failed")):
            dialog._start_audit_preload()
        self.finish(dialog)
        self.assertTrue(dialog._audit_finished)
        self.assertIn("thread construction failed", dialog._lazy_tabs[dialog._decision_events_tab_index][2].text())

    def test_snapshot_copy_error_is_displayed_without_starting_worker(self):
        dialog = self.dialog()
        with patch.object(self.gui, "deepcopy", side_effect=ValueError("snapshot copy failed")):
            dialog._start_audit_preload()
        self.finish(dialog)
        self.assertEqual(self.jobs, [])
        self.assertTrue(dialog._audit_finished)
        self.assertIn("snapshot copy failed", dialog._lazy_tabs[dialog._decision_events_tab_index][2].text())

    def test_close_paths_cancel_without_join_and_ignore_queued_completions(self):
        for method, args in (("done", (0,)), ("reject", ()), ("closeEvent", (None,))):
            with self.subTest(method=method):
                dialog = self.dialog()
                dialog._start_audit_preload()
                dialog._audit_results.put(("captures", ([{"price": 1}], ["before.zip"])))
                getattr(dialog, method)(*args)
                self.assertTrue(dialog._audit_closed)
                self.assertTrue(dialog._audit_cancel.is_set())
                self.assertFalse(dialog._audit_timer.active)
                self.assertTrue(dialog._audit_results.empty())
                dialog._audit_results.put(("captures", ([{"price": 2}], ["late.zip"])))
                dialog._poll_audit_loading()
                dialog._materialize_tab(dialog._decision_events_tab_index)
                self.assertNotIn("market_capture_rows", dialog.details)
                self.assertIsNone(dialog._decision_table)

    def test_closed_dialog_ignores_deferred_show_and_tab_callbacks(self):
        dialog = self.dialog()
        dialog.showEvent(None)
        dialog._queue_materialize_tab(dialog._timeline_tab_index)
        dialog._close_audit_loading()
        self.drain_callbacks()
        self.assertEqual(self.jobs, [])

    def test_parent_destruction_before_deferred_start_prevents_widget_access(self):
        dialog = self.dialog()
        dialog.showEvent(None)
        dialog._queue_materialize_tab(dialog._timeline_tab_index)
        dialog.destroyed.emit()
        self.assertTrue(dialog._audit_cancel.is_set())
        self.drain_callbacks()
        self.assertEqual(self.jobs, [])
        self.assertFalse(dialog._audit_timer.active)

    def test_parent_destruction_ignores_already_queued_completion(self):
        dialog = self.dialog()
        dialog._start_audit_preload()
        self.jobs[0].run()
        dialog.destroyed.emit()
        dialog._poll_audit_loading()
        self.assertIsNone(dialog._decision_table)
        self.assertNotIn("market_capture_rows", dialog.details)

    def test_decision_batches_preserve_all_cells_and_raw_tooltips_without_repeated_autosize(self):
        dialog = self.dialog(count=81, loaded=True)
        dialog._start_audit_preload()
        self.jobs[0].run()
        with patch.object(self.gui.time, "perf_counter", return_value=0):
            dialog._poll_audit_loading()
            self.assertEqual(dialog._decision_next_row, 40)
            self.assertEqual(len(dialog._decision_table._table_items), 40 * 8)
            dialog._poll_audit_loading()
            self.assertEqual(dialog._decision_next_row, 80)
            self.finish(dialog)
        table = dialog._decision_table
        self.assertEqual(len(table._table_items), 81 * 8)
        self.assertEqual(table.item(80, 7).text(), "Complete decision message 80")
        self.assertEqual(table.item(80, 7).toolTip(), decisions(81)[80]["raw_json"])
        self.assertEqual(table.resized_rows, list(range(81)))
        self.assertEqual(table.horizontal.precisions, [100])
        self.autosize.assert_called_once_with(table, maximum=320, last_maximum=420)
        self.assertEqual(table.update_states[::2], [False] * (len(table.update_states) // 2))
        self.assertEqual(table.update_states[1::2], [True] * (len(table.update_states) // 2))

    def test_decision_batch_yields_when_time_budget_is_exhausted(self):
        dialog = self.dialog(count=81, loaded=True)
        dialog._start_audit_preload()
        self.jobs[0].run()
        with patch.object(self.gui.time, "perf_counter", side_effect=[1.0, 1.007]):
            dialog._poll_audit_loading()
        self.assertEqual(dialog._decision_next_row, 1)
        self.assertEqual(len(dialog._decision_table._table_items), 8)
        self.assertTrue(dialog._audit_timer.active)

    def test_row_height_refits_after_resize_in_bounded_batches(self):
        dialog = self.dialog(count=81, loaded=True)
        dialog._start_audit_preload()
        self.jobs[0].run()
        with patch.object(self.gui.time, "perf_counter", return_value=0):
            self.finish(dialog)
            table = dialog._decision_table
            table.resized_rows.clear()
            table.horizontal.sectionResized.emit(7, 320, 200)
            self.assertTrue(dialog._audit_timer.active)
            dialog._poll_audit_loading()
            self.assertEqual(table.resized_rows, list(range(40)))
            self.finish(dialog)
        self.assertEqual(table.resized_rows, list(range(81)))
        self.assertEqual(self.autosize.call_count, 1)

    def test_gui_resize_and_font_events_restart_row_fit(self):
        dialog = self.dialog(count=1, loaded=True)
        dialog._start_audit_preload()
        self.jobs[0].run()
        self.finish(dialog)
        events = SimpleNamespace(Resize=1, FontChange=2, StyleChange=3)
        with patch.object(self.gui, "QEvent", events):
            for event_type in (events.Resize, events.FontChange, events.StyleChange):
                with self.subTest(event_type=event_type):
                    event = SimpleNamespace(type=lambda: event_type)
                    dialog.eventFilter(dialog._decision_table.viewport(), event)
                    self.assertEqual(dialog._decision_resize_row, 0)
                    self.assertTrue(dialog._audit_timer.active)
                    self.finish(dialog)
            dialog.eventFilter(dialog._decision_table.viewport(), SimpleNamespace(type=lambda: 99))
            dialog.eventFilter(Dummy(), SimpleNamespace(type=lambda: events.Resize))
            self.assertFalse(dialog._audit_timer.active)

    def test_empty_decisions_keep_explanatory_row_and_finish(self):
        dialog = self.dialog(count=0, loaded=True)
        dialog._start_audit_preload()
        self.jobs[0].run()
        self.finish(dialog)
        self.assertEqual(dialog._decision_table.rowCount(), 1)
        self.assertIn("No structured decision events", dialog._decision_table.item(0, 0).text())
        self.assertFalse(dialog._decision_batch_pending())

    def test_table_updates_are_restored_if_item_insertion_raises(self):
        dialog = self.dialog(count=1, loaded=True)
        dialog._decision_rows = [(["value"] * 8, "raw")]
        dialog._build_decision_events_tab()
        table = dialog._decision_table
        with patch.object(table, "setItem", side_effect=RuntimeError("item rejected")):
            with self.assertRaisesRegex(RuntimeError, "item rejected"):
                dialog._populate_decision_batch()
        self.assertEqual(table.update_states, [False, True])

    def test_account_unknown_offline_is_na_but_connected_auto_and_known_account_are_preserved(self):
        for snapshot, expected in (({}, "N/A"),
                                   ({"connected": True, "broker_connectivity": {"local_connected": False}}, "N/A"),
                                   ({"connected": True}, "Auto (single managed account)"),
                                   ({"connected": False, "connection": {"account": "TEST_ACCOUNT"}}, "TEST_ACCOUNT")):
            with self.subTest(snapshot=snapshot):
                bar = self.gui.LiveStatusBar()
                bar.update_data(snapshot)
                self.assertEqual(bar.pills["Account"].value.text(), expected)


if __name__ == "__main__":
    unittest.main()
