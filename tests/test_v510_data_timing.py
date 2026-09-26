"""Quote consumption, monotonic freshness and final BUY preflight regressions."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_v500_broker_safety import _setup


class DataTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v510_data_timing_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.broker = DeterministicBrokerAdapter()
        self.settings = permissive_strategy()
        self.settings.contract_con_id = 123
        self.settings.stale_data_guard_enabled = True
        self.settings.max_selected_price_age_seconds = 2
        self.settings.max_bid_ask_age_seconds = 2
        self.c = make_controller(self.module, Path(folder.name) / "state.sqlite", self.broker, self.settings)
        self.c.connection.account = "SIM"
        self.addCleanup(self.c._market_capture.shutdown)

    def stream(self, *, stage3=False):
        adapter, _broker, contract = _setup()
        adapter._upstream_connected = True
        adapter._market_data_type = 1
        adapter._active_market_data_type = 1
        ticker = adapter._request_ticker(contract, "")
        self.c.adapter = adapter
        self.c.contract = contract
        self.settings.ticker = "TEST"
        cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100, 0)
        if stage3:
            cycle.stage = Stage.WAIT_RISE_TRIGGER
            cycle.avg_buy_price = 90
            cycle.buy_filled_qty = cycle.quantity = 10
            cycle.buy_status = "Filled"
            cycle.rise_trigger_price = StrategyEngine.recalculate_rise_trigger_price(cycle)
        self.c.active_cycle = cycle
        self.c.storage.upsert_cycle(cycle)
        self.c._update_rth_status = lambda _contract: {"is_open": True}
        self.actions = []
        self.c._execute_actions = lambda actions, _cycle: self.actions.extend(actions)
        return adapter, ticker

    @staticmethod
    def quote(adapter, ticker, price=97.0):
        ticker.bid, ticker.ask, ticker.last = price - 0.1, price + 0.1, price
        ticker.ticks = [SimpleNamespace(tickType=n) for n in (1, 2, 4)]
        adapter._on_pending_tickers([ticker])

    @staticmethod
    def size(adapter, ticker):
        ticker.bidSize = getattr(ticker, "bidSize", 100) + 1
        ticker.ticks = [SimpleNamespace(tickType=0, size=ticker.bidSize)]
        adapter._on_pending_tickers([ticker])

    def test_quote_then_positive_size_still_advances_stage1_once(self):
        adapter, ticker = self.stream()
        self.quote(adapter, ticker)
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertEqual([a.action_type for a in self.actions], ["PLACE_BUY_TRAIL"])
        self.assertEqual(len(self.c._price_history), 1)
        self.assertEqual(self.c.price_snapshot["fields_updated_in_event"], [])
        self.assertEqual(set(self.c.price_snapshot["fields_updated_since_latest_read"]), {"bid", "ask", "last"})
        self.c._record_price_snapshot(adapter._snapshot_from_ticker(ticker, self.c.contract), self.c.contract)
        self.assertFalse(self.c.price_snapshot["strategy_price_usable"])
        self.assertEqual(len(self.c._price_history), 1)

    def test_two_distinct_quotes_survive_following_size_packets_for_stage3(self):
        adapter, ticker = self.stream(stage3=True)
        for index in range(2):
            self.quote(adapter, ticker, 97.0 + index * 0.01)
            self.size(adapter, ticker)
            self.c._run_strategy_cycle(price_timeout=0)
            self.assertEqual(len(self.actions), index)
        self.assertEqual(self.c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertEqual(self.actions[0].action_type, "PLACE_SELL_TRAIL")

    def test_cached_read_does_not_supply_second_stage3_confirmation(self):
        adapter, ticker = self.stream(stage3=True)
        self.quote(adapter, ticker)
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(len(self.c._price_history), 1)

    def test_size_only_cannot_supply_price_or_second_confirmation(self):
        adapter, ticker = self.stream(stage3=True)
        self.quote(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(self.actions, [])
        self.assertFalse(self.c.price_snapshot["strategy_price_usable"])
        self.assertEqual(len(self.c._price_history), 1)

    def test_invalidated_book_still_blocks_stage3_after_quote_then_size(self):
        adapter, ticker = self.stream(stage3=True)
        self.quote(adapter, ticker)
        ticker.bid = float("nan")
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.c._stage3_sell_confirmation, {})

    def test_new_subscription_requires_its_own_price_evidence(self):
        adapter, ticker = self.stream(stage3=True)
        self.quote(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        adapter._forget_market_data_subscriptions()
        ticker = adapter._request_ticker(self.c.contract, "")
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertFalse(self.c.price_snapshot["strategy_price_usable"])
        self.quote(adapter, ticker)
        self.size(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(self.actions, [])
        self.quote(adapter, ticker)
        self.c._run_strategy_cycle(price_timeout=0)
        self.assertEqual(len(self.actions), 1)

    def test_atr_sample_keeps_price_receipt_time_before_size_packet(self):
        adapter, ticker = self.stream()
        with patch.object(self.module.time, "monotonic", return_value=100.0):
            self.quote(adapter, ticker)
        with patch.object(self.module.time, "monotonic", return_value=100.05):
            self.size(adapter, ticker)
            self.c._run_strategy_cycle(price_timeout=0)
        self.assertAlmostEqual(self.c._price_history[-1][0], 100.0)

    def test_monotonic_age_ignores_rounded_or_adjusted_wall_clock(self):
        self.settings.max_selected_price_age_seconds = 0.5
        self.settings.max_bid_ask_age_seconds = 0.5
        cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100, 0)
        self.c.active_cycle = cycle
        with patch.object(self.module.time, "monotonic", return_value=100.0):
            publish_fresh_price(self.c, self.broker, 100)
        initial = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)
        self.c.price_snapshot["field_update_received_at"] = {
            field: initial.isoformat() for field in ("bid", "ask", "last")
        }
        self.c._rth_status_age_seconds = lambda: 0.0
        for wall_offset in (0.99, -60, 60):
            wall_clock = SimpleNamespace(
                now=lambda _zone, offset=wall_offset: initial + timedelta(seconds=offset),
                fromisoformat=datetime.fromisoformat,
            )
            with self.subTest(wall_offset=wall_offset), patch.object(self.module, "datetime", wall_clock):
                with patch.object(self.module.time, "monotonic", return_value=100.02):
                    self.assertAlmostEqual(self.c._snapshot_field_age_now(self.c.price_snapshot, "bid"), 0.02)
                    self.assertIsNone(self.c._stale_data_guard_message_for_buy(cycle))
                with patch.object(self.module.time, "monotonic", return_value=110.0):
                    self.assertAlmostEqual(self.c._snapshot_field_age_now(self.c.price_snapshot, "bid"), 10.0)
                    self.assertIsNotNone(self.c._stale_data_guard_message_for_buy(cycle))

    def test_bad_monotonic_age_evidence_fails_closed(self):
        for observed, age in ((101, 0), (100, None), (100, -1), (float("nan"), 0), (100, float("inf"))):
            with self.subTest(observed=observed, age=age), patch.object(self.module.time, "monotonic", return_value=100):
                self.assertIsNone(self.c._observed_age_now({"observation_monotonic": observed}, age))

    def test_precise_adapter_age_is_retained_before_controller_read(self):
        adapter, ticker = self.stream()
        with patch.object(self.module.time, "monotonic", return_value=100):
            self.quote(adapter, ticker)
        with patch.object(self.module.time, "monotonic", return_value=101):
            snapshot = adapter._snapshot_from_ticker(ticker, self.c.contract)
        with patch.object(self.module.time, "monotonic", return_value=104):
            self.c._record_price_snapshot(snapshot, self.c.contract)
            self.assertAlmostEqual(self.c._snapshot_field_age_now(self.c.price_snapshot, "bid"), 4)

    def final_buy(self, order_type, scenario):
        self.settings.buy_rebound_trail_pct = 0 if order_type == "MKT" else 1
        self.settings.session_timing_guard_enabled = scenario == "entry_window"
        self.settings.no_new_buy_last_minutes = 5
        clock = [100.0]
        with patch.object(self.module.time, "monotonic", side_effect=lambda: clock[0]):
            publish_fresh_price(self.c, self.broker, 97)
            cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100, 0)
            cycle, actions = StrategyEngine.on_price_update(cycle, 97, is_rth=True)
            self.c.active_cycle = cycle
            self.c.storage.upsert_cycle(cycle)
            ref = actions[0].payload["order_ref"]
            self.c._session_minutes_from_rth_status = lambda: {
                "available": True, "minutes_since_open": 100,
                "minutes_to_close": 10 if clock[0] == 100 else 4,
            }
            def delay(_reason):
                if scenario in {"backup_age", "entry_window"}:
                    clock[0] += 4 if scenario == "backup_age" else 0.1
                if scenario == "rth_close":
                    self.broker.rth_open = False
            self.c.storage.backup_database = delay
            record = self.c._record_order_intent
            def record_then_delay(*args, **kwargs):
                record(*args, **kwargs)
                if scenario == "intent_age":
                    clock[0] += 4
            self.c._record_order_intent = record_then_delay
            self.c._execute_actions(actions, cycle)
            order = self.c.storage.get_order_for_cycle_ref(cycle.id, ref)
        return order

    def test_final_buy_rechecks_after_backup_for_both_order_types(self):
        # Separate test instances below exercise the same persisted paths for
        # trailing and market orders; no preflight guard is mocked away.
        order = self.final_buy("TRAIL", "backup_age")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(order["status"], "SUBMIT_FAILED")
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_INITIAL_DROP)

    def test_market_buy_rechecks_after_backup(self):
        order = self.final_buy("MKT", "backup_age")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(order["status"], "SUBMIT_FAILED")

    def test_market_buy_rechecks_after_intent_persistence(self):
        order = self.final_buy("MKT", "intent_age")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(order["status"], "SUBMIT_FAILED")

    def test_trailing_buy_rechecks_after_intent_persistence(self):
        order = self.final_buy("TRAIL", "intent_age")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(order["status"], "SUBMIT_FAILED")

    def test_final_buy_checks_rth_after_backup(self):
        self.final_buy("MKT", "rth_close")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertIn("RTH guard", self.c.active_cycle.error_message)

    def test_final_buy_checks_entry_window_after_backup(self):
        self.final_buy("TRAIL", "entry_window")
        self.assertEqual(self.broker.placed_orders, [])
        self.assertIn("Session timing guard", self.c.active_cycle.error_message)

    def test_fresh_market_buy_still_submits(self):
        self.final_buy("MKT", "fresh")
        self.assertEqual(len(self.broker.placed_orders), 1)

    def test_fresh_trailing_buy_still_submits(self):
        self.final_buy("TRAIL", "fresh")
        self.assertEqual(len(self.broker.placed_orders), 1)

    def test_disabled_stale_guard_preserves_configuration(self):
        self.settings.stale_data_guard_enabled = False
        self.final_buy("MKT", "backup_age")
        self.assertEqual(len(self.broker.placed_orders), 1)


if __name__ == "__main__":
    unittest.main()
