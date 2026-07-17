"""Tests built from documented IBKR TWS API callback shapes.

These examples intentionally run offline. They verify that the app's normalized
models and helper logic can handle representative data from reqMatchingSymbols,
reqMktData/tick callbacks, openOrder/orderStatus, execDetails/commissionReport,
and what-if margin checks.
"""

from __future__ import annotations

from app.ib_adapter import ContractSearchResult, IbAsyncTwsAdapter, MarketPriceSnapshot, PolledOrderState
from app.models import StrategySettings, suggested_hard_risk_defaults
from tests.fixtures import ibkr_api_documented_examples as fx


def test_documented_contract_search_example_normalizes_to_supported_stock():
    raw_contract = fx.MATCHING_SYMBOL_STOCK["contract"]
    result = ContractSearchResult(
        symbol=raw_contract["symbol"],
        sec_type=raw_contract["secType"],
        currency=raw_contract["currency"],
        exchange=raw_contract["exchange"],
        primary_exchange=raw_contract["primaryExchange"],
        con_id=raw_contract["conId"],
        local_symbol=raw_contract["localSymbol"],
        trading_class=raw_contract["tradingClass"],
        description=fx.MATCHING_SYMBOL_STOCK["description"],
        derivative_sec_types=fx.MATCHING_SYMBOL_STOCK["derivativeSecTypes"],
    )

    assert result.supported is True
    assert "NASDAQ" in result.label()
    assert result.to_dict()["con_id"] == 265598


def test_documented_market_data_fields_choose_best_available_price():
    fields = dict(fx.MARKET_DATA_TICKS)
    price, source = IbAsyncTwsAdapter._choose_price(fields)

    assert price == fx.MARKET_DATA_TICKS["marketPrice"]
    assert source == "marketPrice"


def test_documented_delayed_market_data_fields_can_fallback_to_delayed_midpoint():
    fields = dict(fx.DELAYED_MARKET_DATA_TICKS)
    fields["delayedBidAskMidpoint"] = (fields["delayedBid"] + fields["delayedAsk"]) / 2.0
    price, source = IbAsyncTwsAdapter._choose_price(fields)

    assert price == 100.10
    assert source in {"delayedBidAskMidpoint", "delayedMarkPrice", "delayedLast"}


def test_documented_order_callbacks_normalize_to_polled_order_state():
    order = fx.OPEN_ORDER_CALLBACK
    status = fx.ORDER_STATUS_CALLBACK
    state = PolledOrderState(
        order_ref=order["orderRef"],
        order_id=status["orderId"],
        perm_id=status["permId"],
        status=status["status"],
        filled=int(status["filled"]),
        remaining=int(status["remaining"]),
        avg_fill_price=float(status["avgFillPrice"]),
        commission=0.0,
        executions=[],
        raw={"openOrder": order, "orderStatus": status},
    )

    assert state.order_ref.startswith("IBKRBOT|")
    assert state.status == "Submitted"
    assert state.remaining == order["totalQuantity"]


def test_documented_execution_and_commission_example_can_be_merged():
    execution = dict(fx.EXECUTION_DETAILS_CALLBACK)
    commission = fx.COMMISSION_REPORT_CALLBACK
    execution["commission"] = commission["commission"]
    execution["currency"] = commission["currency"]

    assert execution["execId"] == commission["execId"]
    assert execution["side"] == "BOT"
    assert execution["commission"] == 1.23


def test_documented_what_if_examples_expose_margin_and_warning_fields():
    ok = fx.WHAT_IF_ORDER_STATE
    rejected = fx.WHAT_IF_REJECTED_ORDER_STATE

    assert "initMarginChange" in ok
    assert "maintMarginChange" in ok
    assert rejected["warningText"].lower().find("insufficient") >= 0


def test_market_price_snapshot_accepts_documented_field_set():
    snapshot = MarketPriceSnapshot(
        price=fx.MARKET_DATA_TICKS["marketPrice"],
        source="marketPrice",
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields=dict(fx.MARKET_DATA_TICKS),
        timestamp="2025-07-05T14:32:01+00:00",
        status="OK",
        api_data_received=True,
        api_data_field_count=6,
    )

    data = snapshot.to_dict()
    assert data["fields"]["bid"] == 101.18
    assert data["source"] == "marketPrice"


def test_investment_scaled_risk_defaults_and_disabled_default_flags():
    defaults = suggested_hard_risk_defaults(20_000)
    settings = StrategySettings(ticker="AAPL")

    assert defaults["max_daily_loss_ticker"] == 0.0
    assert defaults["max_daily_loss_total"] == 0.0
    assert settings.reinvest_profits is True
    assert settings.auto_repeat is True
    assert settings.block_delayed_data_in_live is True
    assert settings.what_if_check_enabled is True
    assert settings.stale_data_guard_enabled is True
    assert settings.atr_adaptive_enabled is True
    assert settings.atr_block_new_buy_until_ready is True
    assert settings.volatility_filter_enabled is False
    assert settings.session_timing_guard_enabled is True
