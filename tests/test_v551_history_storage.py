"""Full-database history filters and summary regressions for version 5.5.1."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine


class HistoryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = BotStorage(Path(self.temp.name) / "history.sqlite")
        self.number = 0

    def cycle(
        self, ticker: str, *, net: float = 10.0, date: str = "2026-01-01",
        atr: bool = True, account: str = "U123", protective: bool = False,
    ):
        self.number += 1
        settings = StrategySettings(ticker=ticker, atr_adaptive_enabled=atr)
        cycle = StrategyEngine.start_cycle(settings, self.number, account, 100.0, 0.0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.buy_filled_qty = cycle.sell_filled_qty = 10
        cycle.avg_buy_price = 100.0
        cycle.avg_sell_price = 100.0 + (net + 2.0) / 10.0
        cycle.buy_commission = cycle.sell_commission = 1.0
        cycle.gross_pnl = net + 2.0
        cycle.net_pnl = net
        cycle.created_at = f"{date}T09:00:00+00:00"
        cycle.buy_filled_at = f"{date}T10:00:00+00:00"
        cycle.sell_filled_at = f"{date}T11:00:00+00:00"
        cycle.updated_at = cycle.sell_filled_at
        if protective:
            cycle.protective_sell_filled_qty = 10
        return cycle

    def save(self, *cycles) -> None:
        with self.storage.connect() as con:
            for cycle in cycles:
                self.storage._upsert_cycle_in_connection(con, cycle)

    def assert_matches(self, filters: dict[str, str], expected: list[str]) -> None:
        rows = self.storage.history_cycles(filters=filters)
        self.assertEqual(sorted(row["ticker"] for row in rows), sorted(expected))
        summary = self.storage.history_summary(filters=filters)
        self.assertEqual(summary["cycles"], len(expected))
        self.assertAlmostEqual(summary["total_net_pnl"], sum(row["net_pnl"] for row in rows))

    def seed_filter_cases(self) -> None:
        self.save(
            self.cycle("AAA", net=10.0, date="2026-01-01"),
            self.cycle("AAB", net=-4.0, date="2026-01-02", atr=False, account="DU123"),
            self.cycle("BBB", net=0.0, date="2026-01-03", account=""),
            self.cycle("CCC", net=3.0, date="2026-01-04", atr=False, account="custom-paper"),
            self.cycle("DDD", net=5.0, date="2026-01-05", protective=True),
            self.cycle("EEE", net=-2.0, date="2026-01-06", protective=True),
        )

    def test_blank_filters_cover_all_tickers_independently_of_strategy_ticker_argument(self) -> None:
        self.seed_filter_cases()
        self.assert_matches({}, ["AAA", "AAB", "BBB", "CCC", "DDD", "EEE"])
        self.assertEqual(self.storage.history_summary("UNRELATED", filters={})["cycles"], 6)
        self.assertEqual(len(self.storage.history_cycles("UNRELATED", filters={})), 6)
        self.assertEqual(self.storage.history_summary()["cycles"], 6)
        self.assertEqual(self.storage.history_summary("AAA")["cycles"], 1)
        self.assertEqual(self.storage.history_summary("AA")["cycles"], 0)

    def test_ticker_filter_is_trimmed_case_insensitive_literal_substring(self) -> None:
        self.seed_filter_cases()
        self.assert_matches({"ticker": " aa "}, ["AAA", "AAB"])
        self.assert_matches({"ticker": " aab "}, ["AAB"])
        self.assert_matches({"ticker": "MISSING"}, [])

    def test_ticker_filter_neither_interpolates_sql_nor_expands_wildcards(self) -> None:
        self.save(self.cycle("A%B"), self.cycle("A_B"), self.cycle("A'B"), self.cycle("ABC"))
        self.assert_matches({"ticker": "%"}, ["A%B"])
        self.assert_matches({"ticker": "_"}, ["A_B"])
        self.assert_matches({"ticker": "'"}, ["A'B"])
        self.assert_matches({"ticker": "' OR 1=1 --"}, [])
        self.assertEqual(self.storage.history_summary()["cycles"], 4)

    def test_filtering_precedes_table_limit_and_summary_has_no_500_cycle_cap(self) -> None:
        older = self.cycle("OLD", date="2025-12-01", net=7.0)
        recent = [self.cycle("NEW", date="2026-01-31", net=1.0) for _ in range(503)]
        self.save(older, *recent)
        self.assertEqual(self.storage.history_summary(filters={})["cycles"], 504)
        self.assertEqual(self.storage.history_summary(filters={})["total_net_pnl"], 510.0)
        self.assertEqual(len(self.storage.history_cycles(filters={})), 500)
        self.assert_matches({"ticker": "OLD"}, ["OLD"])
        self.assert_matches({"date_to": "2025-12-31"}, ["OLD"])
        self.assertEqual(len(self.storage.history_cycles(limit=7, filters={"ticker": "NEW"})), 7)

    def test_dates_are_inclusive_and_use_available_timestamp_fallback(self) -> None:
        self.seed_filter_cases()
        self.assert_matches({"date_from": "2026-01-02", "date_to": "2026-01-04"}, ["AAB", "BBB", "CCC"])
        with self.storage.connect() as con:
            con.execute("UPDATE cycles SET sell_filled_at='' WHERE ticker='AAA'")
            con.execute("UPDATE cycles SET sell_filled_at='', buy_filled_at='' WHERE ticker='AAB'")
            con.execute("UPDATE cycles SET sell_filled_at='', buy_filled_at='', updated_at='' WHERE ticker='BBB'")
            con.execute("UPDATE cycles SET sell_filled_at='', buy_filled_at='', updated_at='', created_at='' WHERE ticker='CCC'")
        self.assert_matches({"date_to": "2026-01-03"}, ["AAA", "AAB", "BBB", "CCC"])
        self.assert_matches({"date_from": "2026-12-01"}, ["CCC"])

    def test_outcome_filters_include_breakeven_and_preserve_protective_badges(self) -> None:
        self.seed_filter_cases()
        for outcome, expected in [
            ("Profitable", ["AAA", "BBB", "CCC", "DDD"]),
            ("Losing", ["AAB", "EEE"]),
            ("Profit exit", ["AAA", "BBB", "CCC"]),
            ("Protective exit", ["DDD", "EEE"]),
            ("Manual/error", []), ("Cancelled", []),
        ]:
            with self.subTest(outcome=outcome):
                self.assert_matches({"outcome": outcome}, expected)
        # A protective order which never filled must not reclassify the exit.
        with self.storage.connect() as con:
            con.execute("UPDATE cycles SET protective_sell_order_ref='APP-PROTECT-1' WHERE ticker='AAA'")
        self.assert_matches({"outcome": "Protective exit"}, ["DDD", "EEE"])

    def test_atr_and_account_mode_match_history_controls(self) -> None:
        self.seed_filter_cases()
        self.assert_matches({"atr": "ATR on"}, ["AAA", "BBB", "DDD", "EEE"])
        self.assert_matches({"atr": "ATR off"}, ["AAB", "CCC"])
        self.assert_matches({"mode": "Paper"}, ["AAB", "BBB", "CCC"])
        self.assert_matches({"mode": "Live"}, ["AAA", "BBB", "DDD", "EEE"])
        self.assert_matches({"mode": "Paper", "atr": "ATR off", "outcome": "Profitable"}, ["CCC"])
        self.assert_matches({"ticker": "A", "date_from": "2026-01-02", "mode": "Paper", "atr": "ATR off", "outcome": "Losing"}, ["AAB"])

    def test_only_completed_cycles_count(self) -> None:
        complete = self.cycle("AAA")
        stopped = self.cycle("BBB")
        stopped.stage = Stage.STOPPED
        active = self.cycle("CCC")
        active.stage = Stage.WAIT_RISE_TRIGGER
        self.save(complete, stopped, active)
        self.assert_matches({}, ["AAA"])

    def test_filtered_summary_preserves_existing_metric_calculations(self) -> None:
        self.seed_filter_cases()
        self.assertEqual(self.storage.history_summary(filters={}), self.storage.history_summary())
        summary = self.storage.history_summary(filters={"ticker": "AA"})
        self.assertEqual(summary["cycles"], 2)
        self.assertEqual(summary["total_net_pnl"], 6.0)
        self.assertEqual(summary["total_commissions"], 4.0)
        self.assertEqual(summary["win_rate_pct"], 50.0)
        self.assertAlmostEqual(summary["avg_net_pct"], 0.3)
        self.assertEqual(summary["avg_holding_minutes"], 60.0)
        self.assertEqual(summary["max_completed_drawdown"], 4.0)

    def test_filtered_cache_reuses_summary_but_checks_database_metadata(self) -> None:
        self.seed_filter_cases()
        first = self.storage.history_summary(filters={"ticker": "AA"})
        statements: list[str] = []
        original_connect = self.storage.connect

        def traced_connect():
            con = original_connect()
            con.set_trace_callback(statements.append)
            return con

        with patch.object(self.storage, "connect", side_effect=traced_connect):
            second = self.storage.history_summary(filters={"ticker": " aa "})
        self.assertEqual(second, first)
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)
        self.assertIn("COUNT(*)", selects[0])
        self.assertFalse(any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for statement in statements))
        second["total_net_pnl"] = 999.0
        self.assertEqual(self.storage.history_summary(filters={"ticker": "AA"}), first)

    def test_filtered_cache_invalidates_for_new_and_updated_completed_cycles(self) -> None:
        self.save(self.cycle("AAA", net=1.0))
        self.assertEqual(self.storage.history_summary(filters={})["total_net_pnl"], 1.0)
        self.save(self.cycle("BBB", net=2.0))
        self.assertEqual(self.storage.history_summary(filters={})["total_net_pnl"], 3.0)
        with self.storage.connect() as con:
            con.execute("UPDATE cycles SET net_pnl=5, updated_at='2026-02-01T00:00:00+00:00' WHERE ticker='AAA'")
        self.assertEqual(self.storage.history_summary(filters={})["total_net_pnl"], 7.0)

    def test_filter_cache_is_single_entry_and_does_not_grow_legacy_ticker_cache(self) -> None:
        self.seed_filter_cases()
        self.storage.history_summary("AAA")
        legacy_cache = dict(self.storage._history_summary_cache)
        for number in range(100):
            self.storage.history_summary(filters={"ticker": f"NO-{number}"})
        self.assertEqual(self.storage._history_summary_cache, legacy_cache)
        cached = self.storage._history_filtered_summary_cache
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0][0], "NO-99")
        self.assert_matches({"ticker": "AAA"}, ["AAA"])
        self.assert_matches({}, ["AAA", "AAB", "BBB", "CCC", "DDD", "EEE"])


if __name__ == "__main__":
    unittest.main()
