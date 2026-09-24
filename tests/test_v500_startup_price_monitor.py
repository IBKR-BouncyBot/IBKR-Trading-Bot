"""Restored-cycle quotes stay visible before Start without resuming trading."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ib_adapter import BrokerAdapterError, MarketPriceSnapshot, QualifiedContract, RthStatus
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage


class QuoteBroker:
    requires_exact_contract_selection = True

    def __init__(self):
        self.connected = False
        self.qualifications = []
        self.reads = []
        self.resolved_id = 111
        self.qualification_error = None
        self.price_error = None
        self.upstream_connected = True

    def is_connected(self):
        return self.connected

    def connect(self, *args):
        self.connected = True

    def managed_accounts(self):
        return ["TEST_ACCOUNT"]

    def connectivity_status(self):
        return {
            "local_connected": self.connected,
            "upstream_connected": self.upstream_connected,
            "trading_ready": self.connected and self.upstream_connected,
        }

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        self.qualifications.append((ticker, exchange, currency, primary_exchange, con_id))
        if self.qualification_error is not None:
            raise self.qualification_error
        return QualifiedContract(ticker, self.resolved_id, SimpleNamespace(), currency=currency)

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def set_market_data_type(self, data_type):
        pass

    def price_snapshot(self, contract, timeout):
        self.reads.append((contract.con_id, timeout))
        if self.price_error is not None:
            raise self.price_error
        return MarketPriceSnapshot(
            price=100.0, source="test", timestamp=utc_now_iso(),
            requested_market_data_type=1, subscription_market_data_type=1,
            fields={"last": 100.0, "bid": 99.9, "ask": 100.1},
        )


class StartupPriceMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._startup_price_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.module = module

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.storage = BotStorage(self.root / "state.sqlite")
        self.connection = ConnectionSettings(account="TEST_ACCOUNT")
        self.settings = StrategySettings(
            ticker="TEST", contract_con_id=111, atr_adaptive_enabled=False,
        )
        self.storage.save_connection_settings(self.connection)
        self.storage.save_strategy_settings(self.settings)

    def restored_worker(self, stage, *, con_id=111):
        cycle = CycleState.new(self.settings, 1, "TEST_ACCOUNT", 100, 0)
        cycle.stage = stage
        cycle.con_id = con_id
        self.storage.upsert_cycle(cycle)
        with patch.object(self.module, "debug_captures_dir", return_value=self.root / "capture"):
            worker = self.module.TradingController(self.storage)
        self.addCleanup(worker._market_capture.shutdown)
        worker.adapter = QuoteBroker()
        worker.emit_snapshot = lambda **kwargs: None
        worker._recover_after_connect = Mock(side_effect=AssertionError("unexpected recovery"))
        worker._execute_actions = Mock(side_effect=AssertionError("unexpected trading"))
        return worker, cycle

    def assert_paused(self, worker, cycle):
        self.assertTrue(worker._startup_resume_required)
        self.assertEqual(worker.active_cycle.id, cycle.id)
        self.assertEqual(self.storage.get_cycle(cycle.id).to_dict(), cycle.to_dict())
        worker._recover_after_connect.assert_not_called()
        worker._execute_actions.assert_not_called()

    def test_cold_start_reads_stored_contract_before_start(self):
        for stage in (
            Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE,
        ):
            with self.subTest(stage=stage):
                # One synthetic unresolved cycle per isolated database.
                self.storage = BotStorage(self.root / f"{stage.name}.sqlite")
                self.storage.save_connection_settings(self.connection)
                self.storage.save_strategy_settings(self.settings)
                worker, cycle = self.restored_worker(stage)
                self.assertTrue(worker._connect(self.connection))
                self.assertEqual((worker.price_snapshot or {}).get("price"), 100.0)
                self.assertEqual(worker.adapter.qualifications, [("TEST", "SMART", "USD", "", 111)])
                self.assertEqual(worker.adapter.reads, [(111, 0.0)])
                worker._run_strategy_cycle()
                self.assertEqual((worker.price_snapshot or {}).get("price"), 100.0)
                self.assertEqual(len(worker.adapter.qualifications), 1)
                self.assert_paused(worker, cycle)

    def test_auto_reconnect_loads_stored_contract_without_resuming(self):
        worker, cycle = self.restored_worker(Stage.WAIT_RISE_TRIGGER)
        worker._auto_reconnect_enabled = True
        self.assertTrue(worker._attempt_reconnect_if_due())
        self.assertEqual((worker.price_snapshot or {}).get("price"), 100.0)
        self.assert_paused(worker, cycle)

    def test_wrong_broker_contract_is_not_subscribed(self):
        worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP)
        worker.adapter.resolved_id = 999
        self.assertTrue(worker._connect(self.connection))
        worker._run_strategy_cycle()
        self.assertIsNone(worker.contract)
        self.assertEqual(worker.adapter.reads, [])
        self.assertIn("conId", (worker.price_snapshot or {}).get("error", ""))
        self.assert_paused(worker, cycle)

    def test_missing_stored_exact_identity_does_not_guess_by_symbol(self):
        worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP, con_id=None)
        worker.contract = QualifiedContract("TEST", 999, SimpleNamespace(), currency="USD")
        self.assertTrue(worker._connect(self.connection))
        worker._run_strategy_cycle()
        self.assertEqual(worker.adapter.qualifications, [])
        self.assertEqual(worker.adapter.reads, [])
        self.assertIsNone(worker.contract)
        self.assert_paused(worker, cycle)

    def test_reconnect_discards_rejected_cached_contract(self):
        worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP)
        worker.contract = QualifiedContract("TEST", 999, SimpleNamespace(), currency="USD")
        worker.adapter.resolved_id = 999
        worker._auto_reconnect_enabled = True
        self.assertTrue(worker._attempt_reconnect_if_due())
        worker._run_strategy_cycle()
        worker._run_strategy_cycle()
        self.assertIsNone(worker.contract)
        self.assertEqual(len(worker.adapter.qualifications), 1)
        self.assertEqual(worker.adapter.reads, [])
        self.assert_paused(worker, cycle)

    def test_upstream_restoration_initializes_quotes_before_start(self):
        worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP)
        worker.adapter.upstream_connected = False
        self.assertFalse(worker._connect(self.connection))
        self.assertIsNone(worker.contract)
        self.assertEqual(worker.adapter.qualifications, [])
        worker.adapter.upstream_connected = True
        worker._handle_upstream_connectivity_restored({"upstream_connected": True})
        self.assertTrue(worker._recover_upstream_session_if_needed())
        self.assertEqual((worker.price_snapshot or {}).get("price"), 100.0)
        self.assertEqual(len(worker.adapter.qualifications), 1)
        self.assertFalse(worker._upstream_recovery_pending)
        self.assert_paused(worker, cycle)

    def test_qualification_error_preserves_connection_and_start_gate(self):
        worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP)
        worker.adapter.qualification_error = ValueError("test contract unavailable")
        self.assertTrue(worker._connect(self.connection))
        worker._run_strategy_cycle()
        self.assertTrue(worker.connected)
        self.assertEqual(worker.adapter.reads, [])
        self.assertIn("test contract unavailable", (worker.price_snapshot or {}).get("error", ""))
        worker._run_strategy_cycle()
        worker._run_strategy_cycle()
        self.assertEqual(len(worker.adapter.qualifications), 1)
        self.assert_paused(worker, cycle)

    def test_initial_quote_error_does_not_claim_disconnection(self):
        for reconnect in (False, True):
            with self.subTest(reconnect=reconnect):
                self.storage = BotStorage(self.root / f"quote_error_{reconnect}.sqlite")
                self.storage.save_connection_settings(self.connection)
                self.storage.save_strategy_settings(self.settings)
                worker, cycle = self.restored_worker(Stage.WAIT_INITIAL_DROP)
                worker.adapter.price_error = BrokerAdapterError("quote unavailable")
                if reconnect:
                    worker._auto_reconnect_enabled = True
                    self.assertTrue(worker._attempt_reconnect_if_due())
                else:
                    self.assertTrue(worker._connect(self.connection))
                self.assertTrue(worker.connected)
                self.assertEqual(len(worker.adapter.reads), 1)
                self.assertIn("quote unavailable", (worker.price_snapshot or {}).get("error", ""))
                self.assert_paused(worker, cycle)

    def test_ready_atr_before_start_does_not_advance_or_trade(self):
        self.settings.atr_adaptive_enabled = True
        for stage in (
            Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE,
        ):
            with self.subTest(stage=stage):
                self.storage = BotStorage(self.root / f"ready_atr_{stage.name}.sqlite")
                self.storage.save_connection_settings(self.connection)
                self.storage.save_strategy_settings(self.settings)
                worker, cycle = self.restored_worker(stage)
                worker._build_atr_snapshot = Mock(return_value={"ready": True, "atr_pct": 1.0})
                self.assertTrue(worker._connect(self.connection))
                self.assertEqual((worker.price_snapshot or {}).get("price"), 100.0)
                self.assertTrue((worker.price_snapshot or {}).get("atr_adaptive_applied"))
                self.assertTrue(worker._startup_resume_required)
                stored = self.storage.get_cycle(cycle.id)
                for field in (
                    "id", "stage", "anchor_price", "last_price", "buy_filled_qty", "sell_filled_qty",
                    "buy_order_ref", "sell_order_ref", "protective_sell_order_ref",
                ):
                    self.assertEqual(getattr(stored, field), getattr(cycle, field), field)
                worker._recover_after_connect.assert_not_called()
                worker._execute_actions.assert_not_called()

    def test_no_cycle_does_not_auto_select_strategy_ticker(self):
        with patch.object(self.module, "debug_captures_dir", return_value=self.root / "capture"):
            worker = self.module.TradingController(self.storage)
        self.addCleanup(worker._market_capture.shutdown)
        worker.adapter = QuoteBroker()
        worker.connected = worker.adapter.connected = True
        worker._broker_connectivity = {"upstream_connected": True}
        worker._run_strategy_cycle()
        self.assertEqual(worker.adapter.qualifications, [])
        self.assertEqual(worker.adapter.reads, [])


if __name__ == "__main__":
    unittest.main()
