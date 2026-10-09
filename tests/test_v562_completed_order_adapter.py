"""Completed-order execution evidence survives a new local API session."""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from app.ib_adapter import BrokerAdapterError, IbAsyncTwsAdapter


class CompletedOrderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = IbAsyncTwsAdapter()

    @staticmethod
    def fill(exec_id="execution-1", *, shares=12, price=729.80, commission=5.38):
        at = datetime(2026, 10, 8, 7, 4, 24, tzinfo=timezone.utc)
        return SimpleNamespace(
            execution=SimpleNamespace(
                execId=exec_id, acctNumber="DU_TEST", orderRef="IBKRBOT|TEST|C1|SELL",
                orderId=99, permId=321, side="SLD", shares=shares, price=price,
                avgPrice=price, time=at,
            ),
            contract=SimpleNamespace(conId=123, secType="STK", currency="EUR"),
            commissionReport=SimpleNamespace(execId=exec_id, commission=commission, currency="EUR"),
            time=at,
        )

    @classmethod
    def trade(cls, **status_changes):
        status = dict(status="Filled", filled=0, remaining=0, avgFillPrice=0, permId=0)
        status.update(status_changes)
        return SimpleNamespace(
            order=SimpleNamespace(
                orderRef="IBKRBOT|TEST|C1|SELL", orderId=0, permId=321,
                account="DU_TEST", action="SELL", orderType="TRAIL",
                totalQuantity=0, filledQuantity=12,
            ),
            orderStatus=SimpleNamespace(**status),
            contract=SimpleNamespace(conId=123, secType="STK", currency="EUR"),
            fills=[cls.fill()],
        )

    def assert_not_reconstructed(self, trade):
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual(state.filled, 0)
        self.assertFalse(state.raw["filled_from_executions"])

    def test_completed_order_missing_summary_uses_exact_attached_execution(self):
        trade = self.trade()
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual((state.status, state.filled, state.remaining), ("Filled", 12, 0))
        self.assertAlmostEqual(state.avg_fill_price, 729.8)
        self.assertAlmostEqual(state.commission, 5.38)
        self.assertTrue(state.raw["filled_from_executions"])
        self.assertEqual(state.executions[0]["execId"], "execution-1")
        self.assertEqual(trade.orderStatus.filled, 0)

    def test_missing_total_quantity_attribute_uses_corroborated_filled_quantity(self):
        trade = self.trade()
        del trade.order.totalQuantity
        self.assertEqual(self.adapter._to_polled_order_state(trade).filled, 12)

    def test_missing_filled_status_counter_uses_exact_evidence(self):
        trade = self.trade()
        del trade.orderStatus.filled
        self.assertEqual(self.adapter._to_polled_order_state(trade).filled, 12)

    def test_total_quantity_corroborates_without_filled_quantity(self):
        trade = self.trade()
        trade.order.totalQuantity = 12
        del trade.order.filledQuantity
        self.assertEqual(self.adapter._to_polled_order_state(trade).filled, 12)

    def test_weighted_price_and_commissions_use_all_unique_fills(self):
        trade = self.trade()
        trade.fills = [self.fill("one", shares=5, price=700, commission=1),
                       self.fill("two", shares=7, price=730, commission=2)]
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual(state.filled, 12)
        self.assertEqual(state.avg_fill_price, 717.5)
        self.assertEqual(state.commission, 3)
        self.assertEqual(len(state.executions), 2)

    def test_duplicate_execution_id_does_not_double_quantity_or_commission(self):
        trade = self.trade()
        trade.fills.append(deepcopy(trade.fills[0]))
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual(state.filled, 12)
        self.assertEqual(len(state.executions), 1)
        self.assertAlmostEqual(state.commission, 5.38)

    def test_conflicting_duplicate_execution_id_is_not_reconstructed(self):
        for field, value in (("shares", 1), ("price", 800), ("orderId", 98)):
            with self.subTest(field=field):
                trade = self.trade()
                duplicate = deepcopy(trade.fills[0])
                setattr(duplicate.execution, field, value)
                trade.fills.append(duplicate)
                self.assert_not_reconstructed(trade)

    def test_conflicting_duplicate_commission_is_not_reconstructed(self):
        trade = self.trade()
        duplicate = deepcopy(trade.fills[0])
        duplicate.commissionReport.commission = 10
        trade.fills.append(duplicate)
        self.assert_not_reconstructed(trade)

    def test_filled_quantity_without_executions_never_creates_a_fill(self):
        trade = self.trade()
        trade.fills = []
        self.assert_not_reconstructed(trade)

    def test_partial_or_excess_execution_evidence_does_not_prove_full_order(self):
        for shares in (1, 11, 13):
            with self.subTest(shares=shares):
                trade = self.trade()
                trade.fills = [self.fill(shares=shares)]
                self.assert_not_reconstructed(trade)

    def test_contradictory_total_quantity_does_not_fall_back_to_filled_quantity(self):
        for quantity in (11, 13, 12.5, -1, "invalid", float("nan"), float("inf")):
            with self.subTest(quantity=quantity):
                trade = self.trade()
                trade.order.totalQuantity = quantity
                self.assert_not_reconstructed(trade)

    def test_invalid_expected_quantity_does_not_create_fill(self):
        for quantity in (None, 0, -1, 12.5, "invalid", float("nan"), float("inf")):
            with self.subTest(quantity=quantity):
                trade = self.trade()
                trade.order.filledQuantity = quantity
                self.assert_not_reconstructed(trade)

    def test_invalid_execution_shares_or_price_does_not_prove_fill(self):
        for field, values in (
            ("shares", (None, 0, -1, 12.5, "invalid", float("nan"), float("inf"))),
            ("price", (None, 0, -1, "invalid", float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    trade = self.trade()
                    setattr(trade.fills[0].execution, field, value)
                    self.assert_not_reconstructed(trade)

    def test_invalid_commission_does_not_prove_fill(self):
        for commission in ("invalid", float("nan"), float("inf"), 1.7976931348623157e308):
            with self.subTest(commission=commission):
                trade = self.trade()
                trade.fills[0].commissionReport.commission = commission
                self.assert_not_reconstructed(trade)

    def test_execution_requires_exact_ownership_and_nonempty_id(self):
        for field, value in (
            ("execId", ""), ("acctNumber", "OTHER"), ("acctNumber", ""),
            ("orderRef", "OTHER"), ("orderRef", ""), ("permId", 999),
            ("permId", 0), ("side", "BOT"), ("side", ""),
        ):
            with self.subTest(field=field, value=value):
                trade = self.trade()
                setattr(trade.fills[0].execution, field, value)
                self.assert_not_reconstructed(trade)

    def test_execution_requires_exact_stock_contract(self):
        for field, value in (("conId", 999), ("conId", 0), ("secType", "OPT")):
            with self.subTest(field=field):
                trade = self.trade()
                setattr(trade.fills[0].contract, field, value)
                self.assert_not_reconstructed(trade)

    def test_order_identity_contradictions_are_rejected(self):
        for field, value in (("account", ""), ("permId", 999), ("permId", 0), ("orderId", 98)):
            with self.subTest(field=field, value=value):
                trade = self.trade()
                setattr(trade.order, field, value)
                self.assert_not_reconstructed(trade)
        trade = self.trade(permId=999)
        self.assert_not_reconstructed(trade)

    def test_contradictory_alternate_reference_and_commission_id_rejected(self):
        trade = self.trade()
        trade.fills[0].orderRef = "OTHER"
        self.assert_not_reconstructed(trade)
        trade = self.trade()
        trade.fills[0].commissionReport.execId = "OTHER"
        self.assert_not_reconstructed(trade)

    def test_buy_completed_order_uses_buy_side_evidence(self):
        trade = self.trade()
        trade.order.action = "BUY"
        trade.fills[0].execution.side = "BOT"
        self.assertEqual(self.adapter._to_polled_order_state(trade).filled, 12)

    def test_live_or_cancelled_zero_counter_is_unchanged(self):
        for status in ("Submitted", "PreSubmitted", "Cancelled", "ApiCancelled", "Inactive"):
            with self.subTest(status=status):
                self.assert_not_reconstructed(self.trade(status=status))

    def test_working_partial_counters_are_unchanged(self):
        trade = self.trade(status="Submitted", filled=5, remaining=7, avgFillPrice=700)
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual((state.status, state.filled, state.remaining, state.avg_fill_price),
                         ("Submitted", 5, 7, 700))
        self.assertFalse(state.raw["filled_from_executions"])

    def test_completed_order_valid_counters_are_unchanged(self):
        trade = self.trade(filled=12, avgFillPrice=730)
        state = self.adapter._to_polled_order_state(trade)
        self.assertEqual((state.filled, state.avg_fill_price), (12, 730))
        self.assertFalse(state.raw["filled_from_executions"])

    def test_nonzero_remaining_counter_is_not_overridden(self):
        self.assert_not_reconstructed(self.trade(remaining=1))

    def test_poll_uses_evidence_without_new_broker_requests(self):
        trade = self.trade()
        self.adapter._trades_by_ref[trade.order.orderRef] = trade
        self.adapter.ib = SimpleNamespace(isConnected=Mock(return_value=True), reqExecutions=Mock())
        self.assertEqual(self.adapter.poll_order(trade.order.orderRef).filled, 12)
        self.adapter.ib.reqExecutions.assert_not_called()


class NewSessionTradeCacheTests(unittest.TestCase):
    def setUp(self):
        self.adapter = IbAsyncTwsAdapter()
        self.connected = False
        self.adapter.ib = SimpleNamespace(isConnected=lambda: self.connected, connect=Mock())
        self.adapter._require_ib_async = lambda: (object, object, object)
        self.adapter._register_broker_event_handlers = Mock()
        self.adapter._reset_market_data_session_state = Mock()
        self.adapter.set_market_data_type = Mock()
        self.adapter.refresh_open_trades_cache = Mock()
        self.old = object()
        self.adapter._trades_by_ref["IBKRBOT|TEST|C1|SELL"] = self.old
        self.adapter._last_open_trades_refresh_monotonic = 123.0

    def test_new_session_discards_stale_handles_before_connect_callbacks(self):
        fresh = object()

        def connect(*args, **kwargs):
            self.assertEqual(self.adapter._trades_by_ref, {})
            self.assertEqual(self.adapter._last_open_trades_refresh_monotonic, 0)
            self.adapter._trades_by_ref["IBKRBOT|TEST|C1|SELL"] = fresh
            self.connected = True

        self.adapter.ib.connect.side_effect = connect
        self.adapter.connect("127.0.0.1", 4002, 17)
        self.assertIs(self.adapter._trades_by_ref["IBKRBOT|TEST|C1|SELL"], fresh)

    def test_same_live_session_keeps_current_handles(self):
        self.connected = True
        self.adapter.connect("127.0.0.1", 4002, 17)
        self.adapter.ib.connect.assert_not_called()
        self.assertIs(self.adapter._trades_by_ref["IBKRBOT|TEST|C1|SELL"], self.old)
        self.assertEqual(self.adapter._last_open_trades_refresh_monotonic, 123)

    def test_failed_new_session_does_not_restore_old_handles(self):
        self.adapter.ib.connect.side_effect = TimeoutError("connection interrupted")
        with self.assertRaises(TimeoutError):
            self.adapter.connect("127.0.0.1", 4002, 17)
        self.assertEqual(self.adapter._trades_by_ref, {})


class FreshRecoveryOpenOrderTests(unittest.TestCase):
    def setUp(self):
        self.adapter = IbAsyncTwsAdapter()
        self.adapter._upstream_connected = True
        self.adapter.ib = SimpleNamespace(
            isConnected=lambda: True, RaiseRequestErrors=False, RequestTimeout=0,
            reqOpenOrders=Mock(return_value=[]), openTrades=Mock(return_value=[]),
        )

    @staticmethod
    def trade():
        trade = CompletedOrderAdapterTests.trade(status="PreSubmitted", remaining=12)
        trade.order.orderId = 99
        trade.order.clientId = 17
        trade.order.totalQuantity = 12
        trade.fills = []
        return trade

    def test_stale_cache_only_trade_is_not_fresh_open_order_evidence(self):
        stale = self.trade()
        self.adapter.ib.openTrades.return_value = [stale]
        self.assertEqual(self.adapter.recovery_open_app_orders(), [])
        self.assertEqual(self.adapter._trades_by_ref, {})

    def test_malformed_stale_cache_only_trade_does_not_block_fresh_empty_reply(self):
        stale = self.trade()
        stale.order.account = ""
        stale.contract.conId = 0
        self.adapter.ib.openTrades.return_value = [stale]
        self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_malformed_relevant_cached_identity_defers_recovery(self):
        requested = self.trade()
        current = deepcopy(requested)
        current.order.account = ""
        self.adapter.ib.reqOpenOrders.return_value = [requested]
        self.adapter.ib.openTrades.return_value = [current]
        with self.assertRaisesRegex(BrokerAdapterError, "ownership is incomplete"):
            self.adapter.recovery_open_app_orders()

    def test_fresh_membership_uses_latest_matching_partial_state(self):
        requested = self.trade()
        current = deepcopy(requested)
        current.orderStatus.filled = 5
        current.orderStatus.remaining = 7
        current.orderStatus.avgFillPrice = 700
        stale = self.trade()
        stale.order.orderRef = "IBKRBOT|TEST|C0|SELL"
        stale.order.permId = 999
        self.adapter.ib.reqOpenOrders.return_value = [requested]
        self.adapter.ib.openTrades.return_value = [current, stale]
        states = self.adapter.recovery_open_app_orders()
        self.assertEqual(len(states), 1)
        self.assertEqual((states[0].filled, states[0].remaining), (5, 7))
        self.assertIs(self.adapter._trades_by_ref[requested.order.orderRef], current)

    def test_completed_during_refresh_is_not_returned_as_working(self):
        self.adapter.ib.reqOpenOrders.return_value = [self.trade()]
        self.adapter.ib.openTrades.return_value = []
        self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_same_reference_with_different_permanent_identity_not_included(self):
        requested = self.trade()
        current = deepcopy(requested)
        current.order.permId = 999
        self.adapter.ib.reqOpenOrders.return_value = [requested]
        self.adapter.ib.openTrades.return_value = [current]
        self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_same_reference_and_ids_with_foreign_ownership_not_included(self):
        for foreign_account in (True, False):
            with self.subTest(foreign_account=foreign_account):
                requested = self.trade()
                current = deepcopy(requested)
                if foreign_account:
                    current.order.account = "OTHER"
                else:
                    current.contract.conId = 999
                self.adapter.ib.reqOpenOrders.return_value = [requested]
                self.adapter.ib.openTrades.return_value = [current]
                self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_without_permanent_id_requires_same_client_and_session_order_id(self):
        requested = self.trade()
        requested.order.permId = 0
        current = deepcopy(requested)
        self.adapter.ib.reqOpenOrders.return_value = [requested]
        self.adapter.ib.openTrades.return_value = [current]
        self.assertEqual(len(self.adapter.recovery_open_app_orders()), 1)
        for field, value in (("clientId", 18), ("orderId", 98)):
            with self.subTest(field=field):
                current = deepcopy(requested)
                setattr(current.order, field, value)
                self.adapter.ib.openTrades.return_value = [current]
                self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_unidentified_fresh_request_fails_instead_of_claiming_no_orders(self):
        requested = self.trade()
        requested.order.permId = 0
        del requested.order.clientId
        self.adapter.ib.reqOpenOrders.return_value = [requested]
        with self.assertRaisesRegex(BrokerAdapterError, "identity is incomplete"):
            self.adapter.recovery_open_app_orders()


class StrictRecoveryExecutionDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.adapter = IbAsyncTwsAdapter()
        self.adapter._upstream_connected = True
        self.adapter.ib = SimpleNamespace(
            isConnected=lambda: True, RaiseRequestErrors=False, RequestTimeout=0,
            reqExecutions=Mock(return_value=[]), fills=Mock(return_value=[]), sleep=Mock(),
        )

    def recover(self, cached, requested):
        self.adapter.ib.fills.return_value = cached
        self.adapter.ib.reqExecutions.return_value = requested
        return self.adapter.recovery_recent_executions()

    def test_actual_recovery_rejects_cached_requested_quantity_conflict(self):
        old = CompletedOrderAdapterTests.fill()
        new = deepcopy(old)
        new.execution.shares = 13
        with self.assertRaisesRegex(BrokerAdapterError, "conflicting fill evidence"):
            self.recover([old], [new])
        self.assertFalse(self.adapter.ib.RaiseRequestErrors)
        self.assertEqual(self.adapter.ib.RequestTimeout, 0)

    def test_actual_recovery_rejects_conflicting_price_in_one_request(self):
        old = CompletedOrderAdapterTests.fill()
        new = deepcopy(old)
        new.execution.price = 730
        with self.assertRaisesRegex(BrokerAdapterError, "conflicting fill evidence"):
            self.recover([], [old, new])

    def test_recovery_rejects_conflicting_duplicate_ownership(self):
        for field, value in (
            ("acctNumber", "OTHER"), ("orderRef", "OTHER"),
            ("permId", 999), ("side", "BOT"),
        ):
            with self.subTest(field=field):
                old = CompletedOrderAdapterTests.fill()
                new = deepcopy(old)
                setattr(new.execution, field, value)
                with self.assertRaisesRegex(BrokerAdapterError, "conflicting fill evidence"):
                    self.recover([old], [new])

    def test_recovery_rejects_conflicting_duplicate_contract(self):
        old = CompletedOrderAdapterTests.fill()
        new = deepcopy(old)
        new.contract.conId = 999
        with self.assertRaisesRegex(BrokerAdapterError, "conflicting fill evidence"):
            self.recover([old], [new])

    def test_identified_invalid_execution_is_not_silently_dropped(self):
        for field, value in (("shares", 0), ("shares", -1), ("shares", "bad"), ("price", 0)):
            for invalid_first in (True, False):
                with self.subTest(field=field, invalid_first=invalid_first):
                    valid = CompletedOrderAdapterTests.fill()
                    invalid = deepcopy(valid)
                    setattr(invalid.execution, field, value)
                    rows = [invalid, valid] if invalid_first else [valid, invalid]
                    with self.assertRaisesRegex(BrokerAdapterError, "invalid fill data"):
                        self.recover([], rows)

    def test_identical_cache_and_requested_fill_counts_once(self):
        old = CompletedOrderAdapterTests.fill()
        rows = self.recover([old], [deepcopy(old)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shares"], 12)
        self.assertEqual(rows[0]["commission"], 5.38)

    def test_latest_authoritative_commission_updates_identical_execution(self):
        old = CompletedOrderAdapterTests.fill(commission=0)
        old.commissionReport.execId = ""
        old.commissionReport.currency = ""
        new = CompletedOrderAdapterTests.fill(commission=5.38)
        rows = self.recover([old], [new])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["commission"], 5.38)
        self.assertEqual(rows[0]["currency"], "EUR")

    def test_authoritative_zero_fee_correction_is_preserved(self):
        old = CompletedOrderAdapterTests.fill(commission=5.38)
        new = CompletedOrderAdapterTests.fill(commission=0)
        rows = self.recover([old], [new])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["commission"], 0)

    def test_uninitialized_later_commission_does_not_erase_known_fee(self):
        old = CompletedOrderAdapterTests.fill(commission=5.38)
        new = CompletedOrderAdapterTests.fill(commission=0)
        new.commissionReport.execId = ""
        new.commissionReport.currency = ""
        rows = self.recover([old], [new])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["commission"], 5.38)

    def test_wrong_execution_commission_report_does_not_replace_known_fee(self):
        old = CompletedOrderAdapterTests.fill(commission=5.38)
        new = CompletedOrderAdapterTests.fill(commission=1)
        new.commissionReport.execId = "OTHER"
        self.assertEqual(self.recover([old], [new])[0]["commission"], 5.38)

    def test_distinct_execution_ids_are_separate_partial_prints(self):
        rows = self.recover(
            [CompletedOrderAdapterTests.fill("one", shares=5)],
            [CompletedOrderAdapterTests.fill("two", shares=7)],
        )
        self.assertEqual([row["execution_id"] for row in rows], ["one", "two"])
        self.assertEqual(sum(row["shares"] for row in rows), 12)

    def test_ordinary_recent_executions_retains_existing_first_row_semantics(self):
        old = CompletedOrderAdapterTests.fill(shares=12)
        new = CompletedOrderAdapterTests.fill(shares=13, commission=10)
        self.adapter.ib.fills.return_value = [old]
        self.adapter.ib.reqExecutions.return_value = [new]
        rows = self.adapter.recent_executions()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["shares"], rows[0]["commission"]), (12, 5.38))
