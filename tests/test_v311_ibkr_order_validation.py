"""v3.1.1 market-rule, what-if, broker-error, and rejection regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import (
    APP_ORDER_PREFIX,
    BrokerAdapterError,
    IbAsyncTwsAdapter,
    PolledOrderState,
    QualifiedContract,
)
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyAction, StrategyEngine
from tests.test_comprehensive_ib_adapter import FakeIB, FakeOrder, FakeStock
from tests.test_controller_headless import _install_qt_stub


@pytest.fixture
def adapter_with_ib(monkeypatch: pytest.MonkeyPatch) -> tuple[IbAsyncTwsAdapter, FakeIB]:
    adapter = IbAsyncTwsAdapter()
    ib = FakeIB()
    adapter.ib = ib
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "ready"
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (FakeIB, FakeOrder, FakeStock))
    return adapter, ib


def _iren_details(contract: Any) -> Any:
    return SimpleNamespace(
        contract=contract,
        minTick=0.0001,
        validExchanges="SMART,NASDAQ",
        marketRuleIds="557,26",
    )


def _qualified_iren(adapter: IbAsyncTwsAdapter, ib: FakeIB) -> QualifiedContract:
    contract = FakeStock("IREN", "SMART", "USD", primaryExchange="NASDAQ")
    contract.conId = 526906130
    ib.qualified_contracts = [contract]
    ib.contract_details = [_iren_details(contract)]
    return adapter.qualify_stock("IREN", "SMART", "USD", "NASDAQ", 526906130)


def _trade(order_ref: str, order_id: int = 101, *, status: str = "Inactive") -> Any:
    order = FakeOrder(
        action="BUY",
        orderType="TRAIL",
        totalQuantity=10,
        orderRef=order_ref,
        orderId=order_id,
        permId=202,
    )
    return SimpleNamespace(
        contract=FakeStock("IREN", "SMART", "USD", primaryExchange="NASDAQ"),
        order=order,
        orderStatus=SimpleNamespace(
            status=status,
            filled=0,
            remaining=10,
            avgFillPrice=0.0,
            permId=202,
        ),
        fills=[],
    )


def _controller_cycle(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    settings = StrategySettings(
        ticker="IREN",
        investment_amount=10_000.0,
        hard_risk_limits_enabled=False,
        stale_data_guard_enabled=False,
        session_timing_guard_enabled=False,
        volatility_filter_enabled=False,
        what_if_check_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 42.50, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = f"{APP_ORDER_PREFIX}|IREN|CYCLE-000001|BUY_TRAIL"
    cycle.quantity = 234
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return controller, cycle


def _polled(
    cycle: Any,
    status: str,
    *,
    errors: list[dict[str, Any]] | None = None,
) -> PolledOrderState:
    values = list(errors or [])
    return PolledOrderState(
        order_ref=cycle.buy_order_ref,
        order_id=101,
        perm_id=202,
        status=status,
        filled=0,
        remaining=234,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={
            "status": status,
            "broker_errors": values,
            "broker_error": values[-1] if values else None,
        },
    )


def test_iren_contract_maps_smart_route_to_market_rule_557(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = _qualified_iren(adapter, ib)

    assert contract.min_tick == pytest.approx(0.0001)
    assert contract.market_rule_id == 557
    assert contract.market_rule_exchange == "SMART"
    assert contract.market_rule_advertised is True


def test_market_rule_mapping_preserves_empty_rule_positions(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = FakeStock("TEST", "NASDAQ", "USD", primaryExchange="NASDAQ")
    contract.conId = 9
    ib.qualified_contracts = [contract]
    ib.contract_details = [
        SimpleNamespace(
            contract=contract,
            minTick=0.0001,
            validExchanges="SMART,NASDAQ",
            marketRuleIds=",26",
        )
    ]

    qualified = adapter.qualify_stock("TEST", "NASDAQ", "USD", "NASDAQ", 9)

    assert qualified.market_rule_id == 26
    assert qualified.market_rule_exchange == "NASDAQ"
    assert qualified.market_rule_advertised is True


def test_blank_rule_for_requested_route_does_not_inherit_another_exchange_rule(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = FakeStock("TEST", "SMART", "USD", primaryExchange="NASDAQ")
    contract.conId = 10
    ib.qualified_contracts = [contract]
    ib.contract_details = [
        SimpleNamespace(
            contract=contract,
            minTick=0.0001,
            validExchanges="SMART,NASDAQ",
            marketRuleIds=",26",
        )
    ]

    qualified = adapter.qualify_stock("TEST", "SMART", "USD", "NASDAQ", 10)

    assert qualified.market_rule_id is None
    assert qualified.market_rule_exchange == "SMART"
    assert qualified.market_rule_advertised is True
    with pytest.raises(BrokerAdapterError, match="advertised market-rule pricing"):
        adapter.normalize_order_price(qualified, 42.5996, "up")


def test_iren_stop_uses_price_band_instead_of_smallest_contract_min_tick(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = _qualified_iren(adapter, ib)
    ib.market_rule_values[557] = [
        SimpleNamespace(lowEdge=0.0, increment=0.0001),
        SimpleNamespace(lowEdge=1.0, increment=0.01),
    ]

    buy = adapter.normalize_order_price(contract, 42.5996, "up")
    sell = adapter.normalize_order_price(contract, 42.5996, "down")

    assert buy.normalized_price == pytest.approx(42.60)
    assert sell.normalized_price == pytest.approx(42.59)
    assert buy.increment == pytest.approx(0.01)
    assert buy.market_rule_id == 557


def test_market_rule_rounding_rechecks_a_crossed_price_boundary_and_caches_rule(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = _qualified_iren(adapter, ib)
    ib.market_rule_values[557] = [
        SimpleNamespace(lowEdge=0.0, increment=0.01),
        SimpleNamespace(lowEdge=1.0, increment=0.05),
    ]

    assert adapter.normalize_order_price(contract, 0.999, "up").normalized_price == pytest.approx(1.0)
    assert adapter.normalize_order_price(contract, 1.021, "up").normalized_price == pytest.approx(1.05)
    assert ib.market_rule_requests == [557]


def test_advertised_but_unresolved_market_rule_fails_closed(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib
    contract = QualifiedContract(
        ticker="IREN",
        con_id=1,
        raw=FakeStock("IREN", "SMART", "USD"),
        min_tick=0.0001,
        market_rule_advertised=True,
    )

    with pytest.raises(BrokerAdapterError, match="advertised market-rule pricing"):
        adapter.normalize_order_price(contract, 42.5996, "up")


def test_empty_market_rule_response_fails_closed(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = _qualified_iren(adapter, ib)

    with pytest.raises(BrokerAdapterError, match="no usable price increments"):
        adapter.normalize_order_price(contract, 42.5996, "up")


def test_contract_without_advertised_market_rule_keeps_min_tick_fallback(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib
    contract = QualifiedContract(
        ticker="TEST",
        con_id=1,
        raw=FakeStock("TEST", "SMART", "USD"),
        min_tick=0.05,
    )

    result = adapter.normalize_order_price(contract, 10.021, "up")

    assert result.normalized_price == pytest.approx(10.05)
    assert result.source == "contract_min_tick"


def test_what_if_uses_transmit_true_and_accepts_legitimate_zero_margin(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, ib = adapter_with_ib
    contract = QualifiedContract("IREN", 1, FakeStock("IREN", "SMART", "USD"))
    ib.next_order_state = SimpleNamespace(
        status="PreSubmitted",
        warningText="",
        initMarginChange="0",
        maintMarginChange=0,
        equityWithLoanChange="0.00",
    )

    result = adapter.what_if_trailing_stop(
        contract=contract,
        action="BUY",
        quantity=234,
        trailing_percent=0.14,
        initial_stop_price=42.60,
        order_ref=f"{APP_ORDER_PREFIX}|IREN|BUY|WHATIF",
    )

    order = ib.what_if_orders[-1][1]
    assert result["ok"] is True
    assert order.whatIf is True
    assert order.transmit is True
    assert ib.placed_orders == []


@pytest.mark.parametrize(
    ("state", "message_fragment"),
    [
        (None, "no OrderState"),
        (
            SimpleNamespace(
                status="ValidationError",
                warningText="",
                initMarginChange="",
                maintMarginChange="",
                equityWithLoanChange="",
            ),
            "ValidationError",
        ),
        (
            SimpleNamespace(
                status="PreSubmitted",
                warningText="insufficient funds",
                initMarginChange="10",
                maintMarginChange="5",
                equityWithLoanChange="-10",
            ),
            "insufficient funds",
        ),
        (
            SimpleNamespace(
                status="PreSubmitted",
                warningText="",
                initMarginChange="1.7976931348623157E308",
                maintMarginChange="1.7976931348623157E308",
                equityWithLoanChange="1.7976931348623157E308",
            ),
            "No usable margin",
        ),
    ],
)
def test_what_if_fails_closed_for_invalid_or_missing_results(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
    state: Any,
    message_fragment: str,
) -> None:
    adapter, ib = adapter_with_ib
    ib.next_order_state = state
    if state is None:
        ib.next_trade = SimpleNamespace(orderState=None)
    contract = QualifiedContract("IREN", 1, FakeStock("IREN", "SMART", "USD"))

    result = adapter.what_if_market_order(
        contract=contract,
        action="BUY",
        quantity=1,
        order_ref=f"{APP_ORDER_PREFIX}|IREN|MKT|WHATIF",
    )

    assert result["ok"] is False
    assert message_fragment.lower() in result["message"].lower()


def test_known_app_order_error_is_retained_and_added_to_polled_state(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib
    order_ref = f"{APP_ORDER_PREFIX}|IREN|CYCLE-000001|BUY_TRAIL"
    trade = _trade(order_ref)
    adapter._trades_by_ref[order_ref] = trade

    adapter._on_ib_error(101, 201, "Order rejected - reason: Invalid Price", trade.contract, '{"reason":"price"}')

    events = adapter.drain_broker_events()
    state = adapter._to_polled_order_state(trade)
    assert len(events) == 1
    assert events[0]["event_type"] == "ORDER_ERROR"
    assert events[0]["error_code"] == 201
    assert events[0]["order_ref"] == order_ref
    assert events[0]["advanced_reject_json"] == '{"reason":"price"}'
    assert state is not None
    assert state.raw["broker_error"]["message"].endswith("Invalid Price")


def test_callback_race_error_binds_after_place_order_returns(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = adapter_with_ib
    contract = QualifiedContract("IREN", 1, FakeStock("IREN", "SMART", "USD"))
    order_ref = f"{APP_ORDER_PREFIX}|IREN|CYCLE-000001|BUY_TRAIL"

    def place_order(raw_contract: Any, order: Any) -> Any:
        trade = SimpleNamespace(
            contract=raw_contract,
            order=order,
            orderStatus=SimpleNamespace(
                status="Inactive",
                filled=0,
                remaining=order.totalQuantity,
                avgFillPrice=0.0,
                permId=order.permId,
            ),
            fills=[],
        )
        adapter._on_ib_error(order.orderId, 201, "Order rejected - reason: Invalid Price", raw_contract)
        return trade

    monkeypatch.setattr(ib, "placeOrder", place_order)
    handle = adapter.place_trailing_stop(
        contract=contract,
        action="BUY",
        quantity=10,
        trailing_percent=0.14,
        initial_stop_price=42.60,
        order_ref=order_ref,
    )

    assert handle.status == "Inactive"
    events = adapter.drain_broker_events()
    assert len(events) == 1
    assert events[0]["order_ref"] == order_ref
    assert adapter.poll_order(order_ref).raw["broker_error"]["error_code"] == 201


def test_known_app_order_retains_nonconnectivity_error_even_when_code_is_ambiguous(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib
    order_ref = f"{APP_ORDER_PREFIX}|IREN|CYCLE-000001|BUY_TRAIL"
    trade = _trade(order_ref, order_id=88)
    adapter._trades_by_ref[order_ref] = trade

    adapter._on_ib_error(88, 200, "No security definition has been found", trade.contract)

    events = adapter.drain_broker_events()
    assert len(events) == 1
    assert events[0]["order_ref"] == order_ref
    assert events[0]["error_code"] == 200


def test_unknown_contract_error_is_not_cached_as_an_order_callback_race(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib

    adapter._on_ib_error(89, 200, "No security definition has been found")

    assert adapter.drain_broker_events() == []
    assert 89 not in adapter._pending_order_errors


def test_manual_order_error_is_not_attributed_to_the_app(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
) -> None:
    adapter, _ = adapter_with_ib
    trade = _trade("MANUAL_ORDER", order_id=77)
    adapter._trades_by_ref["MANUAL_ORDER"] = trade

    adapter._on_ib_error(77, 201, "Order rejected - reason: Invalid Price", trade.contract)

    assert adapter.drain_broker_events() == []
    assert 77 not in adapter._pending_order_errors


def test_unknown_order_error_race_cache_is_bounded_and_expires(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = adapter_with_ib
    clock = [100.0]
    monkeypatch.setattr("app.ib_adapter.time.monotonic", lambda: clock[0])

    for order_id in range(1, 400):
        adapter._on_ib_error(order_id, 201, "Order rejected")

    assert sum(len(values) for values in adapter._pending_order_errors.values()) <= 256
    clock[0] += 31.0
    adapter._purge_pending_order_errors()
    assert adapter._pending_order_errors == {}


def test_controller_persists_and_surfaces_order_error_event(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)
    event = {
        "event_type": "ORDER_ERROR",
        "created_at": "2026-07-22T18:17:21+00:00",
        "order_ref": cycle.buy_order_ref,
        "order_id": 101,
        "perm_id": 202,
        "ticker": "IREN",
        "error_code": 201,
        "message": "Order rejected - reason: Invalid Price",
    }
    controller.adapter = SimpleNamespace(drain_broker_events=lambda: [event])

    controller._drain_broker_events()

    audit = controller.storage.cycle_audit_details(cycle.id)
    assert controller.status.startswith("IBKR order error 201")
    assert any(row["event_type"] == "BROKER_ORDER_ERROR" for row in audit["decision_events"])
    assert any(row["event_type"] == "ORDER_ERROR" for row in controller.storage.recent_broker_events())


def test_buy_inactive_or_rejected_enters_error_without_automatic_retry(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)
    error = {"error_code": 201, "message": "Order rejected - reason: Invalid Price"}

    controller._handle_buy_order_poll(cycle, _polled(cycle, "Inactive", errors=[error]))

    assert controller.active_cycle.stage == Stage.ERROR
    assert controller.active_cycle.buy_order_ref == cycle.buy_order_ref
    assert "IBKR error 201" in controller.active_cycle.error_message
    assert "automatic fresh-cycle retry" in controller.active_cycle.error_message


@pytest.mark.parametrize(
    "errors",
    [
        [],
        [{"error_code": 202, "message": "Order canceled - reason:"}],
    ],
)
def test_confirmed_buy_cancellation_still_resets_to_stage_1(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    errors: list[dict[str, Any]],
) -> None:
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)

    controller._handle_buy_order_poll(cycle, _polled(cycle, "Cancelled", errors=errors))

    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.buy_order_ref is None


def test_cancelled_status_with_real_rejection_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)
    error = {"error_code": 110, "message": "The price does not conform to the minimum variation"}

    controller._handle_buy_order_poll(cycle, _polled(cycle, "Cancelled", errors=[error]))

    assert controller.active_cycle.stage == Stage.ERROR
    assert "minimum variation" in controller.active_cycle.error_message


def test_controller_uses_market_rule_for_stop_and_sizing(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = adapter_with_ib
    contract = _qualified_iren(adapter, ib)
    ib.market_rule_values[557] = [
        SimpleNamespace(lowEdge=0.0, increment=0.0001),
        SimpleNamespace(lowEdge=1.0, increment=0.01),
    ]
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)
    controller.adapter = adapter
    controller.contract = contract
    controller.price_snapshot = {
        "price": 42.50,
        "fields": {"ask": 42.51, "last": 42.50, "marketPrice": 42.50},
    }
    payload = {
        "quantity": 234,
        "trailing_percent": 0.14,
        "initial_stop_price": 42.5996,
        "order_ref": cycle.buy_order_ref,
    }

    normalized, message = controller._normalize_trailing_order_payload(cycle, payload, "BUY")

    assert normalized["initial_stop_price"] == pytest.approx(42.60)
    assert normalized["sizing_price"] == pytest.approx(42.60)
    assert normalized["market_rule_id"] == 557
    assert normalized["price_increment_source"] == "market_rule"
    assert "market rule 557" in message


def test_unresolved_market_rule_blocks_before_order_intent_or_submission(
    adapter_with_ib: tuple[IbAsyncTwsAdapter, FakeIB],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, ib = adapter_with_ib
    contract = QualifiedContract(
        ticker="IREN",
        con_id=1,
        raw=FakeStock("IREN", "SMART", "USD"),
        min_tick=0.0001,
        market_rule_advertised=True,
    )
    controller, cycle = _controller_cycle(tmp_path, monkeypatch)
    controller.adapter = adapter
    controller.contract = contract
    controller.connected = True
    controller.price_snapshot = {"price": 42.50, "fields": {"ask": 42.51, "last": 42.50}}
    controller._latest_connectivity = {
        "local_connected": True,
        "upstream_connected": True,
        "trading_ready": True,
    }
    action = StrategyAction(
        "PLACE_BUY_TRAIL",
        {
            "quantity": 234,
            "trailing_percent": 0.14,
            "initial_stop_price": 42.5996,
            "order_ref": cycle.buy_order_ref,
        },
    )
    monkeypatch.setattr(controller, "_update_rth_status", lambda _contract: {"is_open": True})

    controller._place_trailing_order(cycle, action, "BUY")

    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.buy_status == "SubmitFailed"
    assert "Order-price validation blocked" in controller.active_cycle.error_message
    assert ib.placed_orders == []
    with controller.storage.connect() as con:
        row = con.execute("SELECT 1 FROM orders WHERE order_ref=?", (cycle.buy_order_ref,)).fetchone()
    assert row is None
