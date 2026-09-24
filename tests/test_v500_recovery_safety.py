"""Offline recovery regressions; runnable with pytest or stdlib unittest."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from app.ib_adapter import IbAsyncTwsAdapter, PolledOrderState
from app.models import Stage, utc_now_iso
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class RecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_module = sys.modules.pop("app.controller", None)
        self.old_attr = getattr(app, "controller", None)
        env = patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._restore_controller_module)
        self.module = importlib.import_module("app.controller")
        self.broker = DeterministicBrokerAdapter()
        self.settings = permissive_strategy(auto_repeat=True)
        self.settings.contract_con_id = self.broker.contract.con_id
        self.controller = make_controller(
            self.module, Path(self.temp.name) / "state.sqlite", self.broker, self.settings,
        )
        self.controller._start_trade_market_data_capture = lambda *args, **kwargs: None
        self.cycle = StrategyEngine.start_cycle(self.settings, 1, "DU_TEST", 100.0, 0.0)
        self.cycle.buy_filled_qty = 100
        self.cycle.quantity = 100
        self.cycle.avg_buy_price = 100.0
        self.cycle.buy_status = "Filled"
        self.cycle.stage = Stage.WAIT_RISE_TRIGGER
        self.broker.external_position = 100.0
        self.save()

    def _restore_controller_module(self):
        sys.modules.pop("app.controller", None)
        if self.old_module is not None:
            sys.modules["app.controller"] = self.old_module
        if self.old_attr is None:
            if hasattr(app, "controller"):
                delattr(app, "controller")
        else:
            app.controller = self.old_attr

    def save(self):
        self.controller.active_cycle = self.cycle
        self.controller.storage.upsert_cycle(self.cycle)

    @staticmethod
    def poll(ref, *, status="Submitted", filled=0, remaining=100, price=105.0):
        return PolledOrderState(ref, 11, 21, status, filled, remaining, price, 0.0, [], {})

    def stored_order(self, ref, *, role="SELL", quantity=100, status="Submitted"):
        self.controller.storage.add_order(
            cycle=self.cycle, action=role, order_type="PROTECTIVE_TRAIL" if role == "PROTECTIVE_SELL" else "TRAIL",
            order_id=11, perm_id=21, order_ref=ref, quantity=quantity,
            trailing_percent=1.0, initial_stop_price=105.0, status=status,
        )

    def execution(self, ref, *, key="late-sell", shares=20.0, commission=None):
        event = {
            "order_ref": ref, "execution_id": key, "ticker": self.cycle.ticker,
            "side": "SLD", "shares": shares, "price": 105.0,
            "account": self.cycle.account, "con_id": self.cycle.con_id,
            "executed_at": utc_now_iso(),
        }
        if commission is not None:
            event["commission"] = commission
            event["currency"] = "USD"
        return event

    def apply_execution(self, event, kind="EXEC_DETAILS"):
        owner = self.controller._cycle_for_order_ref(event["order_ref"])
        self.controller._apply_execution_callback_event(kind, event, owner)

    def test_recovery_terminal_partial_never_completes_or_repeats(self):
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.sell_order_ref = "IBKRBOT|AAPL|C1|OLD|SELL"
        self.save()
        self.broker.orders[self.cycle.sell_order_ref] = self.poll(
            self.cycle.sell_order_ref, status="Cancelled", filled=40, remaining=0,
        )
        repeats = []
        self.controller._maybe_start_next_cycle = lambda: repeats.append(True)
        self.controller._recover_after_connect()
        self.assertEqual(self.controller.active_cycle.stage, Stage.ERROR)
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 40)
        self.assertEqual(repeats, [])

    def test_recovery_full_sell_completes_and_repeats_once(self):
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.sell_order_ref = "IBKRBOT|AAPL|C1|SELL"
        self.save()
        self.broker.orders[self.cycle.sell_order_ref] = self.poll(
            self.cycle.sell_order_ref, status="Filled", filled=100, remaining=0,
        )
        repeats = []
        self.controller._maybe_start_next_cycle = lambda: repeats.append(True)
        self.controller._recover_after_connect()
        self.assertEqual(self.controller.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(repeats, [True])

    def test_recovery_forced_sell_uses_prior_executions(self):
        old = "IBKRBOT|AAPL|C1|OLD|SELL"
        self.stored_order(old, status="Cancelled")
        self.apply_execution(self.execution(old, shares=40.0))
        self.cycle = self.controller.active_cycle
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.sell_order_ref = "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.save()
        self.broker.orders[self.cycle.sell_order_ref] = self.poll(
            self.cycle.sell_order_ref, status="Filled", filled=60, remaining=0,
        )
        self.controller._maybe_start_next_cycle = lambda: None
        self.controller._recover_after_connect()
        self.assertEqual(self.controller.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 100)

    def test_short_broker_position_blocks_recovery(self):
        self.broker.external_position = 50
        self.assertFalse(self.controller._check_position_for_waiting_cycle(self.cycle))
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.controller._recovery_required)

    def test_extra_external_holdings_are_allowed(self):
        self.broker.external_position = 250
        self.assertTrue(self.controller._check_position_for_waiting_cycle(self.cycle))
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)

    def test_unknown_or_nonfinite_holdings_fail_closed(self):
        for value in (None, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.cycle.stage = Stage.WAIT_RISE_TRIGGER
                self.save()
                self.broker.position_size = lambda *args, value=value, **kwargs: value
                self.assertFalse(self.controller._check_position_for_waiting_cycle(self.cycle))
                self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)

    def test_contract_mismatch_never_uses_other_position(self):
        self.cycle.con_id = 999
        self.save()
        self.assertFalse(self.controller._check_position_for_waiting_cycle(self.cycle))
        self.assertIn("exact account and contract", self.controller.active_cycle.error_message)

    def test_production_position_requires_refresh_and_cycle_account(self):
        self.broker.requires_exact_contract_selection = True
        calls = []
        def position(contract, account="", *, refresh=False):
            calls.append((contract.con_id, account, refresh))
            return 100 if refresh else 999
        self.broker.position_size = position
        self.assertTrue(self.controller._check_position_for_waiting_cycle(self.cycle))
        self.assertEqual(calls, [(self.cycle.con_id, "DU_TEST", True)])

    def test_production_missing_account_fails_closed(self):
        self.broker.requires_exact_contract_selection = True
        self.cycle.account = ""
        self.save()
        self.assertFalse(self.controller._check_position_for_waiting_cycle(self.cycle))

    def test_recovered_protective_fills_reduce_required_position_once(self):
        ref = "IBKRBOT|AAPL|C1|PROTECTIVE_SELL"
        self.cycle.protective_sell_order_ref = ref
        self.cycle.protective_sell_status = "Submitted"
        self.save()
        self.broker.executions = [self.execution(ref, shares=40.0)]
        self.broker.external_position = 60
        self.assertTrue(self.controller._check_position_for_waiting_cycle(self.cycle))
        self.assertEqual(self.controller.active_cycle.protective_sell_filled_qty, 40)
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 40)
        self.assertEqual(self.controller._app_unsold_quantity(self.controller.active_cycle), 60)

    def test_legacy_missing_protection_requires_recovery(self):
        self.cycle.protective_sell_enabled = True
        self.save()
        self.controller._recover_after_connect()
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertIn("protective SELL is absent", self.controller.active_cycle.error_message)
        self.assertEqual(self.broker.placed_orders, [])

    def test_represented_late_execution_does_not_cancel_safe_replacement(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.controller._record_polled_executions(self.cycle, self.poll(
            old, status="Cancelled", filled=20, remaining=80,
        ), "SELL")
        self.controller._reconcile_cycle_execution_ledger(self.cycle.id)
        self.cycle = self.controller.active_cycle
        self.stored_order(new, quantity=80)
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.broker.orders[new] = self.poll(new, remaining=80)
        self.apply_execution(self.execution(old))
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 20)
        self.assertEqual(self.broker.cancelled_orders, [])
        self.assertEqual(self.controller.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)

    def test_late_historical_fill_cancels_oversized_replacement(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.stored_order(new)
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.broker.orders[new] = self.poll(new)
        self.apply_execution(self.execution(old))
        self.assertIsNotNone(self.controller.storage.get_execution("late-sell"))
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 20)
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.broker.cancelled_orders, [new])
        self.assertEqual(self.broker.placed_orders, [])

    def test_late_fill_and_commission_are_idempotent(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.stored_order(new)
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.broker.orders[new] = self.poll(new)
        event = self.execution(old)
        self.apply_execution(event)
        self.apply_execution(event)
        self.apply_execution(self.execution(old, commission=0.4), "COMMISSION_REPORT")
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 20)
        self.assertAlmostEqual(self.controller.active_cycle.sell_commission, 0.4)
        self.assertEqual(self.broker.cancelled_orders, [new])

    def test_safe_replacement_quantity_is_not_cancelled(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.stored_order(new, quantity=80)
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.broker.orders[new] = self.poll(new, remaining=80)
        self.apply_execution(self.execution(old))
        self.assertEqual(self.controller.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_unavailable_replacement_status_requires_recovery(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.apply_execution(self.execution(old))
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.broker.placed_orders, [])

    def test_previous_cycle_overfill_cancels_same_contract_current_buy(self):
        old, settled_ref = "IBKRBOT|AAPL|C1|OLD|SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, status="Cancelled")
        self.stored_order(settled_ref, status="Filled")
        self.cycle.sell_order_ref, self.cycle.sell_status = settled_ref, "Filled"
        self.cycle.stage = Stage.CYCLE_COMPLETE
        self.save()
        self.controller._record_polled_executions(self.cycle, self.poll(
            settled_ref, status="Filled", filled=100, remaining=0,
        ), "SELL")
        self.controller._reconcile_cycle_execution_ledger(self.cycle.id)
        historical_id = self.cycle.id
        current = StrategyEngine.start_cycle(self.settings, 2, "DU_TEST", 100.0, 0.0)
        current.stage = Stage.BUY_TRAIL_ACTIVE
        current.buy_order_ref, current.buy_status = "IBKRBOT|AAPL|C2|BUY", "Submitted"
        self.controller.storage.upsert_cycle(current)
        self.controller.active_cycle = current
        self.broker.orders[current.buy_order_ref] = self.poll(current.buy_order_ref)
        self.apply_execution(self.execution(old))
        self.assertEqual(self.controller.active_cycle.id, current.id)
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.controller.storage.get_cycle(historical_id).stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.broker.cancelled_orders, [current.buy_order_ref])

    def test_unknown_foreign_reference_is_not_ledgered(self):
        self.apply_execution(self.execution("IBKRBOT|AAPL|OTHER|SELL"))
        self.assertIsNone(self.controller.storage.get_execution("late-sell"))
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_late_protective_fill_supervises_normal_replacement(self):
        old, new = "IBKRBOT|AAPL|C1|OLD|PROTECTIVE_SELL", "IBKRBOT|AAPL|C1|FORCED_SELL_MARKET"
        self.stored_order(old, role="PROTECTIVE_SELL", status="Cancelled")
        self.stored_order(new)
        self.cycle.sell_order_ref, self.cycle.sell_status = new, "Submitted"
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.save()
        self.broker.orders[new] = self.poll(new)
        self.apply_execution(self.execution(old))
        self.assertEqual(self.controller.active_cycle.protective_sell_filled_qty, 20)
        self.assertEqual(self.controller.active_cycle.sell_filled_qty, 20)
        self.assertEqual(self.controller._app_unsold_quantity(self.controller.active_cycle), 80)
        self.assertEqual(self.broker.cancelled_orders, [new])

    def test_partial_buy_rejection_manages_shares_but_stops_repeat(self):
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.buy_filled_qty = 0
        self.cycle.avg_buy_price = None
        self.cycle.buy_order_ref = "IBKRBOT|AAPL|C1|BUY"
        self.save()
        polled = self.poll(self.cycle.buy_order_ref, status="Inactive", filled=40, remaining=60, price=100)
        self.controller._handle_buy_order_poll(self.cycle, polled)
        settled = self.controller.active_cycle
        self.assertEqual(settled.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(settled.buy_filled_qty, 40)
        self.assertTrue(settled.stop_after_current_cycle)
        self.controller.active_cycle = StrategyEngine.on_sell_fill(settled, 40, 105, "Filled")
        self.controller._maybe_start_next_cycle()
        self.assertEqual(self.controller.active_cycle.id, settled.id)

    def test_successful_or_cancelled_buy_warning_preserves_repeat(self):
        for status, filled, remaining in (("Filled", 100, 0), ("Cancelled", 40, 60)):
            with self.subTest(status=status):
                self.cycle = StrategyEngine.start_cycle(self.settings, 1, "DU_TEST", 100.0, 0.0)
                self.cycle.quantity = 100
                self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
                self.cycle.buy_filled_qty = 0
                self.cycle.avg_buy_price = None
                self.cycle.buy_order_ref = f"IBKRBOT|AAPL|C1|BUY|{status}"
                self.save()
                state = self.poll(self.cycle.buy_order_ref, status=status, filled=filled, remaining=remaining, price=100)
                state.raw["broker_error"] = {"error_code": 2109, "message": "Outside RTH attribute ignored; order being processed"}
                self.controller._handle_buy_order_poll(self.cycle, state)
                self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
                self.assertFalse(self.controller.active_cycle.stop_after_current_cycle)

    def test_partial_buy_cancelled_with_substantive_error_stops_repeat(self):
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.buy_filled_qty = 0
        self.cycle.avg_buy_price = None
        self.cycle.buy_order_ref = "IBKRBOT|AAPL|C1|BUY"
        self.save()
        state = self.poll(self.cycle.buy_order_ref, status="Cancelled", filled=40, remaining=60, price=100)
        state.raw["broker_error"] = {"error_code": 201, "message": "Order rejected: insufficient funds"}
        self.controller._handle_buy_order_poll(self.cycle, state)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertTrue(self.controller.active_cycle.stop_after_current_cycle)

    def test_cancelled_partial_buy_without_rejection_preserves_repeat(self):
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.buy_filled_qty = 0
        self.cycle.avg_buy_price = None
        self.cycle.buy_order_ref = "IBKRBOT|AAPL|C1|BUY"
        self.save()
        self.controller._handle_buy_order_poll(self.cycle, self.poll(
            self.cycle.buy_order_ref, status="Cancelled", filled=40, remaining=60, price=100,
        ))
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertFalse(self.controller.active_cycle.stop_after_current_cycle)

    def test_uncertain_manual_review_fill_does_not_promote_or_repeat(self):
        self.cycle.stage = Stage.MANUAL_REVIEW
        self.cycle.recovery_required = True
        self.cycle.buy_order_ref = "IBKRBOT|AAPL|C1|BUY"
        self.cycle.buy_filled_qty = 0
        self.save()
        event = self.execution(self.cycle.buy_order_ref)
        event["side"] = "BOT"
        self.apply_execution(event)
        self.assertEqual(self.controller.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(self.controller.active_cycle.recovery_required)
        self.assertEqual(self.broker.placed_orders, [])

    def test_adapter_refresh_uses_authoritative_exact_position(self):
        adapter = IbAsyncTwsAdapter.__new__(IbAsyncTwsAdapter)
        def pos(account, con_id, qty):
            return SimpleNamespace(account=account, contract=SimpleNamespace(conId=con_id, symbol="AAPL"), position=qty)
        requests = []
        def fresh():
            requests.append(True)
            return [pos("DU_OTHER", 123, 999), pos("DU_TEST", 999, 999), pos("", 123, 999), pos("DU_TEST", 123, 50)]
        adapter.ib = SimpleNamespace(isConnected=lambda: True, positions=lambda: [pos("DU_TEST", 123, 1000)], reqPositions=fresh)
        self.assertEqual(adapter.position_size(self.broker.contract, "DU_TEST", refresh=True), 50)
        self.assertEqual(requests, [True])
        adapter.ib.reqPositions = lambda: []
        self.assertEqual(adapter.position_size(self.broker.contract, "DU_TEST", refresh=True), 0)
        def fail():
            raise RuntimeError("refresh unavailable")
        adapter.ib.reqPositions = fail
        self.assertIsNone(adapter.position_size(self.broker.contract, "DU_TEST", refresh=True))


if __name__ == "__main__":
    unittest.main()
