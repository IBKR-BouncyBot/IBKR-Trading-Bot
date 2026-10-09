"""Replay sanitized real broker shapes through adapter, recovery, and SQLite.

The JSON preserves numeric/order-status shapes from six supplied archives. Named
variants deliberately alter delivery order, session cache, or evidence identity;
they are controlled fault injection, not additional observed live incidents.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ib_adapter import IbAsyncTwsAdapter
from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "v562_audit_recovery_cases.json").read_text())
_COMPLETE = [case for case in _FIXTURE["cases"] if case["kind"] == "observed_complete"]
_SELL = [case for case in _COMPLETE if case["trade"]["order"]["action"] == "SELL"]
_SUCCESS_VARIANTS = (
    "observed", "completed_zero_summary", "new_session_order_id_zero",
    "stale_working_cache", "absent_cache", "duplicate_snapshot",
    "reverse_snapshot", "repeated_reconnect", "fee_before_fill", "fee_after_fill",
)
_FAULT_VARIANTS = (
    "wrong_account", "wrong_contract", "wrong_side", "wrong_perm_id",
    "wrong_reference", "missing_execution_id", "conflicting_duplicate_shares",
    "conflicting_duplicate_price", "zero_shares", "negative_shares",
    "fractional_shares", "empty_execution_snapshot",
)


def _object(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _object(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_object(item) for item in value]
    return value


def _trade(case):
    trade = _object(deepcopy(case["trade"]))
    for fill in trade.fills:
        for obj, name in ((fill, "time"), (fill.execution, "time")):
            value = getattr(obj, name, None)
            if isinstance(value, str):
                setattr(obj, name, datetime.fromisoformat(value))
    return trade


class MultiAuditRecoveryReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "app._v562_multi_audit_controller", Path(__file__).parents[1] / "app" / "controller.py",
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(cls.module)

    def build(self, case, *, auto_repeat=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        broker = DeterministicBrokerAdapter()
        broker.accounts = ["SIM"]
        broker.contract.currency = case["trade"]["contract"]["currency"]
        broker.qualify_stock = Mock(return_value=broker.contract)
        settings = permissive_strategy(auto_repeat=auto_repeat)
        settings.contract_con_id = broker.contract.con_id
        settings.currency = case["trade"]["contract"]["currency"]
        controller = make_controller(self.module, Path(folder.name) / "state.sqlite", broker, settings)
        self.addCleanup(controller._market_capture.shutdown)
        controller._start_trade_market_data_capture = Mock()
        controller._queue_database_backup = Mock()
        trade = _trade(case)
        quantity = case["quantity"]
        price = float(trade.fills[0].execution.price) if trade.fills else 100.0
        cycle = StrategyEngine.start_cycle(settings, 1, "SIM", price, 0.0)
        cycle.id = f"SIM-CYCLE-{int(case['id'].split('-')[1]):03d}"
        cycle.con_id = 123
        cycle.quantity = quantity
        action = trade.order.action
        if action == "BUY":
            cycle.stage = Stage.BUY_TRAIL_ACTIVE
            cycle.buy_order_ref = trade.order.orderRef
            cycle.buy_order_id = 10000 + int(case["id"].split("-")[1])
            cycle.buy_perm_id = trade.order.permId
            cycle.buy_status = "PreSubmitted"
            broker.external_position = quantity
        else:
            cycle.stage = Stage.SELL_TRAIL_ACTIVE
            cycle.buy_filled_qty = quantity
            cycle.avg_buy_price = price + 10.0
            cycle.buy_commission = 1.25
            cycle.buy_status = "Filled"
            cycle.sell_order_ref = trade.order.orderRef
            cycle.sell_order_id = 10000 + int(case["id"].split("-")[1])
            cycle.sell_perm_id = trade.order.permId
            cycle.sell_status = "PreSubmitted"
        controller.active_cycle = cycle
        controller.storage.upsert_cycle(cycle)
        controller.storage.add_order(
            cycle=cycle, action=action, order_type=trade.order.orderType,
            order_id=10000 + int(case["id"].split("-")[1]), perm_id=trade.order.permId,
            order_ref=trade.order.orderRef, quantity=quantity, trailing_percent=0.3,
            initial_stop_price=price, status="PreSubmitted",
        )
        adapter = IbAsyncTwsAdapter()
        return controller, broker, cycle, trade, adapter

    def recover(self, controller, broker, cycle, state, rows, *, open_orders=None):
        broker.executions = rows
        broker.poll_order = Mock(return_value=state)
        broker.recovery_open_app_orders = Mock(return_value=open_orders or [])
        self.assertTrue(controller._recover_after_connect())
        self.assertEqual(broker.placed_orders, [], "Recovery may not submit a replacement order.")
        self.assertEqual(broker.cancelled_orders, [], "Recovery may not cancel an unrelated or settled order.")
        return controller.storage.get_cycle(cycle.id)

    def replay_success(self, case, variant):
        controller, broker, cycle, trade, adapter = self.build(case)
        expected_quantity = case["quantity"]
        expected_value = sum(fill.execution.shares * fill.execution.price for fill in trade.fills)
        expected_commission = sum(fill.commissionReport.commission for fill in trade.fills)
        rows = adapter._execution_rows_from_fills(trade.fills, strict=True)
        self.assertEqual(len(rows), len(trade.fills))
        action = trade.order.action
        if variant in {"completed_zero_summary", "new_session_order_id_zero"}:
            trade.orderStatus.filled = trade.orderStatus.avgFillPrice = 0
            trade.orderStatus.remaining = 0
            trade.order.filledQuantity = expected_quantity
            if variant == "new_session_order_id_zero":
                trade.order.orderId = trade.orderStatus.orderId = trade.orderStatus.permId = 0
                if hasattr(trade.order, "totalQuantity"):
                    del trade.order.totalQuantity
        if variant == "stale_working_cache":
            trade.orderStatus.status = "PreSubmitted"
            trade.orderStatus.filled = trade.orderStatus.avgFillPrice = 0
            trade.orderStatus.remaining = expected_quantity
            trade.fills = []
        state = adapter._to_polled_order_state(trade)
        if variant == "absent_cache":
            state = None
        if variant == "duplicate_snapshot":
            rows += deepcopy(rows)
        if variant == "reverse_snapshot":
            rows.reverse()
        if variant == "fee_before_fill":
            for row in rows:
                controller._apply_execution_callback_event("COMMISSION_REPORT", deepcopy(row), cycle)
        if variant == "fee_after_fill":
            rows = [{**row, "commission": 0.0} for row in rows]
            # Both request snapshot and cached Trade lack the delayed fees.
            for fill in trade.fills:
                fill.commissionReport.commission = 0.0
            state = adapter._to_polled_order_state(trade)
        recovered = self.recover(controller, broker, cycle, state, rows)
        if variant == "fee_after_fill":
            original_rows = adapter._execution_rows_from_fills(_trade(case).fills, strict=True)
            for row in original_rows:
                controller._apply_execution_callback_event("COMMISSION_REPORT", row, recovered)
            recovered = controller.storage.get_cycle(cycle.id)
        expected_stage = Stage.WAIT_RISE_TRIGGER if action == "BUY" else Stage.CYCLE_COMPLETE
        self.assertEqual(recovered.stage, expected_stage)
        quantity = recovered.buy_filled_qty if action == "BUY" else recovered.sell_filled_qty
        average = recovered.avg_buy_price if action == "BUY" else recovered.avg_sell_price
        commission = recovered.buy_commission if action == "BUY" else recovered.sell_commission
        self.assertEqual(quantity, expected_quantity)
        self.assertAlmostEqual(average, expected_value / expected_quantity)
        self.assertAlmostEqual(commission, expected_commission)
        totals = controller.storage.get_execution_totals(cycle.id, action)
        self.assertEqual(totals["shares"], expected_quantity)
        self.assertAlmostEqual(totals["commission"], expected_commission)
        ledger = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
        ledger = [row for row in ledger if row["side"] == action]
        self.assertEqual(len(ledger), len(case["trade"]["fills"]))
        self.assertEqual(len({row["execution_id"] for row in ledger}), len(ledger))
        self.assertFalse(recovered.recovery_required)
        if action == "SELL":
            self.assertAlmostEqual(recovered.net_pnl, expected_value - cycle.avg_buy_price * expected_quantity - 1.25 - expected_commission)
        if variant == "repeated_reconnect":
            for _ in range(3):
                self.assertTrue(controller._recover_after_connect())
            after = controller.storage.get_cycle(cycle.id)
            self.assertEqual(after.stage, expected_stage)
            self.assertEqual(controller.storage.get_execution_totals(cycle.id, action), totals)
            self.assertEqual(controller.storage.get_next_cycle_number("AAPL"), 2)
            self.assertEqual(broker.placed_orders, [])
            self.assertEqual(broker.cancelled_orders, [])

    def replay_fault(self, case, variant):
        controller, broker, cycle, trade, adapter = self.build(case)
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = trade.orderStatus.avgFillPrice = trade.orderStatus.remaining = 0
        trade.order.filledQuantity = case["quantity"]
        # Make every matching copy contradictory; a separate valid cache must not
        # accidentally hide the fault under test.
        if variant == "wrong_account":
            for fill in trade.fills:
                fill.execution.acctNumber = "SIM_OTHER"
        elif variant == "wrong_contract":
            for fill in trade.fills:
                fill.contract.conId = 456
        elif variant == "wrong_side":
            for fill in trade.fills:
                fill.execution.side = "BOT"
        elif variant == "wrong_perm_id":
            for fill in trade.fills:
                fill.execution.permId = 888888
        elif variant == "wrong_reference":
            for fill in trade.fills:
                fill.execution.orderRef = "IBKRBOT|AAPL|OTHER-CYCLE|SELL_TRAIL"
        elif variant == "missing_execution_id":
            for fill in trade.fills:
                fill.execution.execId = ""
                fill.commissionReport.execId = ""
        elif variant.startswith("conflicting_duplicate"):
            duplicate = deepcopy(trade.fills[0])
            if variant.endswith("shares"):
                duplicate.execution.shares += 1
            else:
                duplicate.execution.price += 1
            trade.fills.append(duplicate)
        elif variant in {"zero_shares", "negative_shares", "fractional_shares"}:
            for fill in trade.fills:
                fill.execution.shares = {"zero_shares": 0, "negative_shares": -1, "fractional_shares": 0.5}[variant]
        elif variant == "empty_execution_snapshot":
            trade.fills = []
        state = adapter._to_polled_order_state(trade)
        broker.poll_order = Mock(return_value=state)
        broker.recovery_open_app_orders = Mock(return_value=[])
        broker.recovery_recent_executions = lambda: adapter._execution_rows_from_fills(trade.fills, strict=True)
        complete = controller._recover_after_connect()
        recovered = controller.storage.get_cycle(cycle.id)
        if variant in {"conflicting_duplicate_shares", "conflicting_duplicate_price", "zero_shares", "negative_shares"}:
            self.assertFalse(complete)
            self.assertTrue(controller._upstream_recovery_pending)
            self.assertEqual(recovered.stage, Stage.SELL_TRAIL_ACTIVE)
            self.assertFalse(recovered.recovery_required)
            self.assertEqual(recovered.sell_filled_qty, 0)
            self.assertEqual(controller.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)
            self.assertEqual(broker.placed_orders, [])
            self.assertEqual(broker.cancelled_orders, [])
            # A later complete, consistent snapshot can recover without an
            # operator clearing a permanent hold or sending another SELL.
            broker.recovery_recent_executions = lambda: adapter._execution_rows_from_fills(_trade(case).fills, strict=True)
            self.assertTrue(controller._recover_after_connect())
            recovered = controller.storage.get_cycle(cycle.id)
            self.assertEqual(recovered.stage, Stage.CYCLE_COMPLETE)
            self.assertEqual(recovered.sell_filled_qty, case["quantity"])
            self.assertEqual(broker.placed_orders, [])
            self.assertEqual(broker.cancelled_orders, [])
            return
        self.assertTrue(complete)
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(broker.cancelled_orders, [])
        self.assertEqual(recovered.stage, Stage.MANUAL_REVIEW)
        self.assertTrue(recovered.recovery_required)
        self.assertEqual(recovered.sell_filled_qty, 0)
        self.assertEqual(controller.storage.get_execution_totals(cycle.id, "SELL")["shares"], 0)
        self.assertNotEqual(controller._recovery_confidence(), "fully_reconciled")

    def replay_working_partial(self, case):
        controller, broker, cycle, trade, adapter = self.build(case)
        quantity = case["quantity"]
        first = trade.fills[0]
        first.execution.shares = max(1, quantity // 2)
        first.execution.cumQty = first.execution.shares
        trade.fills = [first]
        trade.orderStatus.status = "Submitted"
        trade.orderStatus.filled = first.execution.shares
        trade.orderStatus.remaining = quantity - first.execution.shares
        trade.orderStatus.avgFillPrice = first.execution.price
        state = adapter._to_polled_order_state(trade)
        rows = adapter._execution_rows_from_fills(trade.fills, strict=True)
        recovered = self.recover(controller, broker, cycle, state, rows, open_orders=[state])
        self.assertEqual(recovered.stage, Stage.SELL_TRAIL_ACTIVE)
        self.assertFalse(recovered.recovery_required)
        self.assertEqual(state.remaining, quantity - first.execution.shares)

    def replay_cancel_fill_race(self, case):
        controller, broker, cycle, trade, adapter = self.build(case)
        rows = adapter._execution_rows_from_fills(trade.fills, strict=True)
        trade.orderStatus.status = "PendingCancel"
        trade.orderStatus.filled = 0
        trade.orderStatus.remaining = case["quantity"]
        trade.fills = []
        state = adapter._to_polled_order_state(trade)
        recovered = self.recover(controller, broker, cycle, state, rows)
        self.assertEqual(recovered.stage, Stage.CYCLE_COMPLETE)
        self.assertEqual(recovered.sell_filled_qty, case["quantity"])
        self.assertEqual(controller.storage.get_execution_totals(cycle.id, "SELL")["shares"], case["quantity"])

    def replay_auto_repeat(self, case):
        controller, broker, cycle, trade, adapter = self.build(case, auto_repeat=True)
        rows = adapter._execution_rows_from_fills(trade.fills, strict=True)
        state = adapter._to_polled_order_state(trade)
        recovered = self.recover(controller, broker, cycle, state, rows)
        self.assertEqual(recovered.stage, Stage.CYCLE_COMPLETE)
        for _ in range(3):
            self.assertTrue(controller._recover_after_connect())
        self.assertEqual(controller.storage.get_next_cycle_number("AAPL"), 3)
        self.assertEqual(controller.active_cycle.stage, Stage.WAIT_INITIAL_DROP)
        self.assertEqual(controller.active_cycle.cycle_number, 2)
        self.assertEqual(broker.placed_orders, [])
        self.assertEqual(broker.cancelled_orders, [])
        self.assertEqual(controller.storage.get_execution_totals(cycle.id, "SELL")["shares"], case["quantity"])

    def replay_observed_nonterminal(self, case):
        controller, broker, cycle, trade, adapter = self.build(case)
        state = adapter._to_polled_order_state(trade)
        self.assertEqual(state.status, case["trade"]["orderStatus"]["status"])
        self.assertEqual(state.filled, case["trade"]["orderStatus"].get("filled", 0))
        self.assertEqual(state.remaining, case["trade"]["orderStatus"].get("remaining", 0))
        self.assertFalse(state.raw["filled_from_executions"])
        if case["kind"] == "observed_working":
            recovered = self.recover(controller, broker, cycle, state, adapter._execution_rows_from_fills(trade.fills, strict=True), open_orders=[state])
            self.assertEqual(recovered.stage, cycle.stage)
            self.assertFalse(recovered.recovery_required)

    def test_fixture_provenance_and_anonymization(self):
        self.assertEqual(len(_FIXTURE["archives"]), 6)
        self.assertEqual(len(_COMPLETE), 15)
        self.assertEqual({case["trade"]["contract"]["currency"] for case in _COMPLETE}, {"EUR", "USD"})
        self.assertTrue(any(len(case["trade"]["fills"]) > 1 for case in _COMPLETE))
        for case in _FIXTURE["cases"]:
            self.assertEqual(case["trade"]["order"]["account"], "SIM")
            self.assertEqual(case["trade"]["contract"]["conId"], 123)
            for fill in case["trade"]["fills"]:
                self.assertEqual(fill["execution"]["acctNumber"], "SIM")
                self.assertTrue(fill["execution"]["execId"].startswith("SIM-EXEC-"))


def _install(name, function, case, *args):
    def test(self):
        function(self, case, *args)
    test.__name__ = name
    test.__doc__ = f"{case['archive']} / {case['id']} / {name.rsplit('__', 1)[-1]}"
    setattr(MultiAuditRecoveryReplayTests, name, test)


for _case in _COMPLETE:
    for _variant in _SUCCESS_VARIANTS:
        _install(f"test_{_case['id'].replace('-', '_')}__{_variant}", MultiAuditRecoveryReplayTests.replay_success, _case, _variant)
for _case in _SELL:
    for _variant in _FAULT_VARIANTS:
        _install(f"test_{_case['id'].replace('-', '_')}__{_variant}", MultiAuditRecoveryReplayTests.replay_fault, _case, _variant)
    for _variant, _method in (
        ("working_partial_remainder", MultiAuditRecoveryReplayTests.replay_working_partial),
        ("pending_cancel_full_fill_race", MultiAuditRecoveryReplayTests.replay_cancel_fill_race),
        ("auto_repeat_repeated_reconnect_once", MultiAuditRecoveryReplayTests.replay_auto_repeat),
    ):
        _install(f"test_{_case['id'].replace('-', '_')}__{_variant}", _method, _case)
for _case in _FIXTURE["cases"]:
    if _case["kind"] != "observed_complete":
        _install(f"test_{_case['id'].replace('-', '_')}__preserved", MultiAuditRecoveryReplayTests.replay_observed_nonterminal, _case)
