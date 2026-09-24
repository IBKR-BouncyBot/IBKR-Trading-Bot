"""Offline broker-boundary regressions for uncertain outcomes and evidence.

Run with pytest or ``python -m unittest tests.test_v500_broker_safety``.
All sessions below are protocol fakes; no dependency imports or sockets.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.ib_adapter import (
    BrokerAdapterError,
    BrokerSubmissionUnknownError,
    IbAsyncTwsAdapter,
    QualifiedContract,
)


class _Ticker:
    def __init__(self, contract: Any) -> None:
        self.contract = contract
        self.last = 100.0
        self.bid = 99.9
        self.ask = 100.1
        self.close = 98.0
        self.markPrice = 100.0
        self.marketDataType = 1
        self.ticks: list[Any] = []


class _Broker:
    """Model ib_async's conId ticker reuse and one reverse request mapping."""

    def __init__(self) -> None:
        self.client = SimpleNamespace(clientId=7)
        self.wrapper = SimpleNamespace(
            clientId=7, ticker2ReqId={"mktData": {}}, reqId2Ticker={}
        )
        self.sent: list[Any] = []
        self.cancelled: list[Any] = []
        self.tickers: dict[int, _Ticker] = {}
        self.live_requests: set[int] = set()
        self.request_count = 0
        self.connected = True
        self.cancel_result = True
        self.sleep_error: Exception | None = None
        self.send_error: Exception | None = None
        self.details: list[Any] = []
        self.trade: Any = None
        self.during_sleep: Any = None

    def isConnected(self) -> bool:
        return self.connected

    def sleep(self, _seconds: float) -> None:
        if self.during_sleep is not None:
            self.during_sleep()
        if self.sleep_error is not None:
            raise self.sleep_error

    def placeOrder(self, contract: Any, order: Any) -> Any:
        order.orderId = 42
        order.permId = 1234
        order.clientId = 7
        self.sent.append(order)
        self.trade = SimpleNamespace(
            contract=contract, order=order, fills=[],
            orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=10, avgFillPrice=0.0),
        )
        if self.send_error is not None:
            raise self.send_error
        return self.trade

    def cancelOrder(self, order: Any) -> None:
        self.cancelled.append(order)

    def openTrades(self) -> list[Any]:
        return []

    def reqOpenOrders(self) -> None:
        pass

    def reqMktData(self, contract: Any, _generic: str, _snapshot: bool, _regulatory: bool) -> _Ticker:
        self.request_count += 1
        ticker = self.tickers.setdefault(contract.conId, _Ticker(contract))
        self.wrapper.ticker2ReqId["mktData"][ticker] = self.request_count
        self.wrapper.reqId2Ticker[self.request_count] = ticker
        self.live_requests.add(self.request_count)
        return ticker

    def cancelMktData(self, contract: Any) -> bool:
        if not self.cancel_result:
            return False
        ticker = self.tickers[contract.conId]
        req_id = self.wrapper.ticker2ReqId["mktData"].pop(ticker, 0)
        self.live_requests.discard(req_id)
        return bool(req_id)

    def reqContractDetails(self, _contract: Any) -> list[Any]:
        return self.details

    def disconnect(self) -> None:
        self.connected = False
        self.live_requests.clear()


def _setup() -> tuple[IbAsyncTwsAdapter, _Broker, QualifiedContract]:
    adapter = IbAsyncTwsAdapter()
    broker = _Broker()
    adapter.ib = broker
    adapter._require_ib_async = lambda: (None, SimpleNamespace, None)
    adapter._market_data_event_tracking_available = True
    raw = SimpleNamespace(conId=123, exchange="SMART", primaryExchange="NASDAQ", currency="USD", secType="STK")
    contract = QualifiedContract("TEST", 123, raw, primary_exchange="NASDAQ")
    return adapter, broker, contract


