"""Manual close waits for confirmed broker quantities before replacing orders."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.ib_adapter import BrokerAdapterError
from app.models import Stage, StopAction
from app.strategy import StrategyEngine, make_order_ref
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter


class PendingCancelBroker(DeterministicBrokerAdapter):
    def cancel_order(self, order_ref, order_id=None):
        self._require_ready("cancel")
        state = self.orders[order_ref]
        if order_id is not None and order_id != state.order_id:
            raise BrokerAdapterError("Wrong order ID")
        self.cancelled_orders.append(order_ref)
        self.orders[order_ref] = replace(state, status="PendingCancel")

    def confirm_cancel(self, order_ref, status="Cancelled"):
        self.orders[order_ref] = replace(self.orders[order_ref], status=status)
        return self.poll_order(order_ref)


class ManualCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = "app._v510_manual_close_controller"
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "app" / "controller.py")
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.module
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)
        cls.addClassCleanup(sys.modules.pop, name, None)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.sqlite"
        self.broker = PendingCancelBroker()
        self.settings = permissive_strategy()
        self.settings.contract_con_id = self.broker.contract.con_id
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)

    def controller(self):
        c = make_controller(self.module, self.path, self.broker, self.settings)
        c.connection.account = "SIM"
        c.storage.backup_database = lambda *args, **kwargs: None
        c._start_trade_market_data_capture = lambda *args, **kwargs: None
        publish_fresh_price(c, self.broker, 104.0)
        return c

    def buy(self, filled=4, protective=False):
        self.settings.protective_sell_enabled = protective
        self.settings.protective_sell_trailing_stop_pct = 4.0
        cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100.0, 0.0)
        cycle.con_id = self.broker.contract.con_id
        cycle.stage = Stage.BUY_TRAIL_ACTIVE
        cycle.quantity = 10
        cycle.buy_order_ref = make_order_ref(cycle.ticker, 1, cycle.id, "BUY_TRAIL")
        handle = self.broker.place_trailing_stop(
            contract=self.broker.contract, action="BUY", quantity=10,
            trailing_percent=1.0, initial_stop_price=101.0,
            order_ref=cycle.buy_order_ref, account="SIM",
        )
        cycle.buy_order_id, cycle.buy_perm_id, cycle.buy_status = handle.order_id, handle.perm_id, handle.status
        self.c.active_cycle = cycle
        self.c.storage.upsert_cycle(cycle)
        self.c.storage.add_order(
            cycle=cycle, action="BUY", order_type="TRAIL", order_id=handle.order_id,
            perm_id=handle.perm_id, order_ref=handle.order_ref, quantity=10,
            trailing_percent=1.0, initial_stop_price=101.0, status=handle.status,
        )
        if filled:
            polled = self.broker.fill_order(handle.order_ref, shares=filled, price=100.0, execution_id="BUY-FIRST")
            self.broker.events.clear()
            self.c._handle_buy_order_poll(cycle, polled)
        self.broker.events.clear()
        return self.c.active_cycle

    def sell(self, role, filled=0):
        cycle = self.buy(10)
        prefix = "protective_sell" if role == "PROTECTIVE_SELL" else "sell"
        cycle.stage = Stage.WAIT_RISE_TRIGGER if role == "PROTECTIVE_SELL" else Stage.SELL_TRAIL_ACTIVE
        ref = make_order_ref(cycle.ticker, 1, cycle.id, role + "_TRAIL")
        handle = self.broker.place_trailing_stop(
            contract=self.broker.contract, action="SELL", quantity=10,
            trailing_percent=1.0, initial_stop_price=103.0, order_ref=ref, account="SIM",
        )
        setattr(cycle, prefix + "_order_ref", ref)
        setattr(cycle, prefix + "_order_id", handle.order_id)
        setattr(cycle, prefix + "_perm_id", handle.perm_id)
        setattr(cycle, prefix + "_status", handle.status)
        if role == "PROTECTIVE_SELL":
            cycle.protective_sell_enabled = True
        self.c.storage.upsert_cycle(cycle)
        self.c.storage.add_order(
            cycle=cycle, action=role, order_type="TRAIL", order_id=handle.order_id,
            perm_id=handle.perm_id, order_ref=ref, quantity=10, trailing_percent=1.0,
            initial_stop_price=103.0, status=handle.status,
        )
        if filled:
            polled = self.broker.fill_order(ref, shares=filled, price=104.0, execution_id="SELL-FIRST", terminal=False)
            self.broker.events.clear()
            self.poll_sell(role, polled)
        self.broker.events.clear()
        return ref

    def poll_sell(self, role, polled):
        handler = self.c._handle_protective_sell_order_poll if role == "PROTECTIVE_SELL" else self.c._handle_sell_order_poll
        handler(self.c.active_cycle, polled)

    def request_close(self):
        self.c._apply_stop_action(StopAction.SELL_APP_POSITION_MARKET)

    def markets(self):
        return [row for row in self.broker.placed_orders if row["order_type"] == "MKT"]

    def test_partial_buy_waits_and_closes_final_cumulative_quantity(self):
        cycle = self.buy(4, protective=True)
        self.request_close()
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertFalse(self.markets())
        self.broker.fill_order(cycle.buy_order_ref, shares=2, price=100.0, execution_id="BUY-LATE", terminal=False)
        self.c._drain_broker_events()
        self.assertEqual(self.c.active_cycle.buy_filled_qty, 6)
        self.assertFalse(self.markets())
        polled = self.broker.confirm_cancel(cycle.buy_order_ref)
        self.c._handle_buy_order_poll(self.c.active_cycle, polled)
        self.assertEqual([row["quantity"] for row in self.markets()], [6])
        self.assertIsNone(self.c.active_cycle.protective_sell_order_ref)
        self.assertTrue(self.c.active_cycle.protective_sell_enabled)
        self.assertEqual(self.c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)

    def test_failed_buy_cancel_keeps_monitoring_until_filled(self):
        cycle = self.buy(4)
        self.broker.fail_operations.add("cancel")
        self.request_close()
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertFalse(self.markets())
        polled = self.broker.fill_order(cycle.buy_order_ref, shares=6, price=100.0, execution_id="BUY-LATE")
        self.broker.events.clear()
        self.c._handle_buy_order_poll(self.c.active_cycle, polled)
        self.assertEqual([row["quantity"] for row in self.markets()], [10])

    def test_unfilled_buy_does_not_stop_before_cancel_confirmation(self):
        cycle = self.buy(0)
        self.request_close()
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertTrue(self.c.active_cycle.close_position_market_requested)
        self.c._handle_buy_order_poll(self.c.active_cycle, self.broker.confirm_cancel(cycle.buy_order_ref))
        self.assertEqual(self.c.active_cycle.stage, Stage.STOPPED)
        self.assertFalse(self.c.active_cycle.close_position_market_requested)
        self.assertFalse(self.markets())

    def test_no_fill_cancel_recovered_after_restart_stops_without_new_buy(self):
        cycle = self.buy(0)
        self.request_close()
        self.broker.confirm_cancel(cycle.buy_order_ref)
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        self.assertEqual(self.c.active_cycle.stage, Stage.STOPPED)
        self.assertFalse(self.markets())

    def test_partial_buy_restart_continues_persisted_close(self):
        cycle = self.buy(4, protective=True)
        self.request_close()
        self.broker.confirm_cancel(cycle.buy_order_ref)
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        self.assertEqual([row["quantity"] for row in self.markets()], [4])
        self.assertEqual(self.c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)

    def test_missing_buy_terminal_evidence_on_restart_fails_closed(self):
        cycle = self.buy(4)
        self.request_close()
        del self.broker.orders[cycle.buy_order_ref]
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertFalse(self.markets())

    def test_pending_buy_close_survives_restart_until_cancel_arrives(self):
        cycle = self.buy(4, protective=True)
        self.request_close()
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertFalse(self.markets())
        self.broker.confirm_cancel(cycle.buy_order_ref)
        self.c._run_strategy_cycle()
        self.assertEqual([row["quantity"] for row in self.markets()], [4])

    def test_zero_fill_buy_can_fill_during_pending_cancel(self):
        cycle = self.buy(0)
        self.request_close()
        polled = self.broker.fill_order(cycle.buy_order_ref, shares=3, price=100.0, execution_id="BUY-LATE", terminal=False)
        self.broker.events.clear()
        self.c._handle_buy_order_poll(self.c.active_cycle, polled)
        self.assertEqual(self.c.active_cycle.stage, Stage.BUY_TRAIL_ACTIVE)
        self.assertFalse(self.markets())
        self.c._handle_buy_order_poll(self.c.active_cycle, self.broker.confirm_cancel(cycle.buy_order_ref))
        self.assertEqual([row["quantity"] for row in self.markets()], [3])

    def test_unknown_market_transmission_keeps_single_owned_intent(self):
        cycle = self.buy(4)
        self.request_close()
        with patch.object(self.broker, "place_market_order", side_effect=RuntimeError("unconfirmed send")):
            self.c._handle_buy_order_poll(self.c.active_cycle, self.broker.confirm_cancel(cycle.buy_order_ref))
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertEqual(self.c.active_cycle.sell_status, "SUBMISSION_UNKNOWN")
        self.assertTrue(self.c.active_cycle.sell_order_ref)
        self.c._run_strategy_cycle()
        self.assertFalse(self.markets())

    def test_protective_cancel_confirmation_survives_restart(self):
        ref = self.sell("PROTECTIVE_SELL", 0)
        self.request_close()
        self.broker.confirm_cancel(ref)
        self.broker.external_position = 10
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        self.assertEqual([row["quantity"] for row in self.markets()], [10])

    def test_normal_settled_buy_still_places_protection(self):
        cycle = self.buy(10, protective=True)
        self.assertTrue(cycle.protective_sell_order_ref)
        self.assertEqual(cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(len([row for row in self.broker.placed_orders if row["action"] == "SELL"]), 1)
        self.assertFalse(self.markets())

    def test_final_partial_cancel_replaces_only_unsold_shares(self):
        self.check_partial_cancel("SELL")

    def test_protective_partial_cancel_replaces_only_unsold_shares(self):
        self.check_partial_cancel("PROTECTIVE_SELL")

    def check_partial_cancel(self, role):
        ref = self.sell(role, 4)
        self.request_close()
        self.assertFalse(self.markets())
        self.broker.fill_order(ref, shares=1, price=104.0, execution_id="SELL-LATE", terminal=False)
        self.c._drain_broker_events()
        self.poll_sell(role, self.broker.confirm_cancel(ref))
        original = self.c.storage.get_order_for_cycle_ref(self.c.active_cycle.id, ref)
        self.assertEqual(original["status"], "Cancelled")
        self.assertEqual([row["quantity"] for row in self.markets()], [5])
        self.assertEqual(self.c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
        replacement = self.c.active_cycle.sell_order_ref
        polled = self.broker.fill_order(replacement, shares=5, price=104.0, execution_id="SELL-CLOSE")
        self.broker.events.clear()
        self.c._handle_sell_order_poll(self.c.active_cycle, polled)
        self.assertEqual(self.c.active_cycle.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(self.c.active_cycle.sell_filled_qty, 10)
        self.assertEqual(self.c._app_unsold_quantity(self.c.active_cycle), 0)
        self.assertEqual(len(self.markets()), 1)

    def test_final_cancel_restart_reconciles_offline_partial_fill(self):
        self.check_sell_restart("SELL", complete=False)

    def test_protective_cancel_restart_reconciles_offline_partial_fill(self):
        self.check_sell_restart("PROTECTIVE_SELL", complete=False)

    def test_final_full_fill_during_cancel_restart_sends_no_replacement(self):
        self.check_sell_restart("SELL", complete=True)

    def test_protective_full_fill_during_cancel_restart_sends_no_replacement(self):
        self.check_sell_restart("PROTECTIVE_SELL", complete=True)

    def check_sell_restart(self, role, *, complete):
        ref = self.sell(role, 4)
        self.request_close()
        self.broker.fill_order(ref, shares=6 if complete else 1, price=104.0, execution_id="OFFLINE-SELL", terminal=complete)
        self.broker.events.clear()
        if not complete:
            self.broker.confirm_cancel(ref)
        self.broker.external_position = 0 if complete else 5
        self.c = self.controller()
        self.addCleanup(self.c._market_capture.shutdown)
        self.c._recover_after_connect()
        if complete:
            self.assertEqual(self.c.active_cycle.stage, Stage.CYCLE_COMPLETE)
            self.assertFalse(self.markets())
        else:
            self.assertEqual(self.c.active_cycle.stage, Stage.SELL_TRAIL_ACTIVE)
            self.assertEqual([row["quantity"] for row in self.markets()], [5])

    def test_terminal_partial_forced_replacement_still_pauses(self):
        ref = self.sell("SELL", 4)
        self.request_close()
        self.poll_sell("SELL", self.broker.confirm_cancel(ref))
        replacement = self.c.active_cycle.sell_order_ref
        self.broker.fill_order(replacement, shares=2, price=104.0, execution_id="CLOSE-PARTIAL", terminal=False)
        self.broker.events.clear()
        self.c._handle_sell_order_poll(self.c.active_cycle, self.broker.confirm_cancel(replacement))
        self.assertEqual(self.c.active_cycle.stage, Stage.ERROR)
        self.assertEqual(len(self.markets()), 1)

    def test_unrequested_terminal_partial_remains_error(self):
        ref = self.sell("SELL", 4)
        self.poll_sell("SELL", self.broker.confirm_cancel(ref))
        self.assertEqual(self.c.active_cycle.stage, Stage.ERROR)
        self.assertFalse(self.markets())

    def test_partial_cancel_with_conflicting_order_identity_fails_closed(self):
        ref = self.sell("SELL", 4)
        self.request_close()
        polled = replace(self.broker.confirm_cancel(ref), perm_id=999999)
        self.poll_sell("SELL", polled)
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertFalse(self.markets())

    def test_rejected_original_exit_does_not_authorize_replacement(self):
        ref = self.sell("SELL", 0)
        self.request_close()
        self.poll_sell("SELL", self.broker.confirm_cancel(ref, "Rejected"))
        self.assertEqual(self.c.active_cycle.stage, Stage.ERROR)
        self.assertFalse(self.markets())

    def test_closed_rth_keeps_final_exit(self):
        self.check_closed_rth("SELL")

    def test_closed_rth_keeps_protective_exit(self):
        self.check_closed_rth("PROTECTIVE_SELL")

    def check_closed_rth(self, role):
        ref = self.sell(role, 0)
        self.broker.rth_open = False
        self.request_close()
        self.assertFalse(self.broker.cancelled_orders)
        self.assertEqual(self.broker.orders[ref].status, "Submitted")
        self.assertFalse(self.c.active_cycle.close_position_market_requested)
        self.assertFalse(self.markets())

    def test_open_rth_cancel_confirmation_sends_one_exit(self):
        ref = self.sell("SELL", 0)
        self.request_close()
        self.assertEqual(self.broker.cancelled_orders, [ref])
        self.assertFalse(self.markets())
        self.poll_sell("SELL", self.broker.confirm_cancel(ref))
        self.c._run_strategy_cycle()
        self.assertEqual([row["quantity"] for row in self.markets()], [10])

    def test_rth_closes_after_cancel_request_pauses_without_replacement(self):
        ref = self.sell("SELL", 0)
        self.request_close()
        self.broker.rth_open = False
        self.poll_sell("SELL", self.broker.confirm_cancel(ref))
        self.assertEqual(self.c.active_cycle.stage, Stage.MANUAL_REVIEW)
        self.assertFalse(self.markets())

    def test_invalid_replacement_quantity_keeps_existing_exit(self):
        ref = self.sell("SELL", 1)
        self.broker.contract.size_increment = 2.0
        self.request_close()
        self.assertFalse(self.broker.cancelled_orders)
        self.assertEqual(self.broker.orders[ref].status, "Submitted")
        self.assertFalse(self.markets())


if __name__ == "__main__":
    unittest.main()
