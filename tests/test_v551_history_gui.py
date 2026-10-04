"""History reporting uses GUI filters without changing strategy or broker state."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub

ROOT = Path(__file__).resolve().parents[1]


def summary(count=3, net=53.158846):
    return {"cycles": count, "total_net_pnl": net, "total_commissions": 3.0}


class HistoryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(ROOT)
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        self.controller = _ControllerStub()
        self.controller.refresh_history = Mock()
        self.window = self.gui.MainWindow(self.controller)
        self.controller.refresh_history.reset_mock()

    def test_blank_ticker_requests_all_database_history(self):
        self.window._request_history_refresh(force=True)
        self.controller.refresh_history.assert_called_once_with(filters={})

    def test_all_visible_filters_are_queued_as_plain_values(self):
        w = self.window
        w.history_ticker_filter.setText("  sh  ")
        w.history_from_filter.setText("2026-09-01")
        w.history_to_filter.setText("2026-10-03")
        w.history_outcome_filter.setCurrentText("Losing")
        w.history_atr_filter.setCurrentText("ATR off")
        w.history_mode_filter.setCurrentText("Live")
        w._apply_history_filters()
        self.controller.refresh_history.assert_called_once_with(filters={
            "ticker": "SH", "date_from": "2026-09-01", "date_to": "2026-10-03",
            "outcome": "Losing", "atr": "ATR off", "mode": "Live",
        })

    def test_unchanged_filters_do_not_queue_recurring_database_reads(self):
        self.window._apply_history_filters()
        self.window._apply_history_filters(force_table=True)
        self.controller.refresh_history.assert_not_called()
        self.window.history_ticker_filter.setText("SHELL")
        self.window._apply_history_filters()
        self.window._apply_history_filters(force_flowchart=True)
        self.assertEqual(self.controller.refresh_history.call_count, 1)

    def test_clearing_ticker_reloads_all_tickers(self):
        self.window.history_ticker_filter.setText("SHELL")
        self.window._apply_history_filters()
        self.window.history_ticker_filter.setText("")
        self.window._apply_history_filters()
        self.assertEqual(self.controller.refresh_history.call_args.kwargs, {"filters": {}})
        self.assertEqual(self.controller.refresh_history.call_count, 2)

    def test_delayed_old_filter_result_is_ignored(self):
        self.window.history_ticker_filter.setText("SHELL")
        self.window._apply_history_filters()
        self.window._on_history({"filters": {}, "rows": [{"ticker": "TDIV"}], "summary": summary()})
        self.assertEqual(self.window._all_history_rows, [])
        self.assertEqual(self.window.history_summary_cards["Total cycles"].value.text(), "-")

    def test_summary_uses_full_database_count_instead_of_table_count(self):
        rows = [{"id": str(i), "ticker": "OLD", "stage": Stage.CYCLE_COMPLETE.value} for i in range(500)]
        self.window._on_history({"filters": {}, "rows": rows, "summary": summary(750, 750.0)})
        self.assertEqual(len(self.window._visible_history_rows), 500)
        self.assertEqual(self.window.history_summary_cards["Total cycles"].value.text(), "750")
        self.assertEqual(self.window.history_summary_cards["Total net P/L"].value.text(), "$750.0000")

    def test_no_matching_filter_does_not_insert_example_or_count_it(self):
        self.window.history_ticker_filter.setText("MISSING")
        self.window._apply_history_filters()
        self.window._on_history({"filters": {"ticker": "MISSING"}, "rows": [], "summary": summary(0, 0.0)})
        self.assertEqual(self.window._visible_history_rows, [])
        self.assertEqual(self.window.history_summary_cards["Total cycles"].value.text(), "0")

    def test_empty_database_example_does_not_inflate_summary(self):
        self.window._on_history({"filters": {}, "rows": [], "summary": summary(0, 0.0)})
        self.assertTrue(self.window._all_history_rows[0]["__example"])
        self.assertEqual(self.window.history_summary_cards["Total cycles"].value.text(), "0")

    def test_periodic_snapshot_cannot_overwrite_new_filter_summary(self):
        w = self.window
        w.history_ticker_filter.setText("SHELL")
        w._apply_history_filters()
        w._on_history({"filters": {"ticker": "SHELL"}, "rows": [], "summary": summary(2, 28.23866)})
        snapshot = {
            "connected": False, "connection": asdict(self.controller.connection),
            "strategy": asdict(self.controller.strategy), "history_filters": {},
            "history_summary": summary(99, 999.0),
        }
        w._on_snapshot(snapshot)
        self.assertEqual(w.history_summary_cards["Total cycles"].value.text(), "2")
        snapshot["history_filters"] = {"ticker": "SHELL"}
        snapshot["history_summary"] = summary(2, 30.0)
        w._on_snapshot(snapshot)
        self.assertEqual(w.history_summary_cards["Total net P/L"].value.text(), "$30.0000")


class HistoryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = "app._v551_history_controller"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app/controller.py")
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.module
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)
        cls.addClassCleanup(sys.modules.pop, name, None)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "bot.sqlite"
        self.storage = BotStorage(self.path)
        for ticker, net in (("SHELL", 30.0), ("TDIV", -5.0)):
            cycle = StrategyEngine.start_cycle(StrategySettings(ticker=ticker), 1, "SIM", 100.0, 0.0)
            cycle.stage = Stage.CYCLE_COMPLETE
            cycle.buy_filled_qty = cycle.sell_filled_qty = 10
            cycle.avg_buy_price = 100.0
            cycle.net_pnl = net
            self.storage.upsert_cycle(cycle)
        self.c = self.module.TradingController(storage=self.storage)
        self.addCleanup(self.c._market_capture.shutdown)
        self.c.strategy.ticker = "ARGX"
        self.before = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def assert_no_writes_or_strategy_change(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), self.before)
        self.assertEqual(self.c.strategy.ticker, "ARGX")
        self.assertIsNone(self.c.active_cycle)

    def test_initial_summary_counts_all_tickers_despite_new_strategy_ticker(self):
        cache = self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(cache["history_summary"]["cycles"], 2)
        self.assertEqual(cache["history_summary"]["total_net_pnl"], 25.0)
        self.assertEqual(cache["history_filters"], {})
        self.assert_no_writes_or_strategy_change()

    def test_filter_command_updates_rows_and_summary_then_clears_to_all(self):
        self.c._handle_command("REFRESH_HISTORY", {"filters": {"ticker": "SH"}})
        payload = self.c.signals.history_updated.emissions[-1][0][0]
        self.assertEqual([row["ticker"] for row in payload["rows"]], ["SHELL"])
        self.assertEqual(payload["summary"]["cycles"], 1)
        self.assertEqual(payload["filters"], {"ticker": "SH"})
        self.assertEqual(self.c._refresh_snapshot_database_cache(force=True)["history_summary"]["cycles"], 1)
        self.c._handle_command("REFRESH_HISTORY", {"filters": {}})
        payload = self.c.signals.history_updated.emissions[-1][0][0]
        self.assertEqual(payload["summary"]["cycles"], 2)
        self.assertEqual({row["ticker"] for row in payload["rows"]}, {"SHELL", "TDIV"})
        self.assert_no_writes_or_strategy_change()

    def test_default_initial_history_response_carries_filter_identity(self):
        self.c._handle_command("REFRESH_HISTORY", {"ticker": ""})
        payload = self.c.signals.history_updated.emissions[-1][0][0]
        self.assertEqual(payload["filters"], {})
        self.assertEqual(payload["summary"]["cycles"], 2)

    def test_queued_filter_dictionary_is_detached_from_gui_mutation(self):
        filters = {"ticker": "SHELL"}
        self.c.refresh_history(filters=filters)
        filters["ticker"] = "TDIV"
        name, payload = self.c._commands.get_nowait()
        self.assertEqual(name, "REFRESH_HISTORY")
        self.assertEqual(payload["filters"], {"ticker": "SHELL"})

    def test_old_filter_summary_not_reused_after_failed_new_filter_read(self):
        self.c._refresh_snapshot_database_cache(force=True)
        self.c._history_filters = {"ticker": "MISSING"}
        with patch.object(self.storage, "history_summary", side_effect=RuntimeError("read unavailable")):
            cache = self.c._refresh_snapshot_database_cache(force=True)
        self.assertEqual(cache["history_summary"], {})
        self.assertIn("history_summary", cache["errors"])
        self.assert_no_writes_or_strategy_change()


if __name__ == "__main__":
    unittest.main()