class SubmissionSafetyTests(unittest.TestCase):
    def test_exception_after_send_is_unknown_for_both_sides_and_order_types(self) -> None:
        for side in ("BUY", "SELL"):
            for trailing in (False, True):
                with self.subTest(side=side, trailing=trailing):
                    adapter, broker, contract = _setup()
                    broker.sleep_error = RuntimeError("post-send wait failure")
                    kwargs: dict[str, Any] = dict(contract=contract, action=side, quantity=10, order_ref="IBKRBOT|TEST|OWN")
                    method = adapter.place_market_order
                    if trailing:
                        method = adapter.place_trailing_stop
                        kwargs.update(trailing_percent=1.0, initial_stop_price=99.0)
                    with self.assertRaises(BrokerSubmissionUnknownError) as caught:
                        method(**kwargs)
                    self.assertEqual(len(broker.sent), 1)
                    self.assertIs(adapter._trades_by_ref[kwargs["order_ref"]], broker.trade)
                    self.assertEqual((caught.exception.order_ref, caught.exception.order_id, caught.exception.perm_id), (kwargs["order_ref"], 42, 1234))

    def test_exception_inside_send_retains_attempt_identity(self) -> None:
        adapter, broker, contract = _setup()
        broker.send_error = RuntimeError("transport outcome unknown")
        with self.assertRaises(BrokerSubmissionUnknownError) as caught:
            adapter.place_market_order(contract=contract, action="BUY", quantity=10, order_ref="IBKRBOT|TEST|OWN")
        self.assertEqual(len(broker.sent), 1)
        self.assertEqual(caught.exception.order_id, 42)
        self.assertEqual(caught.exception.order_ref, "IBKRBOT|TEST|OWN")

    def test_post_send_processing_failure_preserves_trade_and_unknown_outcome(self) -> None:
        adapter, broker, contract = _setup()
        with patch.object(adapter, "_bind_pending_order_errors", side_effect=RuntimeError("bind failed")):
            with self.assertRaises(BrokerSubmissionUnknownError):
                adapter.place_market_order(contract=contract, action="SELL", quantity=10, order_ref="IBKRBOT|TEST|OWN")
        self.assertIs(adapter._trades_by_ref["IBKRBOT|TEST|OWN"], broker.trade)
        self.assertEqual(len(broker.sent), 1)

    def test_poll_and_execution_event_expose_exact_account_and_contract(self) -> None:
        adapter, broker, contract = _setup()
        adapter.place_market_order(contract=contract, action="BUY", quantity=10, order_ref="IBKRBOT|TEST|OWN", account="DU1")
        polled = adapter._to_polled_order_state(broker.trade)
        self.assertIsNotNone(polled)
        assert polled is not None
        self.assertEqual((polled.raw["account"], polled.raw["con_id"]), ("DU1", 123))
        fill = SimpleNamespace(contract=contract.raw, execution=SimpleNamespace(acctNumber="DU2", orderRef="IBKRBOT|TEST|OWN"))
        adapter._record_broker_event("EXEC_DETAILS", broker.trade, fill)
        event = adapter.drain_broker_events()[-1]
        self.assertEqual((event["account"], event["con_id"]), ("DU2", 123))

    def test_invalid_quantity_is_definitely_not_transmitted(self) -> None:
        adapter, broker, contract = _setup()
        with self.assertRaises(BrokerAdapterError) as caught:
            adapter.place_market_order(contract=contract, action="BUY", quantity=0, order_ref="IBKRBOT|TEST|OWN")
        self.assertNotIsInstance(caught.exception, BrokerSubmissionUnknownError)
        self.assertEqual(broker.sent, [])

    def test_valid_order_is_registered_before_event_pump_and_returns_identity(self) -> None:
        adapter, broker, contract = _setup()
        order_ref = "IBKRBOT|TEST|OWN"
        broker.during_sleep = lambda: self.assertIs(adapter._trades_by_ref[order_ref], broker.trade)
        handle = adapter.place_market_order(contract=contract, action="BUY", quantity=10, order_ref=order_ref, account="DU1")
        self.assertEqual((handle.status, handle.order_id, handle.perm_id), ("Submitted", 42, 1234))
        self.assertEqual((handle.raw["account"], handle.raw["con_id"]), ("DU1", 123))
        self.assertTrue(broker.sent[0].transmit)


class CancellationSafetyTests(unittest.TestCase):
    def test_numeric_collision_without_exact_reference_never_cancels(self) -> None:
        adapter, broker, _contract = _setup()
        other = SimpleNamespace(order=SimpleNamespace(orderRef="IBKRBOT|TEST|OTHER", orderId=42, clientId=9))
        adapter._trades_by_ref[other.order.orderRef] = other
        with self.assertRaises(BrokerAdapterError):
            adapter.cancel_order("IBKRBOT|TEST|OWN", 42)
        self.assertEqual(broker.cancelled, [])

    def test_reference_or_client_or_order_identity_mismatch_never_cancels(self) -> None:
        for ref, client, order_id in (("IBKRBOT|TEST|OTHER", 7, 42), ("IBKRBOT|TEST|OWN", 9, 42), ("IBKRBOT|TEST|OWN", None, 42), ("IBKRBOT|TEST|OWN", 7, 43)):
            with self.subTest(ref=ref, client=client, order_id=order_id):
                adapter, broker, _contract = _setup()
                adapter._trades_by_ref["IBKRBOT|TEST|OWN"] = SimpleNamespace(order=SimpleNamespace(orderRef=ref, orderId=order_id, clientId=client))
                with self.assertRaises(BrokerAdapterError):
                    adapter.cancel_order("IBKRBOT|TEST|OWN", 42)
                self.assertEqual(broker.cancelled, [])

    def test_valid_exact_current_client_order_remains_cancellable(self) -> None:
        adapter, broker, contract = _setup()
        handle = adapter.place_market_order(contract=contract, action="SELL", quantity=10, order_ref="IBKRBOT|TEST|OWN")
        adapter.cancel_order(handle.order_ref, handle.order_id)
        self.assertEqual(broker.cancelled, broker.sent)


