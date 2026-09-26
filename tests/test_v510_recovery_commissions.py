"""Exact execution ownership and signed, currency-checked fee regressions."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ib_adapter import PolledOrderState
from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class RecoveryCommissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v510_fee_controller", path)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.broker = DeterministicBrokerAdapter()
        self.broker.accounts = ["DU_TEST"]
        self.settings = permissive_strategy()
        self.settings.contract_con_id = self.broker.contract.con_id
        self.c = make_controller(self.module, Path(self.folder.name) / "state.sqlite", self.broker, self.settings)
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._start_trade_market_data_capture = lambda *args, **kwargs: None
        self.cycle = StrategyEngine.start_cycle(self.settings, 1, "DU_TEST", 100.0, 0.0)
        self.cycle.con_id = self.broker.contract.con_id
        self.cycle.stage = Stage.BUY_TRAIL_ACTIVE
        self.cycle.quantity = 10
        self.cycle.buy_order_ref = "IBKRBOT|AAPL|C1|OWN|BUY_TRAIL"
        self.cycle.buy_order_id = 11
        self.cycle.buy_perm_id = 22
        self.cycle.buy_status = "Submitted"
        self.save()

    def save(self):
        self.c.active_cycle = self.cycle
        self.c.storage.upsert_cycle(self.cycle)

    def event(self, **changes):
        result = {
            "order_ref": self.cycle.buy_order_ref, "execution_id": "EXEC_1",
            "order_id": 11, "perm_id": 22, "ticker": "AAPL", "con_id": 123,
            "account": "DU_TEST", "side": "BOT", "shares": 10.0,
            "price": 100.0, "commission": 0.0, "currency": "USD",
        }
        result.update(changes)
        return result

    def apply(self, event, kind="EXEC_DETAILS"):
        owner = self.c._cycle_for_order_ref(event["order_ref"])
        self.c._apply_execution_callback_event(kind, event, owner)
        self.cycle = self.c.active_cycle

    def poll(self, commission, *, currency="USD", role="BUY", executions=True):
        ref = self.cycle.buy_order_ref if role == "BUY" else self.cycle.protective_sell_order_ref
        rows = [{"execId": "EXEC_1", "shares": 10.0, "price": 100.0,
                 "commission": commission, "currency": currency}] if executions else []
        return PolledOrderState(ref, 11, 22, "Filled", 10, 0, 100.0, commission, rows,
                                {"commission_currencies": [currency]})

    def test_foreign_numeric_id_collision_does_not_recover_or_sell(self):
        self.broker.executions = [self.event(order_ref="", perm_id=999, ticker="MSFT", con_id=999, account="DU_OTHER")]
        self.c._recover_after_connect()
        for price in (120.0, 121.0, 122.0):
            self.broker.publish_price(price)
            self.c._run_strategy_cycle()
        self.assertEqual(self.c.active_cycle.buy_filled_qty, 0)
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.broker.placed_orders, [])

    def test_conflicting_available_identity_is_rejected_even_with_exact_ref(self):
        for changed in ({"account": "DU_OTHER"}, {"ticker": "MSFT"}, {"con_id": 999},
                        {"perm_id": 999}, {"sec_type": "OPT"}, {"side": "SLD"}):
            with self.subTest(changed=changed):
                self.assertFalse(self.c._execution_matches_order(self.cycle, self.event(**changed), "BUY"))

    def test_recovery_currency_conflict_clears_stored_fee_from_cycle_and_ledger(self):
        self.apply(self.event(commission=1.0))
        self.apply(self.event(commission=1.0), "COMMISSION_REPORT")
        self.broker.executions = [self.event(commission=2.0, currency="EUR")]
        recovered = self.c._recover_buy_from_executions(self.cycle)
        self.assertEqual(recovered.buy_filled_qty, 10)
        self.assertEqual(recovered.buy_commission, 0.0)
        self.assertEqual(self.c.storage.get_execution("EXEC_1")["commission"], 0.0)
        self.assertTrue(recovered.stop_after_current_cycle)

    def test_exact_reference_allows_missing_legacy_optional_fields(self):
        row = self.event(account="", con_id=None, perm_id=None, order_id=None)
        self.assertTrue(self.c._execution_matches_order(self.cycle, row, "BUY"))

    def test_exact_permanent_id_recovers_missing_reference(self):
        self.assertTrue(self.c._execution_matches_order(self.cycle, self.event(order_ref=""), "BUY"))

    def test_numeric_order_id_alone_is_ambiguous(self):
        self.assertFalse(self.c._execution_matches_order(self.cycle, self.event(order_ref="", perm_id=None), "BUY"))

    def test_superseded_order_uses_its_own_permanent_identity(self):
        old = "IBKRBOT|AAPL|C1|OLD|SELL"
        self.cycle.sell_order_ref, self.cycle.sell_perm_id = "NEW_SELL", 44
        self.save()
        self.c.storage.add_order(cycle=self.cycle, action="SELL", order_type="TRAIL", order_ref=old,
                                 order_id=33, perm_id=77, quantity=10, trailing_percent=1.0,
                                 initial_stop_price=105.0, status="Cancelled")
        row = self.event(order_ref=old, side="SLD", perm_id=77)
        self.assertTrue(self.c._execution_matches_order(self.cycle, row, "SELL"))
        self.assertFalse(self.c._execution_matches_order(self.cycle, {**row, "perm_id": 88}, "SELL"))

    def test_conflicting_callback_is_not_projected(self):
        self.apply(self.event(account="DU_OTHER"))
        self.assertIsNone(self.c.storage.get_execution("EXEC_1"))
        self.assertEqual(self.cycle.buy_filled_qty, 0)

    def test_known_older_permanent_id_on_reused_ref_retains_late_fill(self):
        self.c.storage.add_order(cycle=self.cycle, action="BUY", order_type="TRAIL",
                                 order_ref=self.cycle.buy_order_ref, order_id=8, perm_id=9,
                                 quantity=10, trailing_percent=1.0, initial_stop_price=100.0,
                                 status="Cancelled")
        self.apply(self.event(order_id=8, perm_id=9, shares=2.0))
        self.assertEqual(self.cycle.buy_filled_qty, 2)
        self.assertIsNotNone(self.c.storage.get_execution("EXEC_1"))
        self.assertFalse(self.c._execution_matches_order(self.cycle, self.event(perm_id=999), "BUY"))

    def test_missing_reference_can_use_permanent_id_proven_in_legacy_order_row(self):
        self.c.storage.add_order(cycle=self.cycle, action="BUY", order_type="TRAIL",
                                 order_ref=self.cycle.buy_order_ref, order_id=11, perm_id=22,
                                 quantity=10, trailing_percent=1.0, initial_stop_price=100.0,
                                 status="Filled")
        self.cycle.buy_perm_id = None
        self.assertTrue(self.c._execution_matches_order(self.cycle, self.event(order_ref=""), "BUY"))
        self.assertFalse(self.c._execution_matches_order(self.cycle, self.event(order_ref="", perm_id=999), "BUY"))

    def test_negative_buy_fee_is_not_cancelled_by_placeholder(self):
        self.c._handle_buy_order_poll(self.cycle, self.poll(-0.5))
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "BUY")["commission"], -0.5)
        self.assertEqual(self.c.active_cycle.buy_commission, -0.5)

    def test_authoritative_corrections_include_zero_and_negative_and_are_idempotent(self):
        self.apply(self.event(commission=2.0))
        for fee in (1.0, 0.0, -0.5, -0.5):
            with self.subTest(fee=fee):
                self.apply(self.event(commission=fee), "COMMISSION_REPORT")
                self.assertEqual(self.cycle.buy_commission, fee)
                self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "BUY")["shares"], 10)
        self.apply(self.event(commission=2.0))
        self.assertEqual(self.cycle.buy_commission, -0.5)

    def test_pending_zero_commission_overrides_nonzero_execution_placeholder(self):
        self.apply(self.event(shares=0, price=0, commission=0.0), "COMMISSION_REPORT")
        self.apply(self.event(commission=2.0))
        self.assertEqual(self.cycle.buy_commission, 0.0)

    def test_signed_partial_fee_does_not_create_opposite_residual_fee(self):
        polled = self.poll(0.0, executions=False)
        polled.filled = 20
        self.c._record_polled_executions(self.cycle, polled, "BUY")
        self.apply(self.event(commission=-0.5))
        totals = self.c.storage.get_execution_totals(self.cycle.id, "BUY")
        self.assertEqual(totals["shares"], 20)
        self.assertEqual(totals["commission"], -0.5)

    def test_final_zero_fee_clears_pending_aggregate_fee_without_losing_shares(self):
        self.c._record_polled_executions(self.cycle, self.poll(1.0, executions=False), "BUY")
        self.apply(self.event())
        self.assertEqual(self.cycle.buy_commission, 1.0)
        self.apply(self.event(), "COMMISSION_REPORT")
        self.assertEqual(self.cycle.buy_commission, 0.0)
        self.assertEqual(self.cycle.buy_filled_qty, 10)

    def test_foreign_buy_fee_is_excluded_from_ledger_and_cycle(self):
        self.c._handle_buy_order_poll(self.cycle, self.poll(2.0, currency="EUR"))
        self.assertEqual(self.c.active_cycle.buy_commission, 0.0)
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "BUY")["commission"], 0.0)
        self.assertTrue(self.c.active_cycle.stop_after_current_cycle)

    def test_late_foreign_currency_clears_unattributed_aggregate_fee(self):
        polled = self.poll(2.0, executions=False)
        polled.raw = {}
        self.c._record_polled_executions(self.cycle, polled, "BUY")
        self.c._reconcile_cycle_execution_ledger(self.cycle.id)
        self.cycle = self.c.active_cycle
        self.assertEqual(self.cycle.buy_commission, 2.0)
        polled.raw = {"commission_currencies": ["EUR"]}
        self.c._handle_buy_order_poll(self.cycle, polled)
        self.assertEqual(self.c.active_cycle.buy_commission, 0.0)
        self.assertEqual(self.c.storage.get_execution_totals(self.cycle.id, "BUY")["shares"], 10)

    def test_foreign_authoritative_report_clears_provisional_fee(self):
        self.apply(self.event(commission=2.0))
        self.apply(self.event(commission=3.0, currency="EUR"), "COMMISSION_REPORT")
        self.assertEqual(self.cycle.buy_commission, 0.0)
        self.assertTrue(self.cycle.stop_after_current_cycle)

    def test_recovery_respects_authoritative_zero_over_stale_nonzero_fee(self):
        self.apply(self.event(commission=2.0))
        self.apply(self.event(commission=0.0), "COMMISSION_REPORT")
        self.broker.executions = [self.event(commission=2.0)]
        self.cycle.buy_commission = 2.0
        self.save()
        recovered = self.c._recover_buy_from_executions(self.cycle)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.buy_filled_qty, 10)
        self.assertEqual(recovered.buy_commission, 0.0)

    def test_legacy_non_mapping_execution_raw_does_not_break_recovery(self):
        self.apply(self.event(commission=0.5))
        with self.c.storage.connect() as con:
            con.execute("UPDATE executions SET raw_json='[]' WHERE execution_id='EXEC_1'")
        self.broker.executions = [self.event(commission=0.5)]
        quantity, _price, commission, _rows = self.c._aggregate_recovered_executions(self.cycle, "BUY")
        self.assertEqual(quantity, 10)
        self.assertEqual(commission, 0.5)

    def test_normal_sell_completion_accepts_corrected_zero_fee(self):
        self.cycle.stage = Stage.SELL_TRAIL_ACTIVE
        self.cycle.buy_filled_qty, self.cycle.avg_buy_price = 10, 100.0
        self.cycle.buy_status = "Filled"
        self.cycle.sell_order_ref, self.cycle.sell_status = "OWN_SELL", "Submitted"
        self.cycle.sell_commission = 2.0
        self.save()
        self.c.storage.upsert_execution(cycle=self.cycle, ticker="AAPL", side="SELL", shares=10,
                                        price=110, commission=0.0, commission_authoritative=True,
                                        order_ref="OWN_SELL", execution_id="SELL_1")
        polled = PolledOrderState("OWN_SELL", 33, 44, "Filled", 10, 0, 110, 0.0,
                                 [{"execId": "SELL_1", "shares": 10, "price": 110, "commission": 0.0}], {})
        self.c._handle_sell_order_poll(self.cycle, polled)
        self.assertEqual(self.c.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(self.c.active_cycle.sell_commission, 0.0)
        self.assertEqual(self.c.active_cycle.net_pnl, 100.0)

    def test_completed_sell_corrections_update_net_pnl_without_changing_quantity(self):
        self.cycle.stage = Stage.CYCLE_COMPLETE
        self.cycle.buy_filled_qty = self.cycle.sell_filled_qty = 10
        self.cycle.avg_buy_price, self.cycle.avg_sell_price = 100.0, 110.0
        self.cycle.buy_status = self.cycle.sell_status = "Filled"
        self.cycle.sell_order_ref = "OWN_SELL"
        self.save()
        event = self.event(order_ref="OWN_SELL", execution_id="SELL_1", side="SLD", price=110.0,
                           order_id=None, perm_id=None, commission=2.0)
        self.apply(event)
        for fee in (-0.5, 0.0, 0.0):
            self.apply({**event, "commission": fee}, "COMMISSION_REPORT")
            self.assertEqual(self.cycle.net_pnl, 100.0 - fee)
            self.assertEqual(self.cycle.sell_filled_qty, 10)
            self.assertEqual(self.cycle.stage, Stage.CYCLE_COMPLETE)

    def test_mixed_signed_fees_preserve_actual_sum(self):
        self.apply(self.event(execution_id="BUY_A", shares=4.0, commission=-0.5))
        self.apply(self.event(execution_id="BUY_B", shares=6.0, commission=1.0))
        self.assertEqual(self.cycle.buy_filled_qty, 10)
        self.assertEqual(self.cycle.buy_commission, 0.5)

    def test_protective_fee_uses_same_signed_currency_checked_ledger(self):
        for currency, fee, expected in (("USD", -0.5, -0.5), ("EUR", 2.0, 0.0)):
            with self.subTest(currency=currency):
                self.cycle.stage = Stage.WAIT_RISE_TRIGGER
                self.cycle.buy_filled_qty, self.cycle.avg_buy_price = 10, 100.0
                self.cycle.protective_sell_order_ref = "IBKRBOT|AAPL|C1|PROTECTIVE_SELL"
                self.cycle.protective_sell_status = "Submitted"
                self.save()
                polled = self.poll(fee, currency=currency, role="PROTECTIVE_SELL")
                polled.executions[0]["execId"] = currency
                # Separate temporary cycle identities prevent cross-case ledger mixing.
                self.c._handle_protective_sell_order_poll(self.cycle, polled)
                self.assertEqual(self.c.active_cycle.protective_sell_commission, expected)
                self.assertEqual(self.c.active_cycle.sell_commission, expected)
                if currency == "USD":
                    self.cycle = StrategyEngine.start_cycle(self.settings, 2, "DU_TEST", 100.0, 0.0)
                    self.cycle.con_id = 123


if __name__ == "__main__":
    unittest.main()
