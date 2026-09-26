"""Stable completed-cycle risk dates and ATR edit-readiness regressions."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.atr_memory import AtrSessionMemory, atr_seed_identity
from app.ib_adapter import IbAsyncTwsAdapter
from app.models import ConnectionSettings, CycleState, Stage
from app.storage import BotStorage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.controller_harness import permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter

YESTERDAY = "2026-09-24T15:00:00+00:00"
TODAY = "2026-09-25T15:00:00+00:00"
TOMORROW = "2026-09-26T15:00:00+00:00"


class StrategyRiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v510_strategy_risk", path)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.storage = BotStorage(self.root / "state.sqlite")
        with patch.object(self.module, "debug_captures_dir", return_value=self.root / "capture"):
            self.c = self.module.TradingController(self.storage)
        self.addCleanup(self.c._market_capture.shutdown)
        self.broker = DeterministicBrokerAdapter()
        self.broker.accounts = ["SIM"]
        self.c.adapter = self.broker
        self.c.emit_snapshot = lambda **kwargs: None
        self.settings = replace(
            permissive_strategy(), contract_con_id=123, primary_exchange="NASDAQ",
        )
        self.c.strategy = self.settings
        self.c._connect(ConnectionSettings(account="SIM", market_data_type=1))
        self.c.contract = self.broker.contract

    def completion_stamp(self, cycle):
        with self.storage.connect() as con:
            row = con.execute("SELECT completed_at FROM cycles WHERE id=?", (cycle.id,)).fetchone()
        return row["completed_at"]

    def completed(self, number, stamp, pnl):
        cycle = CycleState.new(self.settings, number, "SIM", 100, 0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.quantity = cycle.buy_filled_qty = cycle.sell_filled_qty = 10
        cycle.avg_buy_price = 100
        cycle.avg_sell_price = 100 + pnl / 10
        cycle.gross_pnl = cycle.net_pnl = pnl
        cycle.buy_status = cycle.sell_status = "Filled"
        cycle.buy_order_ref = make_order_ref(cycle.ticker, number, cycle.id, "BUY_TRAIL")
        cycle.sell_order_ref = make_order_ref(cycle.ticker, number, cycle.id, "SELL_TRAIL")
        cycle.buy_order_id, cycle.buy_perm_id = number * 10 + 1, number * 100 + 1
        cycle.sell_order_id, cycle.sell_perm_id = number * 10 + 2, number * 100 + 2
        cycle.buy_filled_at = cycle.sell_filled_at = cycle.updated_at = stamp
        self.storage.upsert_cycle(cycle)
        return cycle

    def test_late_commission_updates_amount_without_moving_day_or_removing_loss_caps(self):
        old = self.completed(1, YESTERDAY, 200)
        self.completed(2, TODAY, -100)
        for side, price, ref, order_id, perm_id, execution_id in (
            ("BUY", 100, old.buy_order_ref, old.buy_order_id, old.buy_perm_id, "known-buy"),
            ("SELL", 120, old.sell_order_ref, old.sell_order_id, old.sell_perm_id, "known-sell"),
        ):
            self.storage.upsert_execution(
                cycle=old, ticker=old.ticker, side=side, shares=10, price=price,
                avg_price=price, commission=0, currency="USD", order_ref=ref,
                order_id=order_id, perm_id=perm_id, execution_id=execution_id,
                executed_at=YESTERDAY, raw={},
            )
        active = CycleState.new(self.settings, 3, "SIM", 100, 0)
        active.hard_risk_limits_enabled = True
        active.max_daily_loss_ticker = active.max_daily_loss_total = 50
        self.storage.upsert_cycle(active)
        self.c.active_cycle = active

        def blockers():
            with patch("app.storage.utc_now_iso", return_value=TODAY):
                return [item["code"] for item in self.c._risk_guard_blockers_for_buy(active)]

        self.assertEqual(blockers(), ["daily_loss_ticker", "daily_loss_total"])
        execution = SimpleNamespace(
            execId="known-sell", orderRef=old.sell_order_ref, orderId=old.sell_order_id,
            permId=old.sell_perm_id, acctNumber="SIM", side="SLD", shares=10,
            price=120, time=datetime.fromisoformat(YESTERDAY),
        )
        contract = SimpleNamespace(symbol=old.ticker, conId=123, currency="USD")
        trade = SimpleNamespace(
            order=SimpleNamespace(
                orderRef=old.sell_order_ref, orderId=old.sell_order_id,
                permId=old.sell_perm_id, account="SIM", action="SELL",
            ),
            orderStatus=SimpleNamespace(
                status="Filled", filled=10, remaining=0, avgFillPrice=120, permId=old.sell_perm_id,
            ),
            contract=contract,
        )
        fill = SimpleNamespace(execution=execution, contract=contract, time=execution.time)
        formatter = IbAsyncTwsAdapter()
        formatter._record_broker_event(
            "COMMISSION_REPORT", trade, fill,
            SimpleNamespace(execId="known-sell", commission=0.35, currency="USD"),
        )
        self.broker.events.extend(formatter.drain_broker_events())
        with patch("app.models.utc_now_iso", return_value=TODAY):
            self.c._drain_broker_events()
        self.assertAlmostEqual(self.storage.get_daily_net_pnl_total("2026-09-24"), 199.65)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-25"), -100)
        self.assertEqual(self.storage.get_daily_net_pnl_for_ticker("AAPL", "2026-09-25", con_id=123), -100)
        self.assertEqual(blockers(), ["daily_loss_ticker", "daily_loss_total"])
        self.assertEqual(self.completion_stamp(old), YESTERDAY)
        self.assertEqual(self.storage.get_cycle(old.id).updated_at, TODAY)
        self.assertEqual(self.c.active_cycle.id, active.id)

    def test_first_partial_sell_day_is_not_final_completion_day(self):
        cycle = CycleState.new(self.settings, 1, "SIM", 100, 0)
        cycle.stage = Stage.SELL_TRAIL_ACTIVE
        cycle.buy_filled_qty, cycle.avg_buy_price = 10, 100
        cycle.sell_filled_qty, cycle.avg_sell_price = 4, 105
        cycle.sell_filled_at = cycle.updated_at = YESTERDAY
        self.storage.upsert_cycle(cycle)
        self.assertIsNone(self.completion_stamp(cycle))
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-24"), 0)
        with patch("app.models.utc_now_iso", return_value=TODAY):
            completed = StrategyEngine.on_sell_fill(cycle, 10, 105, "Filled", 1)
        self.storage.upsert_cycle(completed)
        self.assertEqual(completed.sell_filled_at, YESTERDAY)
        self.assertEqual(self.completion_stamp(completed), TODAY)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-24"), 0)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-25"), 49)
        self.assertEqual(self.storage.get_completed_cycle_count_today("AAPL", "2026-09-25"), 1)

    def test_protective_completion_uses_final_transition_date(self):
        cycle = CycleState.new(self.settings, 1, "SIM", 100, 0)
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.buy_filled_qty, cycle.avg_buy_price = 10, 100
        cycle.protective_sell_filled_at = YESTERDAY
        self.storage.upsert_cycle(cycle)
        with patch("app.models.utc_now_iso", return_value=TODAY):
            completed = StrategyEngine.on_protective_sell_fill(cycle, 10, 95, "Filled", 1)
        self.storage.upsert_cycle(completed)
        self.assertEqual(self.completion_stamp(completed), TODAY)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-25"), -51)

    def test_old_cycle_object_cannot_overwrite_completion_date(self):
        cycle = self.completed(1, YESTERDAY, 200)
        cycle.updated_at = TOMORROW
        cycle.net_pnl = 198
        self.storage.upsert_cycle(cycle)
        self.assertEqual(self.completion_stamp(cycle), YESTERDAY)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-24"), 198)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-26"), 0)

    def test_late_update_cannot_reorder_consecutive_loss_guard(self):
        profit = self.completed(1, YESTERDAY, 200)
        self.completed(2, TODAY, -100)
        profit.updated_at = TOMORROW
        self.storage.upsert_cycle(profit)
        self.assertEqual(self.storage.get_consecutive_loss_count("AAPL", con_id=123), 1)
        active = CycleState.new(self.settings, 3, "SIM", 100, 0)
        active.hard_risk_limits_enabled = True
        active.max_consecutive_losses = 1
        self.assertEqual(
            [item["code"] for item in self.c._risk_guard_blockers_for_buy(active)], ["loss_streak"],
        )

    def test_legacy_schema_backfill_is_stable_and_does_not_guess_partial_fill_date(self):
        cycle = self.completed(1, TODAY, -100)
        cycle.sell_filled_at = YESTERDAY
        self.storage.upsert_cycle(cycle)
        with self.storage.connect() as con:
            con.execute("ALTER TABLE cycles DROP COLUMN completed_at")
        self.storage = BotStorage(self.storage.db_path)
        self.assertEqual(self.completion_stamp(cycle), TODAY)
        restored = self.storage.get_cycle(cycle.id)
        self.assertIsInstance(restored, CycleState)
        self.assertEqual(restored.sell_filled_at, YESTERDAY)
        restored.updated_at = TOMORROW
        self.storage.upsert_cycle(restored)
        self.storage = BotStorage(self.storage.db_path)
        self.assertEqual(self.completion_stamp(cycle), TODAY)
        self.assertEqual(self.storage.get_daily_net_pnl_total("2026-09-25"), -100)
        self.assertEqual(self.storage.get_completed_cycle_count_today("AAPL", "2026-09-26"), 0)

    def ready_atr_cycle(self):
        settings = replace(
            self.settings, atr_adaptive_enabled=True, atr_block_new_buy_until_ready=True,
            atr_period=3, atr_bar_seconds=60, initial_drop_pct=5,
            buy_rebound_trail_pct=0, what_if_check_enabled=True,
        )
        self.c.strategy = settings
        cycle = CycleState.new(settings, 1, "SIM", 100, 0)
        cycle.last_price = 94
        self.c.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        self.c.price_snapshot = self.broker.publish_price(94).to_dict()
        self.c.price_snapshot.update(
            strategy_price_usable=True, atr_ready=True, atr_bars_available=4,
            atr={"ready": True, "period": 3, "bar_seconds": 60,
                 "bars_available": 4, "bars_required": 4, "atr_pct": 1},
        )
        self.c._api_last_data_monotonic = time.monotonic()
        self.c._api_data_invalidated = False
        self.broker.what_if_market_order = lambda **kwargs: {"ok": False, "message": "temporary what-if failure"}
        advanced, actions = self.c._advance_waiting_cycle_from_price(cycle, 94, is_rth=True, rth_message="open")
        self.c._commit_waiting_transition(advanced, actions)
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertIn("What-if", self.c.active_cycle.error_message)
        self.broker.what_if_market_order = lambda **kwargs: {"ok": True}
        return settings

    def assert_calculation_edit_blocks(self, **changes):
        settings = self.ready_atr_cycle()
        self.c._handle_command("SAVE_DRAFT_SETTINGS", {
            "connection": self.c.connection, "strategy": replace(settings, **changes),
        })
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.c.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertIsNone(self.c.active_cycle.drop_trigger_price)
        self.assertFalse(self.c.price_snapshot["atr_ready"])
        self.assertIn("ATR", self.c.active_cycle.error_message)

    def test_period_edit_cannot_reuse_old_ready_atr(self):
        self.assert_calculation_edit_blocks(atr_period=14)

    def test_bar_size_edit_cannot_reuse_old_ready_atr(self):
        self.assert_calculation_edit_blocks(atr_bar_seconds=300)

    def test_same_calculation_edit_still_allows_ready_entry(self):
        settings = self.ready_atr_cycle()
        self.c._handle_command("SAVE_DRAFT_SETTINGS", {
            "connection": self.c.connection, "strategy": replace(settings, investment_amount=9000),
        })
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)

    def test_submission_guard_rejects_mismatched_or_missing_atr_identity(self):
        self.ready_atr_cycle()
        for changes in ({"period": 14}, {"bar_seconds": 300}, {"period": None}):
            with self.subTest(changes=changes):
                atr = {"ready": True, "period": 3, "bar_seconds": 60, **changes}
                self.c.price_snapshot.update(atr_ready=True, atr=atr)
                self.assertIsNotNone(self.c._atr_warmup_guard_blocker_for_buy(self.c.active_cycle))

    def test_matching_saved_seed_remains_ready_without_current_session_warmup(self):
        settings = self.ready_atr_cycle()
        identity = atr_seed_identity(self.broker.contract, settings, "live|1")
        memory = AtrSessionMemory(self.storage)

        def rth(stamp):
            now = datetime.fromisoformat(stamp)
            return {"is_open": True, "checked_at": stamp,
                    "session_open": (now - timedelta(hours=1)).isoformat(),
                    "session_close": (now + timedelta(hours=1)).isoformat()}

        memory.prepare(identity, rth(YESTERDAY), YESTERDAY)
        memory.note_observation(live=True)
        memory.apply({
            "ready": True, "period": 3, "bar_seconds": 60, "bars_available": 4,
            "bars_required": 4, "atr": 1, "atr_pct": 1, "latest_close": 100,
            "latest_bar_high": 101, "latest_bar_low": 99, "true_ranges_used": 3,
        })
        memory.flush()
        restored = AtrSessionMemory(self.storage)
        restored.prepare(identity, rth(TODAY), TODAY)
        seeded = restored.apply({
            "ready": False, "period": 3, "bar_seconds": 60,
            "bars_available": 1, "bars_required": 4,
        })
        self.assertTrue(seeded["seeded"])
        self.c.price_snapshot.update(atr_ready=True, atr=seeded)
        self.assertIsNone(self.c._atr_warmup_guard_blocker_for_buy(self.c.active_cycle))


if __name__ == "__main__":
    unittest.main()