class MarketDataSafetyTests(unittest.TestCase):
    def test_cached_fields_and_size_ticks_grant_no_price_freshness(self) -> None:
        adapter, _broker, contract = _setup()
        ticker = adapter._request_ticker(contract, "")
        for tick_type in (0, 3, 5, 45):
            ticker.ticks = [SimpleNamespace(tickType=tick_type)]
            adapter._on_pending_tickers([ticker])
            snapshot = adapter._snapshot_from_ticker(ticker, contract)
            self.assertEqual(snapshot.field_update_sequences, {})
            self.assertFalse(snapshot.selected_price_basis_updated_in_event)

    def test_raw_unchanged_price_tick_is_fresh_only_for_that_field(self) -> None:
        adapter, _broker, contract = _setup()
        ticker = adapter._request_ticker(contract, "")
        ticker.ticks = [SimpleNamespace(tickType=1)]
        adapter._on_pending_tickers([ticker])
        snapshot = adapter._snapshot_from_ticker(ticker, contract)
        self.assertEqual(set(snapshot.field_update_sequences), {"bid"})
        self.assertIsNone(snapshot.field_update_age_seconds["ask"])
        self.assertIsNone(snapshot.field_update_age_seconds["last"])
        self.assertEqual(snapshot.fields_changed_in_update, [])

    def test_generation_change_clears_freshness_and_detaches_old_request(self) -> None:
        adapter, broker, contract = _setup()
        ticker = adapter._request_ticker(contract, "")
        ticker.ticks = [SimpleNamespace(tickType=4)]
        adapter._on_pending_tickers([ticker])
        first = adapter._snapshot_from_ticker(ticker, contract)
        self.assertIn("last", first.field_update_sequences)
        replacement = adapter._request_ticker(contract, "232")
        self.assertIs(replacement, ticker)
        self.assertEqual(broker.live_requests, {2})
        self.assertNotIn(1, broker.wrapper.reqId2Ticker)
        replacement.ticks = [SimpleNamespace(tickType=5)]
        adapter._on_pending_tickers([replacement])
        second = adapter._snapshot_from_ticker(replacement, contract)
        self.assertNotEqual(first.market_data_subscription_id, second.market_data_subscription_id)
        self.assertEqual(second.field_update_sequences, {})
        adapter._clear_market_data_subscriptions()
        self.assertEqual(broker.live_requests, set())
        self.assertEqual(broker.wrapper.reqId2Ticker, {})

    def test_lost_subscription_cannot_refresh_a_reused_ticker_from_old_request(self) -> None:
        adapter, broker, contract = _setup()
        original = adapter._request_ticker(contract, "")
        first_id = adapter._snapshot_from_ticker(original, contract).market_data_subscription_id
        broker.wrapper._reqId2Contract = {1: contract.raw}
        foreign = _Ticker(SimpleNamespace(conId=456))
        broker.wrapper.ticker2ReqId["mktData"][foreign] = 99
        broker.wrapper.reqId2Ticker[99] = foreign
        broker.wrapper._reqId2Contract[99] = foreign.contract
        # Another subscription type can legitimately share this Ticker.
        broker.wrapper.ticker2ReqId["BidAsk"] = {original: 100}
        broker.wrapper.reqId2Ticker[100] = original
        broker.wrapper._reqId2Contract[100] = contract.raw

        with patch.object(broker, "cancelMktData", side_effect=AssertionError("Lost subscriptions need no cancellation")):
            adapter._on_ib_error(0, 1101, "Connectivity restored; market data subscriptions were lost")
        self.assertNotIn(1, broker.wrapper.reqId2Ticker)
        self.assertNotIn(1, broker.wrapper._reqId2Contract)
        self.assertNotIn(original, broker.wrapper.ticker2ReqId["mktData"])
        self.assertIs(broker.wrapper.reqId2Ticker[99], foreign)
        self.assertIs(broker.wrapper.reqId2Ticker[100], original)
        self.assertEqual(broker.wrapper.ticker2ReqId["BidAsk"][original], 100)

        replacement = adapter._request_ticker(contract, "")
        self.assertIs(replacement, original)
        second_id = adapter._snapshot_from_ticker(replacement, contract).market_data_subscription_id
        self.assertNotEqual(first_id, second_id)

        def deliver_price_tick(req_id: int, price: float) -> None:
            # Match ib_async's request-ID lookup before pendingTickersEvent.
            ticker = broker.wrapper.reqId2Ticker.get(req_id)
            if ticker is not None:
                ticker.last = price
                ticker.ticks = [SimpleNamespace(tickType=4)]
                adapter._on_pending_tickers([ticker])

        deliver_price_tick(1, 1.0)
        stale = adapter._snapshot_from_ticker(replacement, contract)
        self.assertEqual(stale.field_update_sequences, {})
        self.assertEqual(replacement.last, 100.0)
        deliver_price_tick(2, 101.0)
        fresh = adapter._snapshot_from_ticker(replacement, contract)
        self.assertGreater(fresh.field_update_sequences["last"], 0)
        self.assertEqual(replacement.last, 101.0)

    def test_failed_cancellation_blocks_a_replacement_request(self) -> None:
        adapter, broker, contract = _setup()
        adapter._request_ticker(contract, "")
        broker.cancel_result = False
        with self.assertRaises(BrokerAdapterError):
            adapter._request_ticker(contract, "232")
        self.assertEqual(broker.request_count, 1)
        self.assertEqual(broker.live_requests, {1})
        adapter.disconnect()
        self.assertFalse(broker.connected)
        self.assertEqual(adapter._tickers, {})

    def test_working_variant_is_reused_without_request_churn(self) -> None:
        adapter, broker, contract = _setup()
        direct_raw = SimpleNamespace(conId=123, exchange="NASDAQ", primaryExchange="NASDAQ")
        direct = QualifiedContract("TEST", 123, direct_raw, primary_exchange="NASDAQ", exchange="NASDAQ")
        ticker = adapter._request_ticker(direct, "")
        ticker.ticks = [SimpleNamespace(tickType=1), SimpleNamespace(tickType=2)]
        adapter._on_pending_tickers([ticker])
        snapshot = adapter._price_snapshot_for_active_mode(contract, timeout=0)
        self.assertEqual(snapshot.request_exchange, "NASDAQ")
        self.assertEqual(snapshot.generic_ticks, "")
        self.assertEqual(broker.request_count, 1)

    def test_reused_generic_subscription_reports_requested_ticks(self) -> None:
        adapter, broker, contract = _setup()
        ticker = adapter._request_ticker(contract, "232")
        subscription = adapter._snapshot_from_ticker(ticker, contract).market_data_subscription_id
        for ready in (False, True):
            with self.subTest(ready=ready):
                if ready:
                    ticker.ticks = [SimpleNamespace(tickType=4)]
                    adapter._on_pending_tickers([ticker])
                snapshot = adapter._price_snapshot_for_active_mode(contract, timeout=0)
                self.assertEqual(snapshot.generic_ticks, "232")
                self.assertEqual(snapshot.market_data_subscription_id, subscription)
                self.assertEqual(adapter._snapshot_has_subscription_data(snapshot), ready)
                self.assertEqual(broker.request_count, 1)


    def test_pending_fallback_subscription_survives_zero_wait_polls(self) -> None:
        for fallback in ("generic", "direct"):
            with self.subTest(fallback=fallback):
                adapter, broker, contract = _setup()
                request_contract = contract
                generic = "232"
                if fallback == "direct":
                    raw = SimpleNamespace(conId=123, exchange="NASDAQ", primaryExchange="NASDAQ")
                    request_contract = QualifiedContract("TEST", 123, raw, primary_exchange="NASDAQ", exchange="NASDAQ")
                    generic = ""
                ticker = adapter._request_ticker(request_contract, generic)
                subscription = adapter._snapshot_from_ticker(ticker, request_contract).market_data_subscription_id
                for _ in range(10):
                    pending = adapter._price_snapshot_for_active_mode(contract, timeout=0)
                    self.assertEqual(broker.request_count, 1)
                    self.assertEqual(broker.live_requests, {1})
                    self.assertEqual(pending.market_data_subscription_id, subscription)
                    self.assertEqual(pending.field_update_sequences, {})
                    self.assertFalse(adapter._snapshot_has_subscription_data(pending))
                ticker.last = 101.0
                ticker.ticks = [SimpleNamespace(tickType=4)]
                adapter._on_pending_tickers([ticker])
                fresh = adapter._price_snapshot_for_active_mode(contract, timeout=0)
                self.assertTrue(adapter._snapshot_has_subscription_data(fresh))
                self.assertEqual(fresh.fields["last"], 101.0)
                self.assertEqual(set(fresh.field_update_sequences), {"last"})
                self.assertEqual(fresh.market_data_subscription_id, subscription)
                self.assertEqual(broker.request_count, 1)

    def test_zero_wait_polls_allow_slow_first_subscription_response(self) -> None:
        adapter, broker, contract = _setup()
        clock = 0.0
        scheduled: list[tuple[float, int]] = []
        original_request = broker.reqMktData

        def delayed_request(raw: Any, generic: str, snapshot: bool, regulatory: bool) -> Any:
            ticker = original_request(raw, generic, snapshot, regulatory)
            scheduled.append((clock + 0.3, broker.request_count))
            return ticker

        broker.reqMktData = delayed_request  # type: ignore[method-assign]
        first_usable_at = None
        for step in range(21):
            clock = step / 10
            for due, req_id in list(scheduled):
                if due > clock:
                    continue
                scheduled.remove((due, req_id))
                ticker = broker.wrapper.reqId2Ticker.get(req_id)
                if req_id in broker.live_requests and ticker is not None:
                    ticker.last = 101.0
                    ticker.ticks = [SimpleNamespace(tickType=4)]
                    adapter._on_pending_tickers([ticker])
            snapshot = adapter._price_snapshot_for_active_mode(contract, timeout=0)
            if first_usable_at is None and adapter._snapshot_has_subscription_data(snapshot):
                first_usable_at = clock
        self.assertEqual(first_usable_at, 0.3)
        self.assertEqual(broker.request_count, 2)
        self.assertEqual(broker.live_requests, {2})
        self.assertNotIn(1, broker.wrapper.reqId2Ticker)

    def test_positive_wait_can_probe_an_alternative_to_pending_fallback(self) -> None:
        adapter, broker, contract = _setup()
        ticker = adapter._request_ticker(contract, "232")
        previous_subscription = adapter._snapshot_from_ticker(ticker, contract).market_data_subscription_id

        def deliver_current_price() -> None:
            ticker.ticks = [SimpleNamespace(tickType=4)]
            adapter._on_pending_tickers([ticker])

        broker.during_sleep = deliver_current_price
        snapshot = adapter._price_snapshot_for_active_mode(contract, timeout=0.1)
        self.assertTrue(adapter._snapshot_has_subscription_data(snapshot))
        self.assertNotEqual(snapshot.market_data_subscription_id, previous_subscription)
        self.assertEqual(broker.request_count, 2)
        self.assertEqual(broker.live_requests, {2})
        self.assertNotIn(1, broker.wrapper.reqId2Ticker)
        self.assertEqual(set(snapshot.field_update_sequences), {"last"})


