"""Contract-separated chart data and order-scoped trailing estimates (GUI only)."""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.models import Stage, StrategySettings
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs


class LiveGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        self.graph = self.gui.StrategyGraphWidget()
        self.strategy = StrategySettings(ticker="OLD", contract_con_id=1)
        self.cycle = {
            "id": "cycle-1", "ticker": "OLD", "con_id": 1, "currency": "USD",
            "stage": Stage.WAIT_INITIAL_DROP.value, "anchor_price": 150.0,
            "drop_trigger_price": 140.0, "rise_trigger_price": 155.0,
            "buy_rebound_trail_pct": 1.0, "sell_trailing_stop_pct": 1.0,
        }

    def update(self, cycle=None, *, price=100.0, now=100.0, contract=None, received_at=None, receipt=True, sequence=None, subscription="live-1"):
        snapshot = {"price": price, "contract": contract or {"ticker": "OLD", "con_id": 1, "currency": "USD"}}
        if receipt:
            snapshot["selected_price_basis_received_at"] = datetime.fromtimestamp(
                received_at if received_at is not None else now, timezone.utc,
            ).isoformat()
        if sequence is not None:
            snapshot["market_data_update_sequence"] = sequence
            snapshot["market_data_subscription_id"] = subscription
        before = deepcopy((cycle, snapshot, self.strategy))
        with patch.object(self.gui.time, "time", return_value=now):
            self.graph.update_data(cycle, snapshot, self.strategy, repaint=False)
        self.assertEqual((cycle, snapshot, self.strategy), before, "Graph must not modify trading inputs")
        return snapshot

    def levels(self):
        return {name: value for name, value, _color, _tag in self.graph._levels()}

    def submit(self, side, *, price=110.0, now=101.0):
        cycle = dict(self.cycle)
        cycle.update({
            "stage": Stage.BUY_TRAIL_ACTIVE.value if side == "buy" else Stage.SELL_TRAIL_ACTIVE.value,
            f"{side}_order_ref": f"cycle-1:{side}:1",
            f"{side}_initial_trail_stop_price": 111.1 if side == "buy" else 108.9,
        })
        self.update(cycle, price=price, now=now)
        return cycle

    def test_new_confirmed_contract_clears_retained_cycle_levels_and_graph(self):
        for stage in (Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value):
            with self.subTest(stage=stage):
                old = dict(self.cycle, stage=stage, avg_buy_price=100.0, avg_sell_price=110.0, last_price=110.0)
                self.update(old, price=110.0)
                self.update(old, price=25.0, now=101.0, contract={"ticker": "NEW", "con_id": 2, "currency": "EUR"})
                self.assertEqual(self.graph._display_ticker, "NEW")
                self.assertIsNone(self.graph._cycle)
                self.assertEqual(list(self.graph._history), [(101.0, 25.0)])
                self.assertEqual(self.levels(), {"Current price": 25.0})

    def test_contract_id_change_with_same_symbol_resets_history(self):
        self.update(self.cycle, price=110.0)
        self.update(self.cycle, price=25.0, now=101.0, contract={"ticker": "OLD", "con_id": 2, "currency": "USD"})
        self.assertIsNone(self.graph._cycle)
        self.assertEqual(list(self.graph._history), [(101.0, 25.0)])

    def test_fallback_identity_uses_currency_and_primary_exchange(self):
        for changed in ({"ticker": "NEW"}, {"currency": "EUR"}, {"primary_exchange": "OTHER"}):
            with self.subTest(changed=changed):
                old = dict(self.cycle, con_id=None, primary_exchange="NASDAQ")
                contract = {"ticker": "OLD", "currency": "USD", "primary_exchange": "NASDAQ"} | changed
                self.update(old, contract={"ticker": "OLD", "currency": "USD", "primary_exchange": "NASDAQ"})
                self.update(old, price=25.0, now=101.0, contract=contract)
                self.assertIsNone(self.graph._cycle)
                self.assertEqual(self.levels(), {"Current price": 25.0})

    def test_same_con_id_keeps_cycle_when_confirmed_symbol_is_an_alias(self):
        self.update(self.cycle, price=100.0)
        self.update(self.cycle, price=101.0, now=101.0, contract={"ticker": "ALIAS", "con_id": 1, "currency": "USD"})
        self.assertEqual(self.graph._display_ticker, "ALIAS")
        self.assertEqual(len(self.graph._history), 2)
        self.assertIn("Anchor", self.levels())

    def test_mismatched_cycle_last_price_is_not_used_when_new_contract_has_no_price(self):
        self.update(dict(self.cycle, last_price=150.0), price=None, contract={"ticker": "NEW", "con_id": 2})
        self.assertEqual(list(self.graph._history), [])
        self.assertEqual(self.levels(), {})

    def test_ribbon_uses_current_contract_and_omits_old_profit_trigger(self):
        bar = self.gui.LiveStatusBar()
        snapshot = {"active_cycle": dict(self.cycle, stage=Stage.CYCLE_COMPLETE.value),
                    "price_snapshot": {"price": 25.0, "contract": {"ticker": "NEW", "con_id": 2, "currency": "EUR", "exchange": "SMART"}}}
        before = deepcopy(snapshot)
        bar.update_data(snapshot)
        self.assertEqual(snapshot, before)
        self.assertEqual(bar.pills["Ticker"].value.text(), "NEW / SMART / EUR")
        self.assertNotIn("155", bar.pills["Trading"].detail.text())

    def test_matching_contract_and_missing_contract_preserve_existing_levels(self):
        self.update(self.cycle, price=100.0)
        self.assertEqual(self.levels()["Anchor"], 150.0)
        self.graph.update_data(self.cycle, {"price": 110.0}, self.strategy, repaint=False)
        self.assertEqual(self.levels()["Anchor"], 150.0)
        self.assertEqual(self.graph._display_ticker, "OLD")

    def test_pre_order_extrema_do_not_affect_initial_buy_or_sell_estimates(self):
        for side, pre_price, expected in (("buy", 50.0, 111.1), ("sell", 150.0, 108.9)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle, price=pre_price)
                self.submit(side)
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)

    def test_post_order_prices_move_buy_down_and_sell_up_but_never_reverse(self):
        for side, prices, expected in (("buy", (100.0, 105.0), 101.0), ("sell", (120.0, 115.0), 118.8)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = self.submit(side)
                for now, price in enumerate(prices, start=102):
                    self.update(cycle, price=price, now=float(now))
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)

    def test_estimate_extrema_survive_time_and_count_pruning(self):
        for side, extreme, expected in (("buy", 90.0, 90.9), ("sell", 130.0, 128.7)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.graph._history_max_points = 1
                self.graph._history_max_age_seconds = 1
                self.update(self.cycle)
                cycle = self.submit(side)
                self.update(cycle, price=extreme, now=102.0)
                self.update(cycle, price=110.0, now=120.0)
                self.assertEqual(list(self.graph._history), [(120.0, 110.0)])
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)

    def test_existing_order_at_startup_has_explicit_unavailable_estimate(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                cycle = self.submit(side)
                self.update(cycle, price=90.0 if side == "buy" else 130.0, now=102.0)
                self.assertNotIn(f"Estimated current {side.upper()} stop", self.levels())
                self.assertIn(f"{side.upper()} initial stop", self.levels())
                self.assertIn("estimate unavailable", self.graph._trail_estimate_note)

    def test_restart_notice_and_confirmed_title_are_drawn(self):
        self.submit("sell")
        painter = Dummy()
        rendered = []
        painter.drawText = lambda *args: rendered.append(str(args[-1]))
        with patch.object(self.gui, "QPainter", return_value=painter):
            self.graph.paintEvent(Dummy())
        self.assertTrue(any("stop estimate unavailable" in text for text in rendered))
        self.assertIn("Market and strategy graph - OLD", rendered)

    def test_new_order_reference_resets_old_extremum(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        self.update(cycle, price=150.0, now=102.0)
        replacement = dict(cycle, sell_order_ref="cycle-1:sell:2", sell_initial_trail_stop_price=108.9)
        self.update(replacement, price=110.0, now=103.0)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 108.9)

    def test_rearming_same_cycle_reference_does_not_reuse_previous_order_extremum(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        self.update(cycle, price=150.0, now=102.0)
        self.update(self.cycle, price=110.0, now=103.0)
        self.submit("sell", now=104.0)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 108.9)

    def test_cycle_change_does_not_inherit_order_observation_evidence(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        self.update(cycle, price=150.0, now=102.0)
        self.update(dict(cycle, id="cycle-2"), price=110.0, now=103.0)
        self.assertNotIn("Estimated current SELL stop", self.levels())
        self.assertIn("estimate unavailable", self.graph._trail_estimate_note)

    def test_cached_quote_and_missing_receipt_time_cannot_advance_estimate(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        self.update(cycle, price=150.0, now=102.0, received_at=100.0)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 108.9)
        self.update(cycle, price=150.0, now=103.0, receipt=False)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 108.9)

    def test_changed_prices_with_same_post_submission_receipt_second_are_counted(self):
        for side, first, second, expected in (("buy", 100.0, 90.0, 90.9), ("sell", 120.0, 130.0, 128.7)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = self.submit(side)
                self.update(cycle, price=first, now=102.1, received_at=102.0)
                self.update(cycle, price=second, now=102.6, received_at=102.0)
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)

    def test_ambiguous_price_change_in_submission_second_makes_estimate_unavailable(self):
        self.update(self.cycle)
        cycle = self.submit("sell", now=101.1)
        self.update(cycle, price=150.0, now=101.6, received_at=101.0)
        self.assertNotIn("Estimated current SELL stop", self.levels())
        self.assertIn("quote timing around submission is ambiguous", self.graph._trail_estimate_note)
        self.assertIsNone(self.graph._trail_states["sell"]["extreme"])

    def test_first_submission_second_sequence_advance_proves_new_quote(self):
        for side, price, expected in (("buy", 90.0, 90.9), ("sell", 130.0, 128.7)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = dict(self.cycle, **{
                    "stage": Stage.BUY_TRAIL_ACTIVE.value if side == "buy" else Stage.SELL_TRAIL_ACTIVE.value,
                    f"{side}_order_ref": f"cycle-1:{side}:1",
                    f"{side}_initial_trail_stop_price": 111.1 if side == "buy" else 108.9,
                })
                self.update(cycle, price=110.0, now=101.1, received_at=101.0, sequence=10)
                self.update(cycle, price=price, now=101.6, received_at=101.0, sequence=11)
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)

    def test_unchanged_or_different_subscription_sequence_does_not_prove_quote_order(self):
        for sequence, subscription in ((10, "live-1"), (11, "live-2")):
            with self.subTest(sequence=sequence, subscription=subscription):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = dict(self.cycle, stage=Stage.SELL_TRAIL_ACTIVE.value,
                             sell_order_ref="SELL", sell_initial_trail_stop_price=108.9)
                self.update(cycle, price=110.0, now=101.1, received_at=101.0, sequence=10)
                self.update(cycle, price=150.0, now=101.6, received_at=101.0, sequence=sequence, subscription=subscription)
                self.assertNotIn("Estimated current SELL stop", self.levels())
                self.assertIn("estimate unavailable", self.graph._trail_estimate_note)

    def test_reused_reference_with_new_broker_order_id_resets_extremum(self):
        for side, extreme, expected in (("buy", 90.0, 111.1), ("sell", 150.0, 108.9)):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = self.submit(side)
                cycle[f"{side}_order_id"] = 100
                self.update(cycle, price=extreme, now=102.0)
                replacement = dict(cycle, **{f"{side}_order_id": 101})
                self.update(replacement, price=110.0, now=103.0)
                self.assertAlmostEqual(self.levels()[f"Estimated current {side.upper()} stop"], expected)
                self.assertIsNone(self.graph._trail_states[side]["extreme"])

    def test_order_id_zero_is_a_known_broker_identity(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        cycle["sell_order_id"] = 0
        self.update(cycle, price=150.0, now=102.0)
        cycle["sell_order_id"] = 1
        self.update(cycle, price=110.0, now=103.0)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 108.9)

    def test_learning_missing_reference_does_not_make_restart_estimate_available(self):
        cycle = dict(self.cycle, stage=Stage.SELL_TRAIL_ACTIVE.value,
                     sell_order_id=100, sell_initial_trail_stop_price=108.9)
        self.update(cycle, price=110.0, now=101.0)
        cycle["sell_order_ref"] = "cycle-1:SELL"
        self.update(cycle, price=150.0, now=102.0)
        self.assertNotIn("Estimated current SELL stop", self.levels())
        self.assertIn("estimate unavailable", self.graph._trail_estimate_note)

    def test_learning_broker_id_preserves_known_order_extremum(self):
        self.update(self.cycle)
        cycle = self.submit("sell")
        self.update(cycle, price=150.0, now=102.0)
        cycle["sell_order_id"] = 100
        self.update(cycle, price=110.0, now=103.0)
        self.assertAlmostEqual(self.levels()["Estimated current SELL stop"], 148.5)
        self.assertEqual(self.graph._trail_states["sell"]["extreme"], 150.0)

    def test_learning_broker_id_does_not_make_restart_estimate_available(self):
        cycle = self.submit("sell")
        cycle["sell_order_id"] = 100
        self.update(cycle, price=150.0, now=102.0)
        self.assertNotIn("Estimated current SELL stop", self.levels())
        self.assertIn("estimate unavailable", self.graph._trail_estimate_note)

    def test_market_orders_do_not_reuse_previous_trail_estimate(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.graph = self.gui.StrategyGraphWidget()
                self.update(self.cycle)
                cycle = self.submit(side)
                cycle[f"{side}_order_ref"] = f"cycle-1:{side.upper()}_MARKET"
                self.update(cycle, now=102.0)
                self.assertNotIn(f"Estimated current {side.upper()} stop", self.levels())
                self.assertFalse(self.graph._trail_estimate_note)


if __name__ == "__main__":
    unittest.main()
