"""Comprehensive deterministic tests for the IBKR adapter boundary.

All IB/TWS objects are protocol-shaped fakes.  The suite verifies normalization,
validation, order construction, event ownership, recovery, and market-data
freshness without opening a socket or transmitting an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import (
    APP_ORDER_PREFIX,
    BrokerAdapter,
    BrokerAdapterError,
    BrokerConnectivityStatus,
    ContractSearchResult,
    IbAsyncTwsAdapter,
    MarketPriceSnapshot,
    OrderHandle,
    QualifiedContract,
    RthStatus,
)


class FakeOrder:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
        self.orderId = kwargs.get("orderId", 101)
        self.permId = kwargs.get("permId", 202)


class FakeStock:
    def __init__(self, symbol: str, exchange: str, currency: str, **kwargs: Any) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.secType = "STK"
        self.primaryExchange = kwargs.get("primaryExchange", "")
        self.conId = kwargs.get("conId", 0)
        self.localSymbol = symbol
        self.tradingClass = symbol


class FakeTicker:
    def __init__(self, contract: Any, *, price: float = 100.0) -> None:
        self.contract = contract
        self.marketDataType = 1
        self.last = price
        self.delayedLast = None
        self.bid = price - 0.05
        self.ask = price + 0.05
        self.delayedBid = None
        self.delayedAsk = None
        self.close = price - 1.0
        self.delayedClose = None
        self.markPrice = price
        self.delayedMarkPrice = None
        self.time = "2026-07-11T12:00:00+00:00"

    def marketPrice(self) -> float:
        return float(self.markPrice)


@dataclass
class FakeDescription:
    contract: Any
    description: str = ""
    derivativeSecTypes: list[str] | None = None


class FakeIB:
    def __init__(self) -> None:
        self.connected = True
        self.sleep_calls: list[float] = []
        self.market_data_types: list[int] = []
        self.cancelled_market_data: list[Any] = []
        self.cancelled_orders: list[Any] = []
        self.placed_orders: list[tuple[Any, Any]] = []
        self.open_order_requests = 0
        self.matching_symbols: list[Any] = []
        self.qualified_contracts: list[Any] | None = None
        self.contract_details: list[Any] = []
        self.market_rule_values: dict[int, list[Any]] = {}
        self.market_rule_requests: list[int] = []
        self.what_if_orders: list[tuple[Any, Any]] = []
        self.next_order_state: Any = None
        self.open_trades: list[Any] = []
        self.all_trades: list[Any] = []
        self.fill_values: list[Any] = []
        self.execution_values: list[Any] = []
        self.position_values: list[Any] = []
        self.account_values: Any = []
        self.wrapper = SimpleNamespace(accounts=[])
        self.next_trade: Any = None

    def isConnected(self) -> bool:
        return self.connected

    def connect(self, host: str, port: int, *, clientId: int, timeout: float) -> None:
        self.connected = True
        self.connect_args = (host, port, clientId, timeout)

    def disconnect(self) -> None:
        self.connected = False

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(float(seconds))

    def reqMarketDataType(self, value: int) -> None:
        self.market_data_types.append(int(value))

    def reqMktData(self, contract: Any, generic_ticks: str, snapshot: bool, regulatory: bool) -> FakeTicker:
        return FakeTicker(contract)

    def cancelMktData(self, contract: Any) -> None:
        self.cancelled_market_data.append(contract)

    def reqMatchingSymbols(self, pattern: str) -> list[Any]:
        self.last_search_pattern = pattern
        return list(self.matching_symbols)

    def qualifyContracts(self, contract: Any) -> list[Any]:
        if self.qualified_contracts is None:
            if not getattr(contract, "conId", 0):
                contract.conId = 123
            return [contract]
        return list(self.qualified_contracts)

    def reqContractDetails(self, contract: Any) -> list[Any]:
        return list(self.contract_details)

    def reqMarketRule(self, market_rule_id: int) -> list[Any]:
        self.market_rule_requests.append(int(market_rule_id))
        return list(self.market_rule_values.get(int(market_rule_id), []))

    def whatIfOrder(self, contract: Any, order: Any) -> Any:
        self.what_if_orders.append((contract, order))
        if self.next_order_state is not None:
            return self.next_order_state
        if self.next_trade is not None:
            return getattr(self.next_trade, "orderState", None)
        return SimpleNamespace(
            status="PreSubmitted",
            warningText="",
            initMarginChange="1",
            maintMarginChange="2",
            equityWithLoanChange="-1",
        )

    def placeOrder(self, contract: Any, order: Any) -> Any:
        self.placed_orders.append((contract, order))
        if self.next_trade is not None:
            return self.next_trade
        return SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=getattr(order, "totalQuantity", 0), avgFillPrice=0.0, permId=order.permId),
            orderState=SimpleNamespace(status="PreSubmitted", warningText="", initMarginChange="1", maintMarginChange="2", equityWithLoanChange="-1"),
            fills=[],
        )

    def cancelOrder(self, order: Any) -> None:
        self.cancelled_orders.append(order)

    def reqOpenOrders(self) -> None:
        self.open_order_requests += 1

    def openTrades(self) -> list[Any]:
        return list(self.open_trades)

    def trades(self) -> list[Any]:
        return list(self.all_trades)

    def fills(self) -> list[Any]:
        return list(self.fill_values)

    def reqExecutions(self, *args: Any) -> list[Any]:
        return list(self.execution_values)

    def managedAccounts(self) -> Any:
        return self.account_values

    def positions(self) -> list[Any]:
        return list(self.position_values)

    def reqPositions(self) -> list[Any]:
        return list(self.position_values)


@pytest.fixture
def live_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[IbAsyncTwsAdapter, FakeIB]:
    adapter = IbAsyncTwsAdapter()
    ib = FakeIB()
    adapter.ib = ib
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "ready"
    adapter._market_data_event_tracking_available = True
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (FakeIB, FakeOrder, FakeStock))
    return adapter, ib


def stock_contract(symbol: str = "AAPL", con_id: int = 123, exchange: str = "SMART", primary: str = "NASDAQ") -> QualifiedContract:
    raw = FakeStock(symbol, exchange, "USD", primaryExchange=primary)
    raw.conId = con_id
    return QualifiedContract(symbol, con_id, raw, primary_exchange=primary, local_symbol=symbol, trading_class=symbol)


def fill(
    *,
    symbol: str = "AAPL",
    side: str = "BOT",
    shares: float = 2.0,
    price: float = 100.0,
    order_ref: str = f"{APP_ORDER_PREFIX}|AAPL|BUY",
    order_id: int = 101,
    perm_id: int = 202,
    exec_id: str = "E1",
    account: str = "DU1",
) -> Any:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, conId=123, secType="STK", currency="USD"),
        order=SimpleNamespace(orderRef=order_ref),
        execution=SimpleNamespace(
            orderRef=order_ref,
            orderId=order_id,
            permId=perm_id,
            side=side,
            shares=shares,
            price=price,
            avgPrice=price,
            execId=exec_id,
            time="2026-07-11T12:00:00+00:00",
            acctNumber=account,
            exchange="NASDAQ",
        ),
        commissionReport=SimpleNamespace(commission=0.5, currency="USD"),
    )


def test_broker_adapter_interface_defaults_and_abstract_contracts() -> None:
    base = BrokerAdapter()
    with pytest.raises(NotImplementedError):
        base.connect("127.0.0.1", 4001, 11)
    with pytest.raises(NotImplementedError):
        base.disconnect()
    with pytest.raises(NotImplementedError):
        base.is_connected()
    base.process_events()
    with pytest.raises(NotImplementedError):
        base.set_market_data_type(1)
    assert base.managed_accounts() == []
    with pytest.raises(NotImplementedError):
        base.search_stock_contracts("AAPL")
    with pytest.raises(NotImplementedError):
        base.qualify_stock("AAPL", "SMART", "USD")
    contract = stock_contract()
    with pytest.raises(NotImplementedError):
        base.price_snapshot(contract)
    with pytest.raises(NotImplementedError):
        base.what_if_trailing_stop(contract=contract, action="BUY", quantity=1, trailing_percent=1, initial_stop_price=100, order_ref="x")
    with pytest.raises(NotImplementedError):
        base.what_if_market_order(contract=contract, action="BUY", quantity=1, order_ref="x")
    with pytest.raises(NotImplementedError):
        base.place_trailing_stop(contract=contract, action="BUY", quantity=1, trailing_percent=1, initial_stop_price=100, order_ref="x")
    with pytest.raises(NotImplementedError):
        base.place_market_order(contract=contract, action="BUY", quantity=1, order_ref="x")
    with pytest.raises(NotImplementedError):
        base.cancel_order("x")
    with pytest.raises(NotImplementedError):
        base.poll_order("x")
    with pytest.raises(NotImplementedError):
        base.open_app_orders()
    assert base.recent_executions() == []
    assert base.drain_broker_events() == []
    assert base.position_size(contract) is None
    assert base.regular_trading_hours_status(contract).is_open is True
    with pytest.raises(NotImplementedError):
        base.recover_order_fill(order_ref="x")


class MinimalAdapter(BrokerAdapter):
    def is_connected(self) -> bool:
        return True

    def price_snapshot(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        return MarketPriceSnapshot(12.5, "test", 1, 1, {}, "now")


def test_broker_adapter_compatibility_defaults_use_local_connection() -> None:
    adapter = MinimalAdapter()
    status = adapter.connectivity_status()
    assert status.trading_ready is True
    assert adapter.last_price(stock_contract()) == 12.5


def test_connectivity_dataclasses_and_search_result_serialization() -> None:
    status = BrokerConnectivityStatus(True, True, "connected", "ready")
    assert status.trading_ready is True
    assert status.to_dict()["trading_ready"] is True
    assert RthStatus(True, "test", "open", "now").to_dict()["is_open"] is True
    assert MarketPriceSnapshot(1.0, "last", 1, 1, {"last": 1.0}, "now").to_dict()["source"] == "last"
    result = ContractSearchResult("AAPL", "STK", "USD", "SMART", "NASDAQ", 123, description="Apple")
    assert result.supported is True
    assert "conId 123" in result.label()
    assert result.to_dict()["supported"] is True


def test_api_disconnect_and_broker_event_recording_are_app_owned_only(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, _ = live_adapter
    adapter._tickers[(123, "default", "SMART", "NASDAQ")] = FakeTicker(stock_contract().raw)
    adapter._on_api_disconnected()
    status = adapter.connectivity_status()
    assert status.upstream_connected is False
    assert status.market_data_resubscribe_required is True
    assert adapter.drain_broker_events()[-1]["event_type"] == "IBKR_API_DISCONNECTED"

    manual = SimpleNamespace(order=SimpleNamespace(orderRef="MANUAL", orderId=1, permId=2), orderStatus=SimpleNamespace(status="Submitted"), contract=SimpleNamespace(symbol="AAPL", currency="USD"))
    adapter._record_broker_event("OPEN_ORDER", manual)
    assert adapter.drain_broker_events() == []

    app_trade = SimpleNamespace(
        order=SimpleNamespace(orderRef=f"{APP_ORDER_PREFIX}|AAPL|BUY", orderId=1, permId=2, action="BUY"),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1, avgFillPrice=0.0, permId=2),
        contract=SimpleNamespace(symbol="AAPL", currency="USD"),
    )
    assert adapter._first_with_attr((object(), app_trade), "order") is app_trade
    adapter._record_broker_event("OPEN_ORDER", app_trade, keyword="value")
    event = adapter.drain_broker_events()[0]
    assert event["order_ref"].startswith(APP_ORDER_PREFIX)
    assert event["ticker"] == "AAPL"


def test_require_ib_async_and_process_events(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    # The fixture replaces the loader to make the dependency boundary explicit.
    assert adapter._require_ib_async() == (FakeIB, FakeOrder, FakeStock)
    adapter.process_events(0.2)
    assert ib.sleep_calls[-1] == pytest.approx(0.2)


def test_contract_search_normalizes_deduplicates_sorts_and_caches(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    exact = FakeStock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")
    exact.conId = 123
    duplicate = FakeStock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")
    duplicate.conId = 123
    unsupported = FakeStock("AAPLX", "SMART", "EUR", primaryExchange="NYSE")
    unsupported.conId = 456
    unsupported.secType = "CFD"
    blank = FakeStock("", "SMART", "USD")
    ib.matching_symbols = [
        FakeDescription(unsupported, "Unsupported"),
        FakeDescription(duplicate, "Apple duplicate"),
        FakeDescription(exact, "Apple", ["OPT"]),
        FakeDescription(blank),
    ]
    results = adapter.search_stock_contracts(" aapl ", max_results=8)
    assert [item.symbol for item in results] == ["AAPL", "AAPLX"]
    assert results[0].supported is True
    assert results[1].supported is False
    ib.matching_symbols = []
    assert adapter.search_stock_contracts("AAPL", max_results=8) == results
    assert adapter.search_stock_contracts("  ") == []


def test_qualify_stock_sets_identity_and_minimum_tick(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    qualified = FakeStock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")
    qualified.conId = 987
    qualified.localSymbol = "AAPL"
    qualified.tradingClass = "NMS"
    ib.qualified_contracts = [qualified]
    ib.contract_details = [
        SimpleNamespace(
            minTick=0.005,
            validExchanges="SMART,NASDAQ",
            orderTypes="MKT,TRAIL",
            liquidHours="20260724:0930-20260724:1600",
            timeZoneId="US/Eastern",
        )
    ]
    contract = adapter.qualify_stock("aapl", "smart", "usd", "nasdaq", 987)
    assert contract.ticker == "AAPL"
    assert contract.con_id == 987
    assert contract.min_tick == pytest.approx(0.005)
    assert adapter.qualify_stock("AAPL", "SMART", "USD", "NASDAQ", 987).raw is qualified

    ib.qualified_contracts = []
    with pytest.raises(BrokerAdapterError, match="did not resolve"):
        adapter.qualify_stock("MSFT", "SMART", "USD", "NASDAQ", 654)


def test_market_data_variant_and_active_mode_pipeline(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB], monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = live_adapter
    contract = stock_contract()
    assert adapter._candidate_primary_exchange(contract) == "NASDAQ"
    variant = adapter._qualified_market_data_variant(contract, "NASDAQ")
    assert variant is not None
    assert variant.raw.exchange == "NASDAQ"
    assert adapter._qualified_market_data_variant(contract, "NASDAQ") is variant

    ticker = adapter._request_ticker(contract, "")
    adapter._on_pending_tickers([ticker])
    snapshot = adapter._try_price_for_contract(contract, 0.0, "direct")
    assert snapshot.api_data_received is True
    assert snapshot.price == pytest.approx(100.0)
    assert "direct" in snapshot.source

    usable = MarketPriceSnapshot(
        100.0,
        "last",
        1,
        1,
        {"last": 100.0},
        "now",
        api_data_received=True,
        market_data_update_sequence=1,
        market_data_event_tracking=True,
    )
    monkeypatch.setattr(adapter, "_try_price_for_contract", lambda *_args, **_kwargs: usable)
    variants = adapter._try_market_data_variants(contract, 0.0)
    assert variants is usable

    active = adapter._price_snapshot_for_active_mode(contract, timeout=0.0)
    assert active is usable
    adapter._market_data_type = 1
    assert adapter.last_price(contract, timeout=0.0) == active.price

    # Auto mode attempts modes in order and annotates the selected one.
    adapter._market_data_type = 0
    monkeypatch.setattr(adapter, "_apply_market_data_type_to_tws", lambda mode: setattr(adapter, "_active_market_data_type", mode))
    monkeypatch.setattr(adapter, "_price_snapshot_for_active_mode", lambda _contract, timeout=1.0: MarketPriceSnapshot(
        101.0,
        "last",
        0,
        adapter._active_market_data_type,
        {"last": 101.0},
        "now",
        api_data_received=True,
        market_data_update_sequence=1,
        market_data_event_tracking=True,
    ))
    automatic = adapter._auto_price_snapshot(contract, timeout=0.0)
    assert automatic.auto_market_data is True
    assert automatic.selected_market_data_type == 1
    assert automatic.status.startswith("OK - auto selected")


def test_existing_fresh_subscription_returns_without_an_initial_wait(
    live_adapter: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    ticker = adapter._request_ticker(contract, "")
    adapter._on_pending_tickers([ticker])
    ib.sleep_calls.clear()

    snapshot = adapter._try_price_for_contract(contract, timeout=1.0, note="fresh")

    assert snapshot.api_data_received is True
    assert snapshot.price == pytest.approx(100.0)
    assert ib.sleep_calls == []


def test_market_data_mode_change_does_not_add_a_fixed_quarter_second_wait(
    live_adapter: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = live_adapter
    adapter._active_market_data_type = None
    ib.sleep_calls.clear()

    adapter._apply_market_data_type_to_tws(3)

    assert adapter._active_market_data_type == 3
    assert ib.market_data_types[-1] == 3
    assert ib.sleep_calls == []


def test_nonblocking_price_read_returns_cached_fields_without_sleeping(
    live_adapter: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    ib.sleep_calls.clear()

    snapshot = adapter._try_price_for_contract(contract, timeout=0.0, note="scheduled")

    assert snapshot.price == pytest.approx(100.0)
    assert snapshot.api_data_received is False
    assert ib.sleep_calls == []


def test_bounded_price_wait_uses_short_slices_instead_of_quarter_seconds(
    live_adapter: tuple[IbAsyncTwsAdapter, FakeIB],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    clock = [100.0]

    monkeypatch.setattr("app.ib_adapter.time.monotonic", lambda: clock[0])

    def advance(seconds: float) -> None:
        ib.sleep_calls.append(float(seconds))
        clock[0] += float(seconds)

    monkeypatch.setattr(ib, "sleep", advance)
    ib.sleep_calls.clear()

    snapshot = adapter._try_price_for_contract(contract, timeout=0.12, note="bounded")

    assert snapshot.api_data_received is False
    assert ib.sleep_calls
    assert max(ib.sleep_calls) <= 0.0500001
    assert sum(ib.sleep_calls) == pytest.approx(0.12)


def test_market_data_auto_annotation_no_data_and_public_upstream_block(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB], monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _ = live_adapter
    contract = stock_contract()
    empty = MarketPriceSnapshot(None, "none", 0, None, {}, "now")
    annotated = adapter._annotate_auto_snapshot(empty, selected_mode=None, attempts=[])
    assert annotated.status == "No usable price in auto mode"

    adapter._upstream_connected = False
    adapter._upstream_state = "upstream_disconnected"
    adapter._upstream_message = "internet lost"
    blocked = adapter.price_snapshot(contract)
    assert blocked.price is None
    assert blocked.upstream_connected is False
    assert "paused" in blocked.status

    adapter._upstream_connected = True
    adapter._market_data_type = 1
    monkeypatch.setattr(adapter, "_price_snapshot_for_active_mode", lambda *_args, **_kwargs: MarketPriceSnapshot(88.0, "last", 1, 1, {}, "now"))
    assert adapter.price_snapshot(contract).price == 88.0


def test_rth_status_uses_contract_hours_cache_and_fallback(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB], monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    now = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
    open_status = adapter._parse_liquid_hours_window("20260710:0930-1600", "America/New_York", now)
    assert open_status is not None and open_status.is_open is True
    assert adapter._parse_liquid_hours_window("", "America/New_York", now) is None
    assert adapter._parse_liquid_hours_window("20260710:CLOSED", "America/New_York", now).is_open is False
    assert adapter._fallback_us_equity_rth(now).source == "fallback_us_equity"

    ib.contract_details = [SimpleNamespace(liquidHours="20260710:0930-1600", timeZoneId="America/New_York")]
    monkeypatch.setattr("app.ib_adapter.datetime", SimpleNamespace(now=lambda tz: now, strptime=datetime.strptime))
    # Patch parser instead of timezone clock to keep this test deterministic.
    monkeypatch.setattr(adapter, "_parse_liquid_hours_window", lambda liquid, zone: RthStatus(True, "contract_liquid_hours", "open", "now", liquid, zone))
    status = adapter.regular_trading_hours_status(contract)
    assert status.is_open is True
    assert adapter.regular_trading_hours_status(contract) is status


def test_what_if_order_builders_preserve_optional_account_and_report_warnings(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    ib.next_trade = SimpleNamespace(
        orderState=SimpleNamespace(status="PreSubmitted", warningText="insufficient funds", initMarginChange="10", maintMarginChange="5", equityWithLoanChange="-10"),
        orderStatus=SimpleNamespace(status="PreSubmitted"),
    )
    trailing = adapter.what_if_trailing_stop(
        contract=contract,
        action="buy",
        quantity=5,
        trailing_percent=1.25,
        initial_stop_price=99.5,
        order_ref=f"{APP_ORDER_PREFIX}|AAPL|WHATIF",
        account="DU1",
        outside_rth=True,
    )
    assert trailing["ok"] is False
    order = ib.what_if_orders[-1][1]
    assert order.orderType == "TRAIL"
    assert order.whatIf is True
    assert order.transmit is True
    assert order.account == "DU1"

    ib.next_trade = None
    ib.next_order_state = SimpleNamespace(
        status="PreSubmitted",
        warningText="",
        initMarginChange="0",
        maintMarginChange=0,
        equityWithLoanChange="0.00",
    )
    market = adapter.what_if_market_order(contract=contract, action="SELL", quantity=5, order_ref="ref")
    assert market["ok"] is True
    assert market["initMarginChange"] == "0"
    assert ib.what_if_orders[-1][1].orderType == "MKT"
    assert ib.what_if_orders[-1][1].transmit is True


def test_live_order_builders_validate_and_return_broker_identity(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    contract = stock_contract()
    ib.next_trade = None
    trailing = adapter.place_trailing_stop(
        contract=contract,
        action="BUY",
        quantity=5,
        trailing_percent=1.0,
        initial_stop_price=99.0,
        order_ref=f"{APP_ORDER_PREFIX}|AAPL|BUY",
        account="",
        outside_rth=False,
    )
    assert isinstance(trailing, OrderHandle)
    assert trailing.order_id == 101 and trailing.perm_id == 202
    assert not hasattr(ib.placed_orders[-1][1], "account")

    market = adapter.place_market_order(
        contract=contract,
        action="SELL",
        quantity=5,
        order_ref=f"{APP_ORDER_PREFIX}|AAPL|SELL",
        tif="DAY",
        account="DU1",
    )
    assert market.raw["orderType"] == "MKT"
    assert ib.placed_orders[-1][1].account == "DU1"

    with pytest.raises(BrokerAdapterError, match="Unsupported"):
        adapter.place_market_order(contract=contract, action="HOLD", quantity=1, order_ref="x")
    with pytest.raises(BrokerAdapterError, match="greater than zero"):
        adapter.place_trailing_stop(contract=contract, action="BUY", quantity=0, trailing_percent=1, initial_stop_price=1, order_ref="x")


def test_cancel_poll_and_open_order_paths(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    order_ref = f"{APP_ORDER_PREFIX}|AAPL|BUY"
    order = FakeOrder(action="BUY", orderType="MKT", totalQuantity=2, orderRef=order_ref, orderId=101, permId=202)
    trade = SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=2, avgFillPrice=0.0, permId=202),
        fills=[],
    )
    ib.open_trades = [trade]
    ib.all_trades = [trade]
    adapter.refresh_open_trades_cache(force=True)
    ib.sleep_calls.clear()
    assert adapter.poll_order(order_ref).remaining == 2
    assert ib.sleep_calls == []
    assert adapter.open_app_orders()[0].order_ref == order_ref
    adapter.cancel_order(order_ref)
    assert ib.cancelled_orders == [order]
    with pytest.raises(BrokerAdapterError, match="Could not find"):
        adapter.cancel_order("missing")


def test_periodic_order_poll_cache_miss_requests_refresh_without_waiting(
    live_adapter: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = live_adapter
    adapter._last_open_trades_refresh_monotonic = 0.0
    ib.sleep_calls.clear()

    assert adapter.poll_order(f"{APP_ORDER_PREFIX}|AAPL|MISSING") is None

    assert ib.open_order_requests == 1
    assert ib.sleep_calls == []


def test_optional_int_execution_conversion_and_recovery(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    assert adapter._as_optional_int("12") == 12
    assert adapter._as_optional_int(0) is None
    assert adapter._as_optional_int("bad") is None
    valid = fill()
    item = adapter._execution_dict_from_fill(valid)
    assert item is not None and item["execution_id"] == "E1"
    assert adapter._execution_dict_from_fill(SimpleNamespace(execution=None)) is None
    assert adapter._execution_dict_from_fill(fill(shares=0)) is None

    ib.execution_values = [valid]
    ib.fill_values = [valid]
    recovered = adapter.recover_order_fill(
        order_ref=f"{APP_ORDER_PREFIX}|AAPL|BUY",
        order_id=101,
        perm_id=202,
        ticker="AAPL",
        account="DU1",
        action="BUY",
    )
    assert recovered is not None
    assert recovered.status == "Filled"
    assert recovered.filled == 2


def test_managed_accounts_recent_executions_and_positions(live_adapter: tuple[IbAsyncTwsAdapter, FakeIB]) -> None:
    adapter, ib = live_adapter
    ib.account_values = "DU1, DU2;DU1"
    assert adapter.managed_accounts() == ["DU1", "DU2"]

    first = fill(exec_id="E1")
    duplicate = fill(exec_id="E1")
    second = fill(exec_id="E2", side="SLD", price=101.0)
    ib.fill_values = [first]
    ib.execution_values = [duplicate, second]
    executions = adapter.recent_executions()
    assert [item["execution_id"] for item in executions] == ["E1", "E2"]

    contract = stock_contract()
    ib.position_values = [
        SimpleNamespace(account="DU1", contract=SimpleNamespace(conId=123, symbol="AAPL"), position=3),
        SimpleNamespace(account="DU2", contract=SimpleNamespace(conId=123, symbol="AAPL"), position=4),
        SimpleNamespace(account="DU1", contract=SimpleNamespace(conId=999, symbol="MSFT"), position=10),
    ]
    assert adapter.position_size(contract, account="DU1") == pytest.approx(3.0)
    assert adapter.position_size(contract) == pytest.approx(7.0)
    assert adapter.position_size(stock_contract("TSLA", 777)) is None



def test_dependency_loader_imports_required_types(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake_module = types.ModuleType("ib_async")
    fake_module.IB = type("IB", (), {})
    fake_module.Order = type("Order", (), {})
    fake_module.Stock = type("Stock", (), {})
    monkeypatch.setitem(sys.modules, "ib_async", fake_module)

    IB, Order, Stock = IbAsyncTwsAdapter()._require_ib_async()
    assert (IB.__name__, Order.__name__, Stock.__name__) == ("IB", "Order", "Stock")