class SessionEvidenceTests(unittest.TestCase):
    def test_weekday_holiday_without_metadata_never_authorizes_trading(self) -> None:
        adapter, broker, contract = _setup()
        christmas = datetime(2025, 12, 25, 16, 0, tzinfo=timezone.utc)
        clock = SimpleNamespace(now=lambda _tz: christmas, strptime=datetime.strptime)
        with patch("app.ib_adapter.datetime", clock):
            status = adapter.regular_trading_hours_status(contract)
        self.assertFalse(status.is_open)
        self.assertIn("Regular trading hours are unavailable", status.message)
        self.assertFalse(status.session_open)
        self.assertEqual(broker.sent, [])

    def test_contract_details_failure_does_not_authorize_weekday_trading(self) -> None:
        adapter, _broker, contract = _setup()
        with patch.object(adapter.ib, "reqContractDetails", side_effect=RuntimeError("no metadata")):
            status = adapter.regular_trading_hours_status(contract)
        self.assertFalse(status.is_open)
        self.assertIn("Regular trading hours are unavailable", status.message)

    def test_authoritative_open_and_closed_sessions_are_preserved(self) -> None:
        for hours, expected in (("20260924:0930-1600", True), ("20260924:CLOSED", False)):
            with self.subTest(hours=hours):
                adapter, broker, contract = _setup()
                broker.details = [SimpleNamespace(liquidHours=hours, timeZoneId="America/New_York")]
                now = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
                clock = SimpleNamespace(now=lambda _tz: now, strptime=datetime.strptime)
                with patch("app.ib_adapter.datetime", clock):
                    status = adapter.regular_trading_hours_status(contract)
                self.assertEqual(status.is_open, expected)
                self.assertEqual(status.source, "contract_liquid_hours")


if __name__ == "__main__":
    unittest.main()
