"""Freshness display uses field ages; order execution still requires a new price event."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ib_adapter import MarketPriceSnapshot, QualifiedContract
from app.models import ConnectionSettings, CycleState, StrategySettings, utc_now_iso
from app.storage import BotStorage
from tests.test_v500_guard_identity import Broker


class FreshnessStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._freshness_status_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = BotStorage(Path(self.folder.name) / "state.sqlite")
        self.c = self.controller_type(self.storage)
        self.c.adapter = Broker()
        self.c.connected = True
        self.c.connection = ConnectionSettings(account="ACCOUNT_A")
        self.c.emit_snapshot = lambda **kwargs: None
        self.c.contract = QualifiedContract("AAA", 111, SimpleNamespace(), currency="USD")
        self.settings = StrategySettings(
            ticker="AAA", contract_con_id=111, investment_amount=1000,
            initial_drop_pct=2, atr_adaptive_enabled=False,
            atr_block_new_buy_until_ready=False, session_timing_guard_enabled=False,
            what_if_check_enabled=False, block_delayed_data_in_live=False,
            hard_risk_limits_enabled=False, max_spread_pct=0.5,
            max_selected_price_age_seconds=9, max_bid_ask_age_seconds=9,
        )
        self.c.strategy = self.settings
        self.cycle = CycleState.new(self.settings, 1, "ACCOUNT_A", 100, 0)
        self.c.active_cycle = self.cycle
        self.storage.upsert_cycle(self.cycle)
        self.c._latest_rth_status = {"is_open": True, "checked_at": utc_now_iso()}
        self.c._rth_status_age_seconds = lambda: 0.0
        self.c._adapter_connectivity_snapshot = lambda: {
            "local_connected": True, "upstream_connected": True,
        }
        self.c._order_submission_connectivity_message = lambda side: None

    def tearDown(self):
        self.c._market_capture.shutdown()
        self.folder.cleanup()

    @staticmethod
    def snapshot(*, sequence=10, basis_updated=True):
        return MarketPriceSnapshot(
            price=100.0, source="marketPrice", requested_market_data_type=1,
            subscription_market_data_type=1, timestamp=utc_now_iso(),
            fields={"last": 100.0, "bid": 99.9, "ask": 100.1},
            market_data_event_tracking=True, market_data_field_tracking=True,
            market_data_update_sequence=sequence,
            market_data_subscription_id="AAA|111|g1",
            market_data_update_age_seconds=0.1, api_data_received=True,
            upstream_connected=True, selected_price_basis="last",
            selected_price_basis_fields=["last"],
            selected_price_basis_update_sequence=10,
            selected_price_basis_updated_in_event=basis_updated,
            field_update_sequences={"last": 10, "bid": 9, "ask": 9},
            field_update_age_seconds={"last": 0.8, "bid": 0.7, "ask": 0.7},
            market_data_tick_types=[4] if basis_updated else [8],
        )

    def record_cached_read(self):
        snapshot = self.snapshot()
        self.c._record_price_snapshot(snapshot, self.c.contract)
        self.c._record_price_snapshot(snapshot, self.c.contract)
        self.assertFalse(self.c.price_snapshot["strategy_price_usable"])

    def stale_display_blockers(self):
        return [item for item in self.c._current_trading_blockers() if item["code"] == "stale_data"]

    def test_recent_consumed_price_is_not_displayed_as_stale(self):
        self.record_cached_read()
        self.assertEqual(self.stale_display_blockers(), [])
        self.assertEqual(self.c._api_data_seen_count, 1)
        self.assertEqual(len(self.c._price_history), 1)

    def test_volume_event_does_not_make_recent_selected_price_display_stale(self):
        self.c._record_price_snapshot(self.snapshot(), self.c.contract)
        self.c._record_price_snapshot(self.snapshot(sequence=11, basis_updated=False), self.c.contract)
        self.assertFalse(self.c.price_snapshot["strategy_price_usable"])
        self.assertEqual(self.stale_display_blockers(), [])
        self.assertEqual(len(self.c._price_history), 1)

    def test_consumed_price_still_cannot_authorize_submission_or_settings_evaluation(self):
        self.record_cached_read()
        self.assertIn("not usable", self.c._buy_submission_preflight_message(self.cycle, {}))
        self.cycle.last_price = 97.0
        with patch.object(self.c, "_advance_waiting_cycle_from_price") as advance:
            self.c._apply_active_strategy_edits(replace(self.settings, initial_drop_pct=1))
        advance.assert_not_called()
        self.assertEqual(self.c.adapter.orders, [])

    def test_new_selected_price_event_remains_usable(self):
        self.record_cached_read()
        snapshot = self.snapshot(sequence=11)
        snapshot.selected_price_basis_update_sequence = 11
        snapshot.field_update_sequences = {"last": 11, "bid": 9, "ask": 9}
        self.c._record_price_snapshot(snapshot, self.c.contract)
        self.assertTrue(self.c.price_snapshot["strategy_price_usable"])
        self.assertIsNone(self.c._stale_data_guard_message_for_buy(self.cycle))
        self.assertEqual(self.stale_display_blockers(), [])

    def test_display_still_rejects_expired_independent_fields(self):
        for field in ("last", "bid", "ask"):
            with self.subTest(field=field):
                self.record_cached_read()
                self.c.price_snapshot["field_update_age_seconds"][field] = 60.0
                blockers = self.stale_display_blockers()
                self.assertEqual(len(blockers), 1)
                self.assertNotIn("not usable", blockers[0]["message"])

    def test_display_still_rejects_unknown_upstream_or_invalidated_data(self):
        self.record_cached_read()
        self.c.price_snapshot["upstream_connected"] = False
        self.assertIn("upstream", self.stale_display_blockers()[0]["message"])
        self.c.price_snapshot["upstream_connected"] = True
        self.c._api_data_invalidated = True
        self.assertIn("invalidated", self.stale_display_blockers()[0]["message"])

    def test_display_still_rejects_missing_field_identity_and_crossed_book(self):
        self.record_cached_read()
        self.c.price_snapshot["field_update_sequences"]["last"] = 0
        self.assertIn("selected/API price age", self.stale_display_blockers()[0]["message"])
        self.c.price_snapshot["field_update_sequences"]["last"] = 10
        self.c.price_snapshot["fields"]["ask"] = 99.0
        self.assertIn("non-crossed", self.stale_display_blockers()[0]["message"])

    def test_display_still_rejects_expired_rth_status(self):
        self.record_cached_read()
        self.c._rth_status_age_seconds = lambda: 61.0
        self.assertIn("RTH status", self.stale_display_blockers()[0]["message"])


if __name__ == "__main__":
    unittest.main()
