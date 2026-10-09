"""Regressions for completed SELL executions received after a Gateway outage."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from app.ib_adapter import PolledOrderState
from app.models import Stage, recovery_cycle_signature
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class CompletedExecutionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v562_execution_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.broker = DeterministicBrokerAdapter()
        self.broker.accounts = ["SIM"]
        settings = permissive_strategy()
        settings.contract_con_id = self.broker.contract.con_id
        self.c = make_controller(self.module, Path(folder.name) / "state.sqlite", self.broker, settings)
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._start_trade_market_data_capture = Mock()
        self.c._queue_database_backup = Mock()
        self.cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 827.17, 0.0)
        self.cycle.con_id = self.broker.contract.con_id
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.quantity = self.cycle.buy_filled_qty = 12
        self.cycle.avg_buy_price = 827.17
        self.cycle.buy_status = "Filled"
        self.cycle.buy_commission = 5.3232
        self.cycle.sell_order_ref = f"IBKRBOT|AAPL|{self.cycle.id}|SELL_TRAIL"
        self.cycle.sell_order_id, self.cycle.sell_perm_id = 99, 999
        self.cycle.sell_status = "PreSubmitted"
        self.broker.recovery_open_app_orders = Mock(return_value=[])
        self.save()

    def save(self):
        self.c.active_cycle = self.cycle
        self.c.storage.upsert_cycle(self.cycle)

    def execution(self, **changes):
        row = {
            "execution_id": "SIM-EXIT-1", "order_ref": self.cycle.sell_order_ref,
            "order_id": 99, "perm_id": 999, "account": "SIM", "con_id": 123,
            "ticker": "AAPL", "side": "SLD", "shares": 12.0, "price": 729.80,
            "commission": 5.380772, "currency": "USD",
            "executed_at": "2026-10-08T07:04:24+00:00",
        }
        row.update(changes)
        return row

    def polled(self, *, status="Filled", filled=0, remaining=0, normalized=False):
        return PolledOrderState(
            self.cycle.sell_order_ref, 0, 999, status, filled, remaining, 729.80,
            5.380772, [self.execution()], {
                "filled_from_executions": normalized, "account": "SIM",
                "con_id": 123, "action": "SELL",
            },
        )

    def recover(self, polled=None, rows=None):
        self.broker.executions = [self.execution()] if rows is None else rows
        self.broker.poll_order = Mock(return_value=polled)
        result = self.c._recover_after_connect()
        self.assertTrue(result)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.broker.cancelled_orders, [])
        return self.c.storage.get_cycle(self.cycle.id)

    def assert_complete(self, cycle):
        self.assertEqual(cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(cycle.sell_filled_qty, 12)
        self.assertAlmostEqual(cycle.avg_sell_price, 729.80)
        self.assertAlmostEqual(cycle.net_pnl, -1179.143972)
        self.assertEqual(cycle.sell_filled_at, "2026-10-08T07:04:24+00:00")
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 12)

    def test_archived_completed_zero_counter_with_exact_execution_completes(self):
        self.assert_complete(self.recover(self.polled()))

    def test_cached_presubmitted_zero_does_not_mask_fresh_execution(self):
        self.assert_complete(self.recover(self.polled(status="PreSubmitted", remaining=12)))

    def test_cached_partial_does_not_mask_fresh_complete_execution(self):
        self.assert_complete(self.recover(self.polled(status="Submitted", filled=3, remaining=9)))

    def test_missing_order_object_recovers_same_fill(self):
        self.assert_complete(self.recover())

    def test_duplicate_execution_snapshot_counts_quantity_and_fee_once(self):
        row = self.execution()
        self.assert_complete(self.recover(self.polled(), [row, deepcopy(row)]))

    def test_repeat_recovery_does_not_duplicate_fill_or_repeat_cycle(self):
        self.assert_complete(self.recover(self.polled()))
        self.assertTrue(self.c._recover_after_connect())
        self.assert_complete(self.c.storage.get_cycle(self.cycle.id))
        self.assertEqual(self.broker.placed_orders, [])
        self.assertEqual(self.c.storage.get_next_cycle_number("AAPL"), 2)

    def test_partial_recent_execution_does_not_complete(self):
        cycle = self.recover(self.polled(), [self.execution(shares=6)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 6)

    def test_oversell_recent_execution_does_not_complete(self):
        cycle = self.recover(self.polled(), [self.execution(shares=13)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 13)

    def test_conflicting_duplicate_id_does_not_complete(self):
        cycle = self.recover(self.polled(), [self.execution(), self.execution(shares=6)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)

    def test_malformed_matching_execution_does_not_hide_behind_valid_one(self):
        cycle = self.recover(self.polled(), [self.execution(), self.execution(execution_id="BAD", shares="bad")])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)

    def test_invalid_quantities_cannot_be_rounded_to_complete_sale(self):
        for shares in (float("nan"), float("inf"), -12, 12.5):
            with self.subTest(shares=shares):
                self.broker.executions = [self.execution(shares=shares)]
                self.assertIsNone(self.c._recover_sell_from_executions(self.cycle))
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "SELL")["shares"], 0)

    def test_invalid_prices_are_not_recorded(self):
        for price in (float("nan"), float("inf"), -1, 0):
            with self.subTest(price=price):
                self.broker.executions = [self.execution(price=price)]
                self.assertIsNone(self.c._recover_sell_from_executions(self.cycle))
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "SELL")["shares"], 0)

    def test_finite_price_with_overflowing_notional_cannot_complete(self):
        cycle = self.recover(self.polled(), [self.execution(price=1.7e308)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)

    def test_finite_individual_notionals_with_overflowing_sum_cannot_complete(self):
        rows = [
            self.execution(shares=6, price=2e307),
            self.execution(execution_id="SIM-EXIT-2", shares=6, price=2e307),
        ]
        cycle = self.recover(self.polled(), rows)
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)

    def test_nonfinite_or_malformed_commission_does_not_enter_ledger(self):
        for fee in (float("nan"), float("inf"), -float("inf"), "invalid"):
            with self.subTest(fee=fee):
                self.broker.executions = [self.execution(commission=fee)]
                self.assertIsNone(self.c._recover_sell_from_executions(self.cycle))
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "SELL")["shares"], 0)
        self.assertEqual(self.c.storage.get_cycle(self.cycle.id).stage, Stage.SELL_TRAIL_ACTIVE)

    def test_overflowing_commission_sum_cannot_complete(self):
        rows = [
            self.execution(shares=6, commission=1e308),
            self.execution(execution_id="SIM-EXIT-2", shares=6, commission=1e308),
        ]
        cycle = self.recover(self.polled(), rows)
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)

    def test_numeric_aliases_are_normalized_before_deduplication(self):
        row = self.execution()
        alias = {key: value for key, value in row.items() if key not in {"shares", "price"}}
        alias.update(qty=12, avgPrice=729.80)
        self.assert_complete(self.recover(self.polled(), [row, alias]))

    def test_missing_execution_id_never_accumulates_on_repeated_recovery(self):
        self.broker.executions = [self.execution(execution_id="", shares=6)]
        for _ in range(2):
            self.assertIsNone(self.c._recover_sell_from_executions(self.cycle))
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "SELL")["shares"], 0)
        self.assertEqual(self.c.storage.get_cycle(self.cycle.id).stage, Stage.SELL_TRAIL_ACTIVE)

    def test_recovery_updates_exact_fully_filled_local_order_status(self):
        self.c.storage.add_order(
            cycle=self.cycle, action="SELL", order_type="TRAIL", order_id=99,
            perm_id=999, order_ref=self.cycle.sell_order_ref, quantity=12,
            trailing_percent=0.67, initial_stop_price=831.4, status="PreSubmitted",
        )
        self.assert_complete(self.recover(self.polled(status="PreSubmitted", remaining=12)))
        order = self.c.storage.get_order_for_cycle_ref(self.cycle.id, self.cycle.sell_order_ref)
        self.assertEqual(order["status"], "Filled")
        self.assertEqual(order["order_id"], 99)

    def test_wrong_account_execution_cannot_complete(self):
        cycle = self.recover(self.polled(), [self.execution(account="OTHER")])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 0)

    def test_wrong_contract_execution_cannot_complete(self):
        cycle = self.recover(self.polled(), [self.execution(con_id=456)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 0)

    def test_wrong_permanent_id_execution_cannot_complete(self):
        cycle = self.recover(self.polled(), [self.execution(perm_id=998)])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 0)

    def test_filled_zero_without_execution_is_not_successfully_reconciled(self):
        cycle = self.recover(self.polled(), [])
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c._recovery_confidence(), "manual_review_required")

    def test_explicit_manual_close_keeps_confirmed_cancel_remainder_workflow(self):
        self.cycle.close_position_market_requested = True
        self.cycle.stop_after_current_cycle = True
        self.save()
        polled = self.polled(status="Cancelled", filled=6)
        polled.executions = [self.execution(shares=6)]
        self.c._submit_requested_market_close = Mock()
        cycle = self.recover(polled, [self.execution(shares=6)])
        self.assertEqual(cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertEqual(cycle.sell_filled_qty, 6)
        self.c._submit_requested_market_close.assert_called_once()

    def test_manual_close_full_fresh_execution_prevents_stale_cancel_replacement(self):
        self.cycle.close_position_market_requested = True
        self.cycle.stop_after_current_cycle = True
        self.save()
        first = self.execution(shares=6, commission=2)
        second = self.execution(execution_id="SIM-EXIT-2", shares=6, commission=3)
        polled = self.polled(status="Cancelled", filled=6)
        polled.executions = [first]
        self.c._submit_requested_market_close = Mock()
        cycle = self.recover(polled, [first, second])
        self.assertEqual(cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(cycle.sell_filled_qty, 12)
        self.c._submit_requested_market_close.assert_not_called()

    def test_manual_close_partial_fresh_execution_reduces_replacement_remainder(self):
        self.cycle.close_position_market_requested = True
        self.cycle.stop_after_current_cycle = True
        self.save()
        first = self.execution(shares=6, commission=2)
        second = self.execution(execution_id="SIM-EXIT-2", shares=2, commission=1)
        polled = self.polled(status="Cancelled", filled=6)
        polled.executions = [first]
        self.c._submit_requested_market_close = Mock()
        cycle = self.recover(polled, [first, second])
        self.assertEqual(cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertEqual(cycle.sell_filled_qty, 8)
        submitted_cycle = self.c._submit_requested_market_close.call_args.args[0]
        self.assertEqual(self.c._app_unsold_quantity(submitted_cycle), 4)

    def test_fresh_working_order_retains_its_remainder(self):
        polled = self.polled(status="Submitted", filled=3, remaining=9)
        self.broker.recovery_open_app_orders.return_value = [polled]
        cycle = self.recover(polled, [self.execution(shares=3)])
        self.assertEqual(cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertFalse(cycle.recovery_required)

    def test_old_execution_outside_recent_lookback_counts_toward_exact_close(self):
        self.c.storage.upsert_execution(
            cycle=self.cycle, ticker="AAPL", side="SELL", shares=4, price=729.80,
            commission=1, order_ref=self.cycle.sell_order_ref, execution_id="EARLIER",
        )
        cycle = self.recover(self.polled(), [self.execution(shares=8, commission=2)])
        self.assertEqual(cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(cycle.sell_filled_qty, 12)
        self.assertEqual(cycle.sell_commission, 3)

    def test_prior_execution_cannot_be_hidden_to_complete_oversell(self):
        self.c.storage.upsert_execution(
            cycle=self.cycle, ticker="AAPL", side="SELL", shares=1, price=729.80,
            order_ref=self.cycle.sell_order_ref, execution_id="EARLIER",
        )
        cycle = self.recover(self.polled())
        self.assertEqual(cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(cycle.sell_filled_qty, 13)

    def test_cumulative_placeholder_is_replaced_by_real_execution(self):
        self.c.storage.reconcile_cumulative_execution_placeholder(
            cycle=self.cycle, side="SELL", order_ref=self.cycle.sell_order_ref,
            cumulative_shares=12, cumulative_avg_price=729.80, cumulative_commission=5.380772,
        )
        self.assert_complete(self.recover(self.polled()))

    def test_pending_corrected_fee_is_consumed_by_recovery(self):
        fee = self.execution(commission=7.25)
        self.c._apply_execution_callback_event("COMMISSION_REPORT", fee, self.cycle)
        cycle = self.recover(self.polled())
        self.assertEqual(cycle.sell_commission, 7.25)
        self.assertEqual(self.c._pending_commissions_by_execution_id, {})
        self.c._apply_execution_callback_event("COMMISSION_REPORT", fee, cycle)
        self.assertEqual(self.c.storage.get_execution_totals(cycle.id, "SELL")["shares"], 12)

    def test_pending_zero_fee_overrides_stale_recovered_fee(self):
        self.c._apply_execution_callback_event("COMMISSION_REPORT", self.execution(commission=0), self.cycle)
        cycle = self.recover(self.polled())
        self.assertEqual(cycle.sell_commission, 0)
        self.assertEqual(self.c._pending_commissions_by_execution_id, {})

    def test_pending_foreign_currency_fee_is_excluded(self):
        self.c._apply_execution_callback_event("COMMISSION_REPORT", self.execution(commission=5, currency="EUR"), self.cycle)
        cycle = self.recover(self.polled())
        self.assertEqual(cycle.sell_commission, 0)

    def test_normalized_live_poll_records_fill_and_original_timestamp(self):
        self.c._handle_sell_order_poll(self.cycle, self.polled(filled=12, normalized=True))
        self.assert_complete(self.c.storage.get_cycle(self.cycle.id))

    def test_pending_fee_is_consumed_by_normalized_live_poll(self):
        self.c._apply_execution_callback_event("COMMISSION_REPORT", self.execution(commission=0), self.cycle)
        self.c._handle_sell_order_poll(self.cycle, self.polled(filled=12, normalized=True))
        cycle = self.c.storage.get_cycle(self.cycle.id)
        self.assertEqual(cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(cycle.sell_commission, 0)
        self.assertEqual(self.c._pending_commissions_by_execution_id, {})

    def test_normalized_poll_identity_conflicts_are_rejected_for_all_sides(self):
        for side in ("BUY", "SELL", "PROTECTIVE_SELL"):
            for field, value in (("account", "OTHER"), ("con_id", 456), ("perm_id", 998)):
                with self.subTest(side=side, field=field):
                    cycle = deepcopy(self.cycle)
                    cycle.stage = Stage.BUY_TRAIL_ACTIVE if side == "BUY" else (Stage.WAIT_RISE_TRIGGER if side == "PROTECTIVE_SELL" else Stage.SELL_TRAIL_ACTIVE)
                    if side == "BUY":
                        cycle.buy_order_ref, cycle.buy_perm_id = cycle.sell_order_ref, 999
                        cycle.sell_order_ref = None
                    elif side == "PROTECTIVE_SELL":
                        cycle.protective_sell_order_ref, cycle.protective_sell_perm_id = cycle.sell_order_ref, 999
                        cycle.sell_order_ref = None
                    self.c.active_cycle = cycle
                    self.c.storage.upsert_cycle(cycle)
                    polled = self.polled(filled=12, normalized=True)
                    polled.raw["action"] = "BUY" if side == "BUY" else "SELL"
                    if field == "perm_id":
                        polled.perm_id = value
                    else:
                        polled.raw[field] = value
                    handler = getattr(self.c, f"_handle_{side.lower()}_order_poll")
                    handler(cycle, polled)
                    self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
                    self.assertEqual(self.c.storage.get_execution_totals(cycle.id, side)["shares"], 0)

    def test_buy_execution_deduplication_preserves_quantity(self):
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.buy_order_ref, self.cycle.buy_perm_id = self.cycle.sell_order_ref, 999
        self.cycle.sell_order_ref = None
        self.cycle.buy_filled_qty = 0
        self.cycle.buy_status = "Submitted"
        self.save()
        row = self.execution(order_ref=self.cycle.buy_order_ref, side="BOT")
        self.broker.executions = [row, deepcopy(row)]
        cycle = self.c._recover_buy_from_executions(self.cycle)
        self.assertEqual(cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(cycle.buy_filled_qty, 12)

    def test_protective_execution_deduplication_preserves_quantity(self):
        self.cycle.stage = Stage.WAIT_RISE_TRIGGER
        self.cycle.protective_sell_order_ref, self.cycle.protective_sell_perm_id = self.cycle.sell_order_ref, 999
        self.cycle.sell_order_ref = None
        self.save()
        row = self.execution(order_ref=self.cycle.protective_sell_order_ref)
        self.broker.executions = [row, deepcopy(row)]
        cycle = self.c._recover_protective_sell_from_executions(self.cycle)
        self.assertEqual(cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(cycle.protective_sell_filled_qty, 12)

    def test_unknown_position_is_not_fully_reconciled(self):
        self.c._last_recovery_probe = {"checked_at": "now", "position_size": None}
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")

    def test_nonfinite_position_is_not_fully_reconciled(self):
        self.c._last_recovery_probe = {"checked_at": "now", "position_size": float("nan")}
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")

    def test_filled_status_with_incomplete_local_quantity_is_not_fully_reconciled(self):
        self.cycle.sell_status = "Filled"
        self.save()
        self.c._last_recovery_probe = {"checked_at": "now", "position_size": 0}
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")

    def test_changed_cycle_signature_is_not_fully_reconciled(self):
        signature = recovery_cycle_signature(self.cycle)
        self.cycle.sell_filled_qty = 1
        self.save()
        self.c._last_recovery_probe = {"checked_at": "now", "local_cycle_signature": signature}
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")

    def test_invalidated_probe_is_not_fully_reconciled(self):
        self.c._last_recovery_probe = {"checked_at": "then", "invalidated_at": "now"}
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")

    def test_pending_recovery_is_not_fully_reconciled(self):
        self.c._last_recovery_probe = {"checked_at": "now"}
        self.c._upstream_recovery_pending = True
        self.assertEqual(self.c._recovery_confidence(), "broker_partially_checked")
