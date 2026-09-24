"""Bounded market context and real stage changes in the read-only audit GUI."""
from __future__ import annotations

import copy
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.support.qt_stubs import Dummy, imported_gui_with_stubs


class AuditContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = {"id": "cycle-37", "cycle_number": 37, "ticker": "NBIS", "con_id": 88819736, "currency": "USD"}
        self.details = {"cycle": self.row, "decision_events": []}
        self.manifest = {
            "cycle_id": "cycle-37", "cycle_number": 37, "ticker": "NBIS",
            "event_type": "SELL_FILL", "order_ref": "IBKRBOT|NBIS|CYCLE-000037|cycle-37|SELL",
            "started_at_utc": "2026-08-14T19:55:05+00:00",
            "first_row_utc": "2026-08-14T19:40:05+00:00", "last_row_utc": "2026-08-14T20:10:05+00:00",
            "pre_window_seconds": 900, "post_window_seconds": 900,
        }
        self.event = {
            "cycle": dict(self.row), "event_type": "SELL_FILL",
            "order_ref": self.manifest["order_ref"], "event_time_utc": self.manifest["started_at_utc"],
        }

    def capture_row(self, time="2026-08-14T20:10:05+00:00", cycle=38, **changes):
        row = {
            "captured_at_utc": time, "monotonic_ts": 9000, "price": 276.9,
            "cycle_id": f"cycle-{cycle}", "cycle_number": cycle, "ticker": "NBIS",
            "contract": {"ticker": "NBIS", "con_id": 88819736, "currency": "USD"},
        }
        row.update(changes)
        return row

    def write_capture(self, rows, *, manifest=True, event=True, csv_only=False):
        path = self.root / "NBIS" / "cycle_37" / "capture.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if manifest:
                archive.writestr("manifest.json", json.dumps(self.manifest))
            if event:
                archive.writestr("event.json", json.dumps(self.event))
            if csv_only:
                text = io.StringIO()
                writer = csv.DictWriter(text, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
                archive.writestr("market_data.csv", text.getvalue())
            else:
                archive.writestr("market_data.jsonl", "\n".join(json.dumps(row) for row in rows))
        return path

    def load(self, **kwargs):
        with patch.object(self.gui, "debug_captures_dir", return_value=self.root):
            return self.gui.CycleAuditDialog._load_market_capture_rows(self.row, self.details, **kwargs)

    def test_post_sell_market_context_survives_auto_repeat_cycle_change(self):
        rows = [self.capture_row("2026-08-14T19:55:05+00:00", cycle=37), self.capture_row()]
        path = self.write_capture(rows)
        before = copy.deepcopy(self.details)
        loaded, files = self.load()
        self.assertEqual([row["cycle_number"] for row in loaded], [37, 38])
        self.assertEqual(loaded[-1]["captured_at_utc"], "2026-08-14T20:10:05+00:00")
        self.assertEqual(files, [str(path)])
        self.assertEqual(self.details, before)

    def test_pre_buy_context_also_survives_previous_cycle_identity(self):
        self.manifest["event_type"] = self.event["event_type"] = "BUY_FILL"
        self.write_capture([self.capture_row("2026-08-14T19:40:05+00:00", cycle=36)])
        self.assertEqual(len(self.load()[0]), 1)

    def test_only_rows_inside_both_declared_and_recorded_window_are_included(self):
        self.write_capture([
            self.capture_row("2026-08-14T19:40:04+00:00"),
            self.capture_row("2026-08-14T19:40:05+00:00"),
            self.capture_row(), self.capture_row("2026-08-14T20:10:06+00:00", cycle=37),
            self.capture_row("not-a-time"),
        ])
        self.assertEqual([row["captured_at_utc"] for row in self.load()[0]], ["2026-08-14T19:40:05+00:00", "2026-08-14T20:10:05+00:00"])

    def test_wrong_ticker_contract_and_currency_are_excluded(self):
        for changes in (
            {"ticker": "IREN"},
            {"contract": {"ticker": "IREN", "con_id": 88819736}},
            {"contract": {"ticker": "NBIS", "con_id": 42}},
            {"contract": {"ticker": "NBIS", "con_id": 88819736, "currency": "EUR"}},
        ):
            with self.subTest(changes=changes):
                self.write_capture([self.capture_row(**changes)])
                self.assertEqual(self.load(), ([], []))

    def test_missing_contract_identity_does_not_relax_cycle_filter(self):
        self.write_capture([self.capture_row(contract={})])
        self.assertEqual(self.load(), ([], []))

    def test_wrong_instrument_is_excluded_even_with_matching_cycle(self):
        self.write_capture([self.capture_row(cycle=37, contract={"con_id": 42})])
        self.assertEqual(self.load(), ([], []))

    def test_unrelated_archive_is_rejected_despite_matching_folder(self):
        for key, value in (("cycle_id", "cycle-38"), ("cycle_number", 38), ("ticker", "IREN")):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_capture([self.capture_row()])
                self.assertEqual(self.load(), ([], []))
                self.manifest[key] = original

    def test_incomplete_or_inconsistent_manifest_keeps_strict_cycle_filter(self):
        cases = (
            ("cycle_id", ""), ("cycle_number", ""), ("cycle_number", 37.9), ("ticker", ""),
            ("post_window_seconds", -1), ("pre_window_seconds", "invalid"),
            ("post_window_seconds", float("inf")), ("post_window_seconds", float("nan")),
            ("started_at_utc", "2026-08-14T21:00:00+00:00"),
            ("first_row_utc", "2026-08-14T21:00:00+00:00"),
            ("order_ref", "other-order"), ("event_type", "ATR_UPDATE"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_capture([self.capture_row(), self.capture_row(cycle=37)])
                self.assertEqual([row["cycle_number"] for row in self.load()[0]], [37])
                self.manifest[key] = original

    def test_unrelated_event_cannot_authorize_context(self):
        for key, value in (("id", "cycle-38"), ("ticker", "IREN"), ("con_id", 42)):
            with self.subTest(key=key):
                original = self.event["cycle"][key]
                self.event["cycle"][key] = value
                self.write_capture([self.capture_row()])
                self.assertEqual(self.load(), ([], []))
                self.event["cycle"][key] = original

    def test_legacy_without_manifest_or_event_keeps_matching_cycle_only(self):
        for manifest, event in ((False, False), (True, False)):
            with self.subTest(manifest=manifest):
                self.write_capture([self.capture_row(), self.capture_row(cycle=37)], manifest=manifest, event=event)
                self.assertEqual([row["cycle_number"] for row in self.load()[0]], [37])

    def test_streams_market_json_without_bulk_zip_read(self):
        self.write_capture([self.capture_row()])
        original_read = zipfile.ZipFile.read

        def read(archive, name, *args, **kwargs):
            self.assertNotIn(name, ("market_data.jsonl", "market_data.csv"))
            return original_read(archive, name, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "read", read):
            self.assertEqual(len(self.load()[0]), 1)

    def test_csv_capture_uses_flattened_contract_identity(self):
        row = self.capture_row()
        row.pop("contract")
        row.update({"contract.con_id": 88819736, "contract.ticker": "NBIS", "contract.currency": "USD"})
        self.write_capture([row], csv_only=True)
        self.assertEqual(len(self.load()[0]), 1)

    def test_cancellation_discards_partial_rows_and_releases_archive(self):
        path = self.write_capture([self.capture_row(), self.capture_row("2026-08-14T20:10:04+00:00")])
        calls = 0

        def should_cancel():
            nonlocal calls
            calls += 1
            return calls >= 4

        closed = []
        original_close = zipfile.ZipFile.close

        def close(archive):
            original_close(archive)
            closed.append(archive.fp is None)

        with patch.object(zipfile.ZipFile, "close", close):
            self.assertEqual(self.load(should_cancel=should_cancel), ([], []))
            self.assertTrue(closed)
            self.assertTrue(all(closed))
        path.unlink()

    def test_cancellation_before_loading_avoids_archive_scan(self):
        with patch.object(self.gui.CycleAuditDialog, "_candidate_capture_files", side_effect=AssertionError("cancelled scan")):
            self.assertEqual(self.load(should_cancel=lambda: True), ([], []))

    def test_stage_markers_and_table_keep_only_actual_changes(self):
        events = [
            {"stage_before": "1_WAIT_DROP", "stage_after": "1_WAIT_DROP", "event_type": "ATR_UPDATE"},
            {"stage_before": "1_WAIT_DROP", "stage_after": "2_BUY_TRAIL", "event_type": "BUY_SUBMIT"},
            {"stage_before": "", "stage_after": "1_WAIT_DROP", "event_type": "START"},
            {"stage_before": "5_CYCLE_COMPLETE", "stage_after": "", "event_type": "END"},
            {"stage_before": " 2_BUY_TRAIL ", "stage_after": "2_BUY_TRAIL", "event_type": "PRICE"},
        ]
        self.details["decision_events"] = events
        original = copy.deepcopy(events)
        widget = self.gui.CycleTimelineWidget(self.row, self.details)
        self.assertEqual([item["event_type"] for item in widget._build_stage_transitions()], ["BUY_SUBMIT"])
        tables = []

        def table(records, *args, **kwargs):
            tables.append(records)
            return Dummy()

        with patch.object(self.gui.CycleAuditDialog, "_records_table", side_effect=table):
            self.gui.CycleAuditDialog._timeline_tab(self.row, self.details)
        self.assertEqual([item["event_type"] for item in tables[0]], ["BUY_SUBMIT"])
        self.assertEqual(events, original)


if __name__ == "__main__":
    unittest.main()
