"""Historical GUI values keep persisted identity, settings and fill evidence."""

from __future__ import annotations

import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from app.models import StrategySettings
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs

ROOT = Path(__file__).resolve().parents[1]


class HistoricalAuditGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(ROOT)
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def summary_values(self, row, details):
        with patch.object(self.gui.CycleAuditDialog, "_multi_pair_key_value_table", return_value=Dummy()) as table:
            self.gui.CycleAuditDialog._summary_tab(row, details)
        return dict(table.call_args.args[0])

    def markers(self, cycle, executions=(), orders=()):
        widget = self.gui.CycleTimelineWidget(
            cycle, {"cycle": cycle, "executions": list(executions), "orders": list(orders)}
        )
        return {marker["label"]: marker for marker in widget._build_markers()}

    def test_history_selection_preserves_cycle_identity_after_prepend_and_reorder(self):
        panel = self.gui.FlowchartPanel()
        first = {"id": "first", "ticker": "AAA"}
        second = {"id": "second", "ticker": "BBB"}
        panel.set_history_rows([first, second])
        panel.history_combo.setCurrentIndex(2)
        panel.set_history_rows([{"id": "new", "ticker": "CCC"}, second, first])
        self.assertEqual(panel.flowchart._cycle["id"], "second")
        panel.set_history_rows([second, first])
        self.assertEqual(panel.history_combo.currentIndex(), 1)
        self.assertEqual(panel.flowchart._cycle["id"], "second")

    def test_removed_historical_cycle_returns_to_current(self):
        panel = self.gui.FlowchartPanel()
        panel.update_data({"id": "live", "ticker": "LIVE"}, None, StrategySettings())
        panel.set_history_rows([{"id": "old", "ticker": "OLD"}])
        panel.history_combo.setCurrentIndex(1)
        panel.set_history_rows([{"id": "different", "ticker": "NEW"}])
        self.assertEqual(panel.history_combo.currentIndex(), 0)
        self.assertEqual(panel.flowchart._cycle["id"], "live")

    def test_row_without_persisted_id_does_not_reuse_an_unrelated_index(self):
        panel = self.gui.FlowchartPanel()
        panel.set_history_rows([{"ticker": "AAA"}])
        panel.history_combo.setCurrentIndex(1)
        panel.set_history_rows([{"ticker": "BBB"}])
        self.assertEqual(panel.history_combo.currentIndex(), 0)

    def test_all_recorded_settings_override_current_strategy(self):
        panel = self.gui.FlowchartPanel()
        recorded = StrategySettings(
            ticker="OLD", atr_adaptive_enabled=False, atr_period=9, atr_bar_seconds=300,
            atr_adapt_minimum_profit_enabled=False, atr_protective_sell_multiplier=2.4,
            auto_repeat=False, reinvest_profits=False, rth_only=False, currency="EUR",
            what_if_check_enabled=False, stale_data_guard_enabled=False,
            volatility_filter_enabled=True, volatility_window_seconds=600,
            session_timing_guard_enabled=False, cancel_sell_and_liquidate_before_close_enabled=True,
        )
        self.assertEqual(asdict(panel._strategy_from_history_row(asdict(recorded))), asdict(recorded))

    def test_missing_legacy_settings_never_inherit_current_configuration(self):
        panel = self.gui.FlowchartPanel()
        row = {"id": "legacy", "ticker": "OLD", "configured_initial_drop_pct": 0,
               "configured_min_profit_pct": 0, "con_id": 42, "reinvest_profits": False}
        before = asdict(panel._strategy_from_history_row(row))
        panel._current_strategy = StrategySettings(atr_period=99, currency="EUR", auto_repeat=False)
        self.assertEqual(asdict(panel._strategy_from_history_row(row)), before)
        self.assertEqual(before["initial_drop_pct"], 0)
        self.assertEqual(before["rise_trigger_pct"], 0)
        self.assertEqual(before["contract_con_id"], 42)
        panel.set_history_rows([row])
        panel.history_combo.setCurrentIndex(1)
        panel._redraw()
        self.assertIn("Not recorded; fixed display defaults used:", panel.explanation_label.text())
        self.assertIn("auto repeat", panel.history_combo.toolTip())
        panel.history_combo.setCurrentIndex(0)
        panel._redraw()
        self.assertTrue(panel.explanation_label.text().startswith("Live explanation view"))
        self.assertEqual(panel.history_combo.toolTip(), "")

    def test_completion_markers_use_aggregate_prices_and_completion_times(self):
        cycle = {
            "avg_buy_price": 10.25, "buy_filled_at": "2026-10-01T10:02:00+00:00",
            "avg_sell_price": 13.691500630517025, "sell_filled_at": "2026-10-01T10:30:00+00:00",
            "protective_avg_sell_price": 9.85, "protective_sell_filled_at": "2026-10-01T10:15:00+00:00",
        }
        executions = [
            {"side": "BUY", "avg_price": 10.5, "executed_at": "2026-10-01T10:01:00+00:00"},
            {"side": "SELL", "order_ref": "SELL_TRAIL", "avg_price": 13.71,
             "executed_at": "2026-10-01T10:29:00+00:00"},
            {"side": "SELL", "order_ref": "PROTECTIVE_SELL", "avg_price": 9.9,
             "executed_at": "2026-10-01T10:14:00+00:00"},
        ]
        markers = self.markers(cycle, executions)
        for label, price_key, time_key in (
            ("BUY", "avg_buy_price", "buy_filled_at"),
            ("FINAL SELL", "avg_sell_price", "sell_filled_at"),
            ("PROTECTIVE SELL", "protective_avg_sell_price", "protective_sell_filled_at"),
        ):
            self.assertEqual(markers[label]["price"], cycle[price_key])
            self.assertEqual(markers[label]["time"], self.gui._parse_timestamp(cycle[time_key]))

    def test_without_aggregate_execution_price_keeps_its_own_timestamp(self):
        execution = {"side": "BUY", "price": 10.5, "executed_at": "2026-10-01T10:01:00+00:00"}
        marker = self.markers({"buy_filled_at": "2026-10-01T10:02:00+00:00"}, [execution])["BUY"]
        self.assertEqual(marker["price"], 10.5)
        self.assertEqual(marker["time"], self.gui._parse_timestamp(execution["executed_at"]))

    def test_unfilled_submission_marker_is_still_available(self):
        markers = self.markers(
            {"buy_initial_trail_stop_price": 10.5},
            orders=[{"action": "BUY", "created_at": "2026-10-01T10:00:00+00:00"}],
        )
        self.assertEqual(markers["BUY"]["price"], 10.5)
        self.assertEqual(markers["BUY"]["time"], self.gui._parse_timestamp("2026-10-01T10:00:00+00:00"))

    def test_summary_preserves_zero_percentages_quantities_and_pnl(self):
        values = self.summary_values({
            "configured_initial_drop_pct": 0, "configured_buy_rebound_pct": 0,
            "configured_min_profit_pct": 0, "configured_sell_trail_pct": 0,
            "configured_slippage_buffer_pct": 0, "gross_pnl": 0, "net_pnl": 0,
        }, {"cycle": {"initial_drop_pct": 2, "rise_trigger_pct": 3,
                       "buy_filled_qty": 0, "sell_filled_qty": 0, "gross_pnl": 100, "net_pnl": 90}})
        self.assertEqual(values["Entry condition"], "Initial drop 0%; BUY rebound 0%")
        self.assertEqual(values["Exit condition"], "Minimum profit 0%; SELL trailing-stop 0%")
        self.assertEqual(values["Slippage"], 0)
        self.assertTrue(values["Buy fill"].startswith("0 @"))
        self.assertTrue(values["Sell fill"].startswith("0 @"))
        self.assertEqual(values["Gross / net P&L"], "$0.0000 / $0.0000")

    def test_summary_derives_matching_buy_order_type_and_duration(self):
        values = self.summary_values({}, {
            "cycle": {"buy_order_ref": "correct", "buy_filled_at": "2026-10-01T12:00:00+02:00",
                      "sell_filled_at": "2026-10-01T10:03:30+00:00"},
            "orders": [{"action": "SELL", "order_type": "MKT"},
                       {"action": "BUY", "order_ref": "wrong", "order_type": "MKT"},
                       {"action": "BUY", "order_ref": "correct", "order_type": "TRAIL"}],
        })
        self.assertEqual(values["Buy order type"], "TRAIL")
        self.assertEqual(values["Duration"], "3.50 min")

    def test_summary_buy_order_id_and_raw_order_type_fallback(self):
        values = self.summary_values({}, {
            "cycle": {"buy_order_id": 42},
            "orders": [{"action": "BUY", "order_id": 41, "order_type": "TRAIL"},
                       {"action": "BUY", "order_id": 42, "raw_json": '{"order": {"orderType": "MKT"}}'}],
        })
        self.assertEqual(values["Buy order type"], "MKT")

    def test_summary_does_not_guess_order_type_from_unrelated_order(self):
        values = self.summary_values({}, {"cycle": {"buy_order_ref": "missing"},
                                         "orders": [{"action": "BUY", "order_ref": "other", "order_type": "TRAIL"}]})
        self.assertEqual(values["Buy order type"], "Not available from audit rows")

    def test_summary_does_not_guess_between_multiple_buy_orders_without_identity(self):
        values = self.summary_values({}, {
            "orders": [{"action": "BUY", "order_type": "MKT"},
                       {"action": "BUY", "order_type": "TRAIL"}],
        })
        self.assertEqual(values["Buy order type"], "Not available from audit rows")

    def test_summary_zero_duration_and_protective_exit_duration(self):
        for end, expected in (("2026-10-01T10:00:00+00:00", "0.00 min"),
                              ("2026-10-01T10:05:00+00:00", "5.00 min")):
            with self.subTest(end=end):
                values = self.summary_values({}, {"cycle": {"buy_filled_at": "2026-10-01T10:00:00+00:00",
                                                             "protective_sell_filled_at": end}})
                self.assertEqual(values["Duration"], expected)

    def test_summary_invalid_or_reversed_duration_stays_unavailable(self):
        for end in (None, "bad time", "2026-10-01T09:00:00+00:00"):
            with self.subTest(end=end):
                values = self.summary_values({}, {"cycle": {"buy_filled_at": "2026-10-01T10:00:00+00:00",
                                                             "sell_filled_at": end}})
                self.assertEqual(values["Duration"], "Not available from audit rows")

    def test_summary_keeps_existing_explicit_evidence(self):
        values = self.summary_values({"buy_order_type": "MKT", "holding_minutes_display": "1.5 min"}, {})
        self.assertEqual(values["Buy order type"], "MKT")
        self.assertEqual(values["Duration"], "1.5 min")


if __name__ == "__main__":
    unittest.main()
