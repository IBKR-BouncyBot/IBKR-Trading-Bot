"""Recovery reads reject failed or disconnected broker snapshots."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from app.ib_adapter import BrokerAdapter, BrokerAdapterError, IbAsyncTwsAdapter, QualifiedContract


class RecoveryAdapterReadTests(unittest.TestCase):
    def setUp(self):
        self.adapter = IbAsyncTwsAdapter()
        self.connected = True
        self.adapter._upstream_connected = True
        self.ib = SimpleNamespace(
            isConnected=lambda: self.connected,
            RaiseRequestErrors=False,
            RequestTimeout=0,
            reqOpenOrders=Mock(return_value=[]),
            openTrades=Mock(return_value=[]),
            reqExecutions=Mock(return_value=[]),
            fills=Mock(return_value=[]),
            reqPositions=Mock(return_value=[]),
            positions=Mock(return_value=[]),
            sleep=Mock(),
        )
        self.adapter.ib = self.ib
        self.contract = QualifiedContract("AAPL", 123, object())

    @staticmethod
    def position(quantity, *, account="DU_TEST", con_id=123):
        return SimpleNamespace(
            account=account, contract=SimpleNamespace(conId=con_id, symbol="AAPL"),
            position=quantity,
        )

    @staticmethod
    def trade(ref="IBKRBOT|AAPL|C1|BUY", *, status="Submitted", filled=0, remaining=10):
        return SimpleNamespace(
            order=SimpleNamespace(
                orderRef=ref, orderId=11, permId=21, account="DU_TEST",
                action="BUY", orderType="TRAIL", totalQuantity=10,
            ),
            orderStatus=SimpleNamespace(
                status=status, filled=filled, remaining=remaining, avgFillPrice=100,
            ),
            contract=SimpleNamespace(conId=123),
            fills=[],
        )

    @staticmethod
    def fill(execution_id="fill-1", *, price=100, at=None):
        timestamp = at or datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        return SimpleNamespace(
            execution=SimpleNamespace(
                execId=execution_id, orderRef="IBKRBOT|AAPL|C1|BUY", orderId=11,
                permId=21, side="BOT", shares=5, price=price, avgPrice=price,
                acctNumber="DU_TEST", exchange="NASDAQ", time=timestamp,
            ),
            contract=SimpleNamespace(symbol="AAPL", conId=123, secType="STK", currency="USD"),
            commissionReport=SimpleNamespace(commission=1.0, currency="USD"),
            time=timestamp,
        )

    def reads(self):
        return (
            (self.adapter.recovery_open_app_orders, self.ib.reqOpenOrders, self.ib.openTrades),
            (self.adapter.recovery_recent_executions, self.ib.reqExecutions, self.ib.fills),
        )

    def test_base_adapter_forwards_existing_deterministic_methods(self):
        adapter = BrokerAdapter()
        orders = [object()]
        executions = [{"execution_id": "test"}]
        adapter.open_app_orders = lambda: orders
        adapter.recent_executions = lambda: executions
        self.assertIs(adapter.recovery_open_app_orders(), orders)
        self.assertIs(adapter.recovery_recent_executions(), executions)
        adapter.position_size = Mock(return_value=12.0)
        self.assertEqual(adapter.recovery_position_size(self.contract, "DU_TEST"), 12.0)
        adapter.position_size.assert_called_once_with(self.contract, account="DU_TEST", refresh=True)

    def test_position_recovery_requires_exact_identity(self):
        for contract, account in ((self.contract, ""), (QualifiedContract("AAPL", None, object()), "DU_TEST")):
            with self.subTest(contract=contract.con_id, account=account):
                with self.assertRaisesRegex(BrokerAdapterError, "exact account and contract"):
                    self.adapter.recovery_position_size(contract, account)
        self.ib.reqPositions.assert_not_called()

    def test_position_completed_empty_response_proves_zero(self):
        self.ib.positions.return_value = [self.position(999)]
        self.assertEqual(self.adapter.recovery_position_size(self.contract, "DU_TEST"), 0.0)
        self.ib.reqPositions.assert_called_once_with()
        self.ib.positions.assert_not_called()

    def test_position_uses_only_exact_account_and_contract_and_preserves_extra_shares(self):
        self.ib.reqPositions.return_value = [
            self.position(999, account="OTHER"), self.position(999, con_id=999),
            self.position(250), self.position(5),
        ]
        self.assertEqual(self.adapter.recovery_position_size(self.contract, "DU_TEST"), 255.0)

    def test_position_invalid_matching_quantity_remains_unknown(self):
        for quantity in (None, "invalid", float("nan"), float("inf"), float("-inf")):
            with self.subTest(quantity=quantity):
                self.ib.reqPositions.return_value = [self.position(quantity)]
                self.assertIsNone(self.adapter.recovery_position_size(self.contract, "DU_TEST"))

    def test_position_failed_or_incomplete_request_never_uses_cached_holdings(self):
        self.ib.positions.return_value = [self.position(999)]
        for failure in (TimeoutError("position timeout"), RuntimeError("position failed"), None):
            with self.subTest(failure=failure):
                self.ib.reqPositions.side_effect = failure
                self.ib.reqPositions.return_value = None
                with self.assertRaises(BrokerAdapterError):
                    self.adapter.recovery_position_size(self.contract, "DU_TEST")
                self.ib.positions.assert_not_called()
                self.assertFalse(self.ib.RaiseRequestErrors)
                self.assertEqual(self.ib.RequestTimeout, 0)

    def test_position_disconnect_during_request_fails_closed(self):
        for local_loss in (True, False):
            with self.subTest(local_loss=local_loss):
                self.connected = True
                self.adapter._upstream_connected = True

                def disconnect():
                    if local_loss:
                        self.connected = False
                    else:
                        self.adapter._upstream_connected = False
                    return [self.position(100)]

                self.ib.reqPositions.side_effect = disconnect
                with self.assertRaisesRegex(BrokerAdapterError, "connectivity is unavailable"):
                    self.adapter.recovery_position_size(self.contract, "DU_TEST")

    def test_position_request_settings_are_scoped_and_restored(self):
        def request():
            self.assertIs(self.ib.RaiseRequestErrors, True)
            self.assertEqual(self.ib.RequestTimeout, 10.0)
            return []

        self.ib.reqPositions.side_effect = request
        self.assertEqual(self.adapter.recovery_position_size(self.contract, "DU_TEST"), 0)
        self.assertFalse(self.ib.RaiseRequestErrors)
        self.assertEqual(self.ib.RequestTimeout, 0)

    def test_ordinary_position_failed_refresh_still_returns_none(self):
        self.ib.positions.return_value = [self.position(999)]
        self.ib.reqPositions.side_effect = TimeoutError("position timeout")
        self.assertIsNone(self.adapter.position_size(self.contract, "DU_TEST", refresh=True))
        self.ib.positions.assert_not_called()

    def test_successful_empty_responses_are_authoritative_and_do_not_sleep(self):
        for read, request, cache in self.reads():
            with self.subTest(read=read.__name__):
                self.assertEqual(read(), [])
                request.assert_called_once_with()
                cache.assert_called_once_with()
        self.ib.sleep.assert_not_called()

    def test_open_orders_keep_app_filter_current_partial_state_and_handles(self):
        trade = self.trade(filled=3, remaining=7)
        self.ib.reqOpenOrders.return_value = [self.trade()]
        self.ib.openTrades.return_value = [trade, self.trade("MANUAL")]
        rows = self.adapter.recovery_open_app_orders()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].filled, rows[0].remaining), (3, 7))
        self.assertEqual(rows[0].order_ref, trade.order.orderRef)
        self.assertIs(self.adapter._trades_by_ref[trade.order.orderRef], trade)

    def test_order_completed_during_refresh_is_not_resurrected(self):
        self.ib.reqOpenOrders.return_value = [self.trade()]
        self.ib.openTrades.return_value = []
        self.assertEqual(self.adapter.recovery_open_app_orders(), [])

    def test_executions_preserve_cached_history_and_deduplicate_requested_fills(self):
        old, recent = self.fill("old"), self.fill("new", price=101)
        self.ib.fills.return_value = [old, recent]
        self.ib.reqExecutions.return_value = [recent]
        rows = self.adapter.recovery_recent_executions()
        self.assertEqual([row["execution_id"] for row in rows], ["old", "new"])
        self.assertEqual(rows[-1]["price"], 101)
        self.assertEqual(rows[-1]["account"], "DU_TEST")
        self.assertEqual(rows[-1]["con_id"], 123)

    def test_execution_fallback_identity_keeps_distinct_partial_times(self):
        first = self.fill("")
        second = self.fill("", at=datetime(2026, 10, 5, 12, 1, tzinfo=timezone.utc))
        self.ib.reqExecutions.return_value = [first, second]
        self.assertEqual(len(self.adapter.recovery_recent_executions()), 2)

    def test_request_failure_never_uses_populated_cache(self):
        self.ib.openTrades.return_value = [self.trade()]
        self.ib.fills.return_value = [self.fill()]
        for read, request, cache in self.reads():
            with self.subTest(read=read.__name__):
                request.side_effect = RuntimeError("request failed")
                with self.assertRaisesRegex(BrokerAdapterError, "request failed"):
                    read()
                cache.assert_not_called()
                self.assertFalse(self.ib.RaiseRequestErrors)
                self.assertEqual(self.ib.RequestTimeout, 0)

    def test_request_timeout_never_becomes_empty_success(self):
        for read, request, cache in self.reads():
            with self.subTest(read=read.__name__):
                request.side_effect = TimeoutError("incomplete request")
                with self.assertRaisesRegex(BrokerAdapterError, "incomplete request"):
                    read()
                cache.assert_not_called()

    def test_missing_completed_result_is_not_an_empty_snapshot(self):
        for read, request, cache in self.reads():
            with self.subTest(read=read.__name__):
                request.return_value = None
                with self.assertRaisesRegex(BrokerAdapterError, "no completed result"):
                    read()
                cache.assert_not_called()

    def test_cache_failure_or_unavailability_is_not_empty_success(self):
        for read, request, cache in self.reads():
            for failure in (RuntimeError("cache failed"), None):
                with self.subTest(read=read.__name__, failure=failure):
                    cache.side_effect = failure
                    cache.return_value = None
                    with self.assertRaises(BrokerAdapterError):
                        read()

    def test_local_disconnect_before_request_sends_nothing(self):
        self.connected = False
        for read, request, cache in self.reads():
            with self.subTest(read=read.__name__):
                with self.assertRaisesRegex(BrokerAdapterError, "connectivity is unavailable"):
                    read()
                request.assert_not_called()
                cache.assert_not_called()

    def test_unknown_or_down_upstream_before_request_sends_nothing(self):
        for upstream in (None, False):
            self.adapter._upstream_connected = upstream
            for read, request, cache in self.reads():
                with self.subTest(read=read.__name__, upstream=upstream):
                    with self.assertRaises(BrokerAdapterError):
                        read()
                    request.assert_not_called()
                    cache.assert_not_called()

    def test_disconnect_during_request_rejects_empty_and_nonempty_results(self):
        for read, request, cache in self.reads():
            for local_loss in (True, False):
                for rows in ([], [object()]):
                    with self.subTest(read=read.__name__, local_loss=local_loss, empty=not rows):
                        self.connected = True
                        self.adapter._upstream_connected = True

                        def disconnect():
                            if local_loss:
                                self.connected = False
                            else:
                                self.adapter._upstream_connected = False
                            return rows

                        request.side_effect = disconnect
                        with self.assertRaisesRegex(BrokerAdapterError, "connectivity is unavailable"):
                            read()
                        cache.assert_not_called()
                        self.assertFalse(self.ib.RaiseRequestErrors)
                        self.assertEqual(self.ib.RequestTimeout, 0)

    def test_disconnect_during_cache_read_rejects_result(self):
        for read, request, cache in self.reads():
            for local_loss in (True, False):
                with self.subTest(read=read.__name__, local_loss=local_loss):
                    self.connected = True
                    self.adapter._upstream_connected = True

                    def disconnect():
                        if local_loss:
                            self.connected = False
                        else:
                            self.adapter._upstream_connected = False
                        return []

                    cache.side_effect = disconnect
                    with self.assertRaisesRegex(BrokerAdapterError, "connectivity is unavailable"):
                        read()

    def test_request_error_flag_and_timeout_are_scoped_and_restored(self):
        for read, request, cache in self.reads():
            for original_timeout, scoped_timeout in ((0, 10.0), (30, 10.0), (2.0, 2.0)):
                for original_raise in (False, True):
                    with self.subTest(read=read.__name__, timeout=original_timeout, errors=original_raise):
                        self.ib.RequestTimeout = original_timeout
                        self.ib.RaiseRequestErrors = original_raise

                        def inspect():
                            self.assertIs(self.ib.RaiseRequestErrors, True)
                            self.assertEqual(self.ib.RequestTimeout, scoped_timeout)
                            return []

                        request.side_effect = inspect
                        read()
                        self.assertEqual(self.ib.RequestTimeout, original_timeout)
                        self.assertEqual(self.ib.RaiseRequestErrors, original_raise)

    def test_ordinary_disconnected_reads_remain_empty(self):
        self.connected = False
        self.assertEqual(self.adapter.open_app_orders(), [])
        self.assertEqual(self.adapter.recent_executions(), [])
        self.ib.reqOpenOrders.assert_not_called()
        self.ib.reqExecutions.assert_not_called()

    def test_ordinary_reads_preserve_their_existing_request_failure_fallback(self):
        self.ib.reqOpenOrders.side_effect = RuntimeError("temporary request failure")
        self.ib.openTrades.return_value = [self.trade()]
        self.ib.reqExecutions.side_effect = RuntimeError("temporary request failure")
        self.ib.fills.return_value = [self.fill()]
        self.assertEqual(len(self.adapter.open_app_orders()), 1)
        self.assertEqual(len(self.adapter.recent_executions()), 1)
        self.assertFalse(self.ib.RaiseRequestErrors)
        self.assertEqual(self.ib.RequestTimeout, 0)


if __name__ == "__main__":
    unittest.main()
