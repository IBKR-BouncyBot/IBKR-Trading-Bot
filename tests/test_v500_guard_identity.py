"""Offline regression tests for exact cycle ownership and BUY quote evidence."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ib_adapter import OrderHandle, QualifiedContract, RthStatus
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyAction


class Broker:
    requires_exact_contract_selection = True

    def __init__(self):
        self.orders = []
        self.accounts = ["ACCOUNT_A"]
        self.polled = {}
        self.executions = []
        self.connect_calls = []

    def is_connected(self):
        return True

    def managed_accounts(self):
        return self.accounts

    def regular_trading_hours_status(self, contract):
        return RthStatus(True, "test", "open", utc_now_iso())

    def place_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return OrderHandle(kwargs["order_ref"], 10, 100, "Submitted", dict(kwargs))

    def place_trailing_stop(self, **kwargs):
        return self.place_market_order(**kwargs)

    def poll_order(self, ref):
        return self.polled.get(ref)

    def recent_executions(self):
        return self.executions

    def connect(self, *args):
        self.connect_calls.append(args)


class GuardIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load an isolated headless copy without replacing the normal GUI module
        # during pytest collection or changing another test's Qt configuration.
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v500_guard_test_controller", path)
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
        )
        self.c.strategy = self.settings
        self.cycle = CycleState.new(self.settings, 1, "ACCOUNT_A", 100, 0)
        self.c.active_cycle = self.cycle
        self.storage.upsert_cycle(self.cycle)
        self.c._api_last_data_monotonic = time.monotonic()
        self.c._api_data_invalidated = False
        self.c._rth_status_age_seconds = lambda: 0.0
        self.c.price_snapshot = self.quote()

    def tearDown(self):
        self.c._market_capture.shutdown()
        self.folder.cleanup()

    @staticmethod
    def quote(**changes):
        data = {
            "timestamp": utc_now_iso(), "price": 100.0, "strategy_price_usable": True,
            "market_data_field_tracking": True, "upstream_connected": True,
            "market_data_update_sequence": 4, "market_data_subscription_id": "AAA|111|g1",
            "subscription_market_data_type": 1, "selected_price_basis": "last",
            "selected_price_basis_fields": ["last"],
            "fields": {"last": 100.0, "bid": 99.9, "ask": 100.1},
            "field_update_sequences": {"last": 4, "bid": 3, "ask": 3},
            "field_update_age_seconds": {"last": 0.0, "bid": 0.0, "ask": 0.0},
        }
        data.update(changes)
        return data

    def sell_action(self):
        self.cycle.buy_filled_qty = 10
        self.cycle.avg_buy_price = 100
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.sell_order_ref = "IBKRBOT|AAA|CYCLE-000001|guard|SELL"
        self.storage.upsert_cycle(self.cycle)
        return StrategyAction("PLACE_SELL_MARKET", {
            "quantity": 10, "order_ref": self.cycle.sell_order_ref, "reference_price": 101.0,
        })

    def test_spread_blocks_with_master_and_stale_guards_disabled(self):
        self.cycle.stale_data_guard_enabled = False
        self.c.price_snapshot["fields"] = {"last": 100, "bid": 99, "ask": 101}
        blockers = self.c._risk_guard_blockers_for_buy(self.cycle)
        self.assertEqual([b["code"] for b in blockers], ["spread"])
        self.assertIn("2.00%", blockers[0]["message"])

    def test_spread_zero_disables_only_ceiling(self):
        self.cycle.max_spread_pct = 0
        self.cycle.stale_data_guard_enabled = False
        self.c.price_snapshot = None
        self.assertEqual(self.c._risk_guard_blockers_for_buy(self.cycle), [])
        self.cycle.stale_data_guard_enabled = True
        self.assertTrue(self.c._risk_guard_blockers_for_buy(self.cycle))

    def test_spread_requires_fresh_opposite_side_even_with_stale_guard_disabled(self):
        self.cycle.stale_data_guard_enabled = False
        self.c.price_snapshot["field_update_age_seconds"]["ask"] = 60
        self.assertIn("independent bid/ask", self.c._spread_guard_message_for_buy(self.cycle))

    def test_fresh_last_does_not_refresh_stale_book(self):
        self.c.price_snapshot["field_update_age_seconds"].update(bid=60, ask=60)
        self.assertIn("independent bid/ask", self.c._stale_data_guard_message_for_buy(self.cycle))

    def test_valid_quote_and_same_price_updates_are_accepted(self):
        self.assertIsNone(self.c._stale_data_guard_message_for_buy(self.cycle))
        self.assertEqual(self.c._risk_guard_blockers_for_buy(self.cycle), [])

    def test_nan_field_age_and_crossed_book_are_rejected(self):
        self.c.price_snapshot["field_update_age_seconds"]["bid"] = float("nan")
        self.assertIsNotNone(self.c._spread_guard_message_for_buy(self.cycle))
        self.c.price_snapshot = self.quote(fields={"last": 100, "bid": 101, "ask": 100})
        self.assertIn("non-crossed", self.c._spread_guard_message_for_buy(self.cycle))

    def test_unusable_selected_price_cannot_authorize_new_buy(self):
        self.c.price_snapshot["strategy_price_usable"] = False
        self.assertIn("not usable", self.c._stale_data_guard_message_for_buy(self.cycle))

    def test_partial_buy_does_not_cancel_only_because_quote_was_already_consumed(self):
        self.c.price_snapshot["strategy_price_usable"] = False
        self.assertIsNone(self.c._buy_partial_market_session_safety_reason(self.cycle))
        self.c.price_snapshot["field_update_age_seconds"]["bid"] = 60
        self.assertEqual(self.c._buy_partial_market_session_safety_reason(self.cycle)[0], "stale_data")

    def test_partial_buy_independent_spread_guard(self):
        self.cycle.stale_data_guard_enabled = False
        self.c.price_snapshot["fields"].update(bid=99, ask=101)
        self.assertEqual(self.c._buy_partial_market_session_safety_reason(self.cycle)[0], "spread")

    def test_edit_does_not_advance_cached_selected_price(self):
        self.cycle.last_price = 97
        self.c.price_snapshot["strategy_price_usable"] = False
        advanced = []
        self.c._advance_waiting_cycle_from_price = lambda *args, **kwargs: advanced.append(True)
        self.c._apply_active_strategy_edits(replace(self.settings, initial_drop_pct=1))
        self.assertEqual(advanced, [])

    def test_valid_edit_still_reevaluates(self):
        self.cycle.last_price = 97
        advanced = []
        def advance(cycle, *args, **kwargs):
            advanced.append(True)
            return cycle, []
        self.c._advance_waiting_cycle_from_price = advance
        self.c._apply_active_strategy_edits(replace(self.settings, initial_drop_pct=1))
        self.assertEqual(advanced, [True])

    def test_default_spread_validation_does_not_depend_on_master(self):
        for invalid in (-1, float("nan"), float("inf"), "bad"):
            self.assertTrue(replace(self.settings, max_spread_pct=invalid).validate())
        self.assertTrue(replace(self.settings, stale_data_guard_enabled=False, max_bid_ask_age_seconds=0).validate())
        self.assertEqual(self.settings.validate(), [])

    def test_market_sell_rejects_other_contract_even_in_error_recovery(self):
        action = self.sell_action()
        self.c.contract = QualifiedContract("BBB", 222, SimpleNamespace())
        self.c._place_market_order(self.cycle, action, "SELL")
        self.assertEqual(self.c.adapter.orders, [])
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_market_sell_rejects_same_ticker_wrong_conid(self):
        action = self.sell_action()
        self.c.contract = QualifiedContract("AAA", 222, SimpleNamespace())
        self.c._place_market_order(self.cycle, action, "SELL")
        self.assertEqual(self.c.adapter.orders, [])

    def test_manual_close_checks_identity_before_cancelling_protection(self):
        self.sell_action()
        self.cycle.protective_sell_order_ref = "IBKRBOT|AAA|guard|PROTECTIVE_SELL"
        self.cycle.protective_sell_status = "Submitted"
        self.c.contract = QualifiedContract("BBB", 222, SimpleNamespace())
        cancellations = []
        self.c.adapter.cancel_order = lambda *args: cancellations.append(args)
        self.c._request_market_close_for_app_position(self.cycle)
        self.assertEqual(cancellations, [])
        self.assertEqual(self.c.adapter.orders, [])
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_market_sell_rejects_draft_account_override(self):
        action = self.sell_action()
        self.c.connection.account = "ACCOUNT_B"
        self.c.adapter.accounts.append("ACCOUNT_B")
        self.c._place_market_order(self.cycle, action, "SELL")
        self.assertEqual(self.c.adapter.orders, [])

    def test_valid_market_sell_uses_cycle_account_when_draft_blank(self):
        action = self.sell_action()
        self.c.connection.account = ""
        self.c._place_market_order(self.cycle, action, "SELL")
        self.assertEqual(len(self.c.adapter.orders), 1)
        self.assertEqual(self.c.adapter.orders[0]["account"], "ACCOUNT_A")

    def test_new_blank_account_binds_only_unambiguous_managed_account(self):
        self.cycle.account = ""
        self.c.connection.account = ""
        self.assertIsNone(self.c._bind_cycle_account(self.cycle))
        self.assertEqual(self.storage.get_cycle(self.cycle.id).account, "ACCOUNT_A")
        self.cycle.account = ""
        self.c.adapter.accounts.append("ACCOUNT_B")
        self.assertIsNotNone(self.c._bind_cycle_account(self.cycle))

    def test_legacy_blank_account_uses_exact_order_evidence(self):
        self.cycle.account = ""
        self.c.connection.account = ""
        self.cycle.buy_order_ref = "IBKRBOT|AAA|old|BUY"
        self.cycle.buy_filled_qty = 10
        self.c.adapter.polled[self.cycle.buy_order_ref] = SimpleNamespace(
            order_ref=self.cycle.buy_order_ref, raw={"account": "ACCOUNT_A", "con_id": 111},
        )
        self.assertIsNone(self.c._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "ACCOUNT_A")

    def test_legacy_blank_account_cannot_use_sole_current_account_as_historical_proof(self):
        self.cycle.account = ""
        self.cycle.buy_filled_qty = 10
        self.cycle.buy_order_ref = "IBKRBOT|AAA|old|BUY"
        self.assertIsNotNone(self.c._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_error_cycle_blocks_identity_change_and_advanced_connect(self):
        self.cycle.stage = Stage.ERROR
        self.storage.upsert_cycle(self.cycle)
        self.c._handle_command("CONNECT", {"settings": replace(self.c.connection, client_id=99)})
        self.assertEqual(self.c.adapter.connect_calls, [])
        self.assertEqual(self.c.connection.client_id, 11)
        draft = replace(self.settings, ticker="BBB", contract_con_id=222)
        self.c._handle_command("SAVE_DRAFT_SETTINGS", {"connection": self.c.connection, "strategy": draft})
        self.assertEqual(self.c.strategy.ticker, "AAA")

    def test_start_second_ticker_cannot_orphan_error_cycle(self):
        self.cycle.stage = Stage.ERROR
        self.cycle.buy_filled_qty = 10
        self.storage.upsert_cycle(self.cycle)
        self.c._start_strategy(replace(self.settings, ticker="BBB", contract_con_id=222))
        self.assertIsNone(self.storage.get_latest_active_cycle("BBB"))
        self.assertEqual(self.c.active_cycle.id, self.cycle.id)
        self.assertIn("Start blocked", self.c.status)

    def test_multiple_old_cycles_allow_same_session_connect_only(self):
        other = CycleState.new(replace(self.settings, ticker="BBB", contract_con_id=222), 1, "ACCOUNT_A", 100, 0)
        other.stage = Stage.ERROR
        self.storage.upsert_cycle(other)
        connection = replace(self.c.connection)
        self.assertIsNone(self.c._identity_command_message(connection))
        self.assertIsNotNone(self.c._identity_command_message(replace(connection, client_id=99)))
        self.assertIsNotNone(self.c._identity_command_message(connection, self.settings))
        connected = []
        self.c._connect = lambda value: connected.append(value)
        self.c._handle_command("CONNECT", {"settings": connection})
        self.assertEqual(connected, [connection])
        self.c._start_strategy(self.settings)
        self.assertIn("Start blocked", self.c.status)
        self.assertEqual(len(self.storage.get_unresolved_cycles()), 2)

    def test_multiple_unresolved_cycles_block_new_orders_after_recovery_connect(self):
        action = self.sell_action()
        other = CycleState.new(replace(self.settings, ticker="BBB", contract_con_id=222), 1, "ACCOUNT_A", 100, 0)
        other.stage = Stage.ERROR
        self.storage.upsert_cycle(other)
        self.c._place_market_order(self.cycle, action, "SELL")
        self.assertEqual(self.c.adapter.orders, [])
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("Multiple unresolved cycles", self.c.active_cycle.error_message)

    def test_stopped_position_remains_unresolved_until_explicitly_handled(self):
        self.cycle.stage = Stage.STOPPED
        self.cycle.buy_filled_qty = 10
        self.storage.upsert_cycle(self.cycle)
        self.assertEqual(len(self.storage.get_unresolved_cycles()), 1)
        self.storage.add_decision_event(event_type="MANUALLY_HANDLED", message="Operator reconciled externally", cycle=self.cycle)
        self.assertEqual(self.storage.get_unresolved_cycles(), [])

    def test_completed_flat_cycle_does_not_block_new_cycle(self):
        self.cycle.stage = Stage.CYCLE_COMPLETE
        self.cycle.buy_filled_qty = 10
        self.cycle.sell_filled_qty = 10
        self.storage.upsert_cycle(self.cycle)
        self.assertEqual(self.storage.get_unresolved_cycles(), [])


if __name__ == "__main__":
    unittest.main()
