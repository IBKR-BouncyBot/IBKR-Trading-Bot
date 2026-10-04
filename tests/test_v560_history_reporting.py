"""G10/G11/G15: consistent exports, executed exits and change-driven rows."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub

ROOT = Path(__file__).resolve().parents[1]


class HistoryReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = "app._v560_history_controller"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app/controller.py")
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.module
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)
        cls.addClassCleanup(sys.modules.pop, name, None)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.storage = BotStorage(self.folder / "history.sqlite")
        self.number = 0
        self.c = self.module.TradingController(storage=self.storage)
        self.addCleanup(self.c._market_capture.shutdown)

    def cycle(self, ticker="AAA", *, net=10.0, date="2026-10-01", atr=True, account="U123"):
        self.number += 1
        settings = StrategySettings(ticker=ticker, atr_adaptive_enabled=atr)
        cycle = StrategyEngine.start_cycle(settings, self.number, account, 100.0, 0.0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.buy_filled_qty = cycle.sell_filled_qty = 10
        cycle.avg_buy_price = 100.0
        cycle.avg_sell_price = 100.0 + net / 10
        cycle.gross_pnl = cycle.net_pnl = net
        cycle.buy_filled_at = f"{date}T10:00:00+00:00"
        cycle.sell_filled_at = cycle.updated_at = f"{date}T11:00:00+00:00"
        return cycle

    def save(self, *cycles):
        with self.storage.connect() as con:
            for cycle in cycles:
                self.storage._upsert_cycle_in_connection(con, cycle)

    def last_payload(self):
        return self.c.signals.history_updated.emissions[-1][0][0]

    def test_csv_uses_all_filters_and_literal_ticker_substring(self):
        wanted = self.cycle("SHELL", net=-3, atr=False, account="DU123")
        self.save(wanted, self.cycle("TDIV", net=-3), self.cycle("SHELL", net=5),
                  self.cycle("SHELL", net=-1, date="2026-09-01"))
        filters = {"ticker": "SH", "date_from": "2026-10-01", "date_to": "2026-10-01",
                   "outcome": "Losing", "atr": "ATR off", "mode": "Paper"}
        before = hashlib.sha256(self.storage.db_path.read_bytes()).hexdigest()
        with patch.object(self.module, "exports_dir", return_value=self.folder):
            target = self.c.export_history(filters=filters)
        with target.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["ticker"] for row in rows], ["SHELL"])
        self.assertEqual(float(rows[0]["net_pnl"]), -3)
        self.assertEqual(hashlib.sha256(self.storage.db_path.read_bytes()).hexdigest(), before)
        self.assertEqual(self.storage.history_summary(filters=filters)["cycles"], len(rows))

    def test_csv_is_unbounded_and_empty_filter_exports_only_header(self):
        self.save(*(self.cycle() for _ in range(504)))
        with patch.object(self.storage, "history_cycles", wraps=self.storage.history_cycles) as query:
            target = self.storage.export_history_csv(self.folder / "all.csv", filters={})
        self.assertEqual(query.call_args.kwargs["limit"], -1)
        with target.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 504)
        target = self.storage.export_history_csv(self.folder / "none.csv", filters={"ticker": "NONE"})
        with target.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(list(reader), [])
            self.assertIn("ticker", reader.fieldnames)

    def test_legacy_csv_ticker_remains_exact(self):
        self.save(self.cycle("AA"), self.cycle("AAA"))
        target = self.storage.export_history_csv(self.folder / "legacy.csv", "AA")
        with target.open(newline="", encoding="utf-8") as handle:
            self.assertEqual([r["ticker"] for r in csv.DictReader(handle)], ["AA"])

    def test_exit_badge_and_sql_require_fills_not_unused_protective_reference(self):
        ordinary = self.cycle("ORDINARY")
        ordinary.protective_sell_order_ref = "BOT-PROTECT-CANCELLED"
        protective = self.cycle("PROTECTIVE")
        protective.protective_sell_filled_qty = 10
        legacy = self.cycle("LEGACY")
        legacy.sell_order_ref = "BOT-PROTECT-EXECUTED"
        unfilled = self.cycle("UNFILLED")
        unfilled.sell_filled_qty = 0
        unfilled.sell_order_ref = "BOT-PROTECT-NOFILL"
        self.save(ordinary, protective, legacy, unfilled)
        with imported_gui_with_stubs(ROOT) as gui:
            for row in self.storage.history_cycles():
                badge = gui.CycleAuditDialog._outcome_badge(row, {"cycle": row})
                self.assertEqual(badge == "PROTECTIVE EXIT", row["ticker"] in {"PROTECTIVE", "LEGACY"})
        self.assertEqual({r["ticker"] for r in self.storage.history_cycles(filters={"outcome": "Protective exit"})},
                         {"PROTECTIVE", "LEGACY"})
        self.assertEqual(self.storage.history_summary(filters={"outcome": "Profit exit"})["cycles"], 2)

    def test_new_completion_refreshes_rows_and_summary_once(self):
        self.save(self.cycle())
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        self.save(self.cycle("BBB"))
        with patch.object(self.storage, "history_cycles", wraps=self.storage.history_cycles) as read:
            self.c._refresh_snapshot_database_cache(force=True)
            for _ in range(4):
                self.c._refresh_snapshot_database_cache(force=True)
            self.assertEqual(read.call_count, 1)
        payload = self.last_payload()
        self.assertEqual(payload["summary"]["cycles"], 2)
        self.assertEqual({r["ticker"] for r in payload["rows"]}, {"AAA", "BBB"})

    def test_commission_and_nonaggregate_changes_refresh_rows(self):
        cycle = self.cycle()
        self.save(cycle)
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        cycle.sell_commission = 2.5
        cycle.net_pnl = 7.5
        cycle.updated_at = "2026-10-02T11:00:00+00:00"
        self.save(cycle)
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["rows"][0]["net_pnl"], 7.5)
        self.assertEqual(self.last_payload()["summary"]["total_net_pnl"], 7.5)
        cycle.sell_order_ref = "UPDATED-REFERENCE"
        cycle.updated_at = "2026-10-03T11:00:00+00:00"
        self.save(cycle)
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["rows"][0]["sell_order_ref"], "UPDATED-REFERENCE")

    def test_filtered_refresh_preserves_scope_after_completed_writes(self):
        a, b = self.cycle("AAA", net=-1), self.cycle("BBB", net=-1)
        self.save(a, b)
        filters = {"ticker": "AA", "outcome": "Losing"}
        self.c._handle_command("REFRESH_HISTORY", {"filters": filters})
        b.updated_at = "2026-10-02T11:00:00+00:00"
        self.save(b)
        with patch.object(self.storage, "history_cycles", wraps=self.storage.history_cycles) as read:
            self.c._refresh_snapshot_database_cache(force=True)
            self.assertEqual(read.call_count, 1)
            self.assertEqual([r["ticker"] for r in self.last_payload()["rows"]], ["AAA"])
            a.net_pnl = 1
            a.updated_at = "2026-10-03T11:00:00+00:00"
            self.save(a)
            self.c._refresh_snapshot_database_cache(force=True)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.last_payload()["filters"], filters)
        self.assertEqual(self.last_payload()["rows"], [])
        self.assertEqual(self.last_payload()["summary"]["cycles"], 0)

    def test_same_second_commission_correction_invalidates_summary_and_rows(self):
        cycle = self.cycle()
        self.save(cycle)
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        revision = self.c._history_rows_revision
        cycle.sell_commission = 3.5
        cycle.net_pnl = 6.5
        # Intentionally preserve the timestamp used in the cached summary.
        self.save(cycle)
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["summary"]["total_net_pnl"], 6.5)
        self.assertEqual(self.last_payload()["rows"][0]["sell_commission"], 3.5)
        self.assertNotEqual(revision, self.c._history_rows_revision)

    def test_failed_filter_change_with_equal_metadata_retries_new_scope(self):
        self.save(self.cycle("AAA"), self.cycle("BBB"))
        self.c._handle_command("REFRESH_HISTORY", {"filters": {"ticker": "AAA"}})
        with patch.object(self.storage, "history_cycles", side_effect=OSError("busy")), \
                self.assertRaises(OSError):
            self.c._handle_command("REFRESH_HISTORY", {"filters": {"ticker": "BBB"}})
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["filters"], {"ticker": "BBB"})
        self.assertEqual([r["ticker"] for r in self.last_payload()["rows"]], ["BBB"])

    def test_initial_failed_refresh_is_retried_by_existing_cadence(self):
        self.save(self.cycle())
        with patch.object(self.storage, "history_cycles", side_effect=OSError("busy")), \
                self.assertRaises(OSError):
            self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["summary"]["cycles"], 1)

    def test_failed_row_refresh_retries_without_marking_revision_delivered(self):
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        old_revision = self.c._history_rows_revision
        self.save(self.cycle())
        with patch.object(self.storage, "history_cycles", side_effect=OSError("busy")):
            cache = self.c._refresh_snapshot_database_cache(force=True)
        self.assertIn("history_rows", cache["errors"])
        self.assertEqual(self.c._history_rows_revision, old_revision)
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(self.last_payload()["summary"]["cycles"], 1)
        self.assertNotEqual(self.c._history_rows_revision, old_revision)

    def test_summary_failure_preserves_rows_until_successful_read(self):
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        self.save(self.cycle())
        with patch.object(self.storage, "history_summary", side_effect=OSError("busy")), \
                patch.object(self.storage, "history_cycles", wraps=self.storage.history_cycles) as read:
            self.c._refresh_snapshot_database_cache(force=True)
            read.assert_not_called()
        self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(len(self.last_payload()["rows"]), 1)

    def test_unchanged_cadence_does_not_reload_rows_or_write_database(self):
        self.save(self.cycle())
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        before = self.storage.db_path.read_bytes()
        with patch.object(self.storage, "history_cycles", wraps=self.storage.history_cycles) as read:
            for _ in range(5):
                self.c._refresh_snapshot_database_cache(force=True)
            read.assert_not_called()
        self.assertEqual(self.storage.db_path.read_bytes(), before)


class HistoryGuiAndSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        context = imported_gui_with_stubs(ROOT)
        cls.gui = context.__enter__()
        cls.addClassCleanup(context.__exit__, None, None, None)

    def test_gui_export_passes_every_current_filter(self):
        controller = _ControllerStub()
        controller.export_history = Mock(return_value=Path("export.csv"))
        window = self.gui.MainWindow(controller)
        window.history_ticker_filter.setText("SH")
        window.history_from_filter.setText("2026-09-01")
        window.history_to_filter.setText("2026-10-04")
        window.history_outcome_filter.setCurrentText("Losing")
        window.history_atr_filter.setCurrentText("ATR off")
        window.history_mode_filter.setCurrentText("Paper")
        window._export_history()
        controller.export_history.assert_called_once_with(filters={
            "ticker": "SH", "date_from": "2026-09-01", "date_to": "2026-10-04",
            "outcome": "Losing", "atr": "ATR off", "mode": "Paper",
        })

    def test_all_six_supplied_addresses_match_about_and_readme_exactly(self):
        expected = {
            "Cardano / ADA": "addr1q85w2v474ywzx868s69pghygek3vrhxm69e7c6ysuf28qhv8kmj5wd059grxl82f8h5mtyzl87cvqj8ldv2e0las7tnsdej9ax",
            "Ethereum / ETH": "0x78bDC85a97e2d87812Cc37e49936102d897B32d1",
            "Midnight / NIGHT": "addr1qyrzra5qhupeleruc3jezmswkfad32h9qz5lxa88ry2egm8686pww4mw030q7jrf05mjc20ez9ya0nyvuvjvs8v36tlsnhr5nd",
            "Solana / SOL": "3S69hjpdnkHgsdeBBQwHY9oLjHuqvw8rLzuAC2jc7CUY",
            "XRP": "rJfnMVkbCfVUsyyTxWaeE6b3LVFgqasitw",
            "Zcash / ZEC": "t1aDkPkv8n8jJiWFtueANZS2b89x1BsHmFq",
        }
        self.assertEqual(dict(self.gui.BOUNCYBOT_SUPPORT_ADDRESSES), expected)
        dialog = self.gui.AboutInfoDialog()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for label, address in expected.items():
            with self.subTest(label=label):
                self.assertEqual(dialog.support_address_fields[label].text(), address)
                self.assertIn(f"- {label}: `{address}`", readme)


if __name__ == "__main__":
    unittest.main()
