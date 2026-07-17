"""Synthetic IBKR API callback examples used by tests.

The shapes mirror fields documented for common TWS API callbacks. They are not
live IBKR responses and they do not contain account data. They let tests verify
that the app can normalize representative market-data, contract-search, order,
execution, and what-if/margin data without connecting to TWS or IB Gateway.
"""

APP_ORDER_REF = "IBKRBOT|AAPL|CYCLE-000001|BUY_TRAIL"
SELL_ORDER_REF = "IBKRBOT|AAPL|CYCLE-000001|SELL_TRAIL"

MATCHING_SYMBOL_STOCK = {
    "contract": {
        "symbol": "AAPL",
        "secType": "STK",
        "currency": "USD",
        "exchange": "SMART",
        "primaryExchange": "NASDAQ",
        "conId": 265598,
        "localSymbol": "AAPL",
        "tradingClass": "NMS",
    },
    "description": "APPLE INC",
    "derivativeSecTypes": ["OPT", "CFD"],
}

MARKET_DATA_TICKS = {
    "last": 101.20,
    "bid": 101.18,
    "ask": 101.22,
    "close": 100.50,
    "delayedLast": None,
    "delayedBid": None,
    "delayedAsk": None,
    "markPrice": 101.21,
    "marketPrice": 101.20,
    "marketDataType": 1,
}

DELAYED_MARKET_DATA_TICKS = {
    "last": None,
    "bid": None,
    "ask": None,
    "close": None,
    "delayedLast": 100.10,
    "delayedBid": 100.05,
    "delayedAsk": 100.15,
    "delayedClose": 99.80,
    "delayedMarkPrice": 100.10,
    "marketDataType": 3,
}

OPEN_ORDER_CALLBACK = {
    "orderId": 1001,
    "permId": 700001,
    "orderRef": APP_ORDER_REF,
    "action": "BUY",
    "orderType": "TRAIL",
    "totalQuantity": 98,
    "trailingPercent": 1.0,
    "trailStopPrice": 99.95,
    "outsideRth": False,
    "status": "Submitted",
}

ORDER_STATUS_CALLBACK = {
    "orderId": 1001,
    "status": "Submitted",
    "filled": 0,
    "remaining": 98,
    "avgFillPrice": 0.0,
    "permId": 700001,
}

EXECUTION_DETAILS_CALLBACK = {
    "orderId": 1001,
    "permId": 700001,
    "execId": "0001.abcdef.01",
    "orderRef": APP_ORDER_REF,
    "side": "BOT",
    "shares": 98,
    "price": 101.02,
    "avgPrice": 101.02,
    "time": "20250705 14:32:01 US/Eastern",
}

COMMISSION_REPORT_CALLBACK = {
    "execId": "0001.abcdef.01",
    "commission": 1.23,
    "currency": "USD",
    "realizedPNL": 0.0,
}

WHAT_IF_ORDER_STATE = {
    "status": "PreSubmitted",
    "warningText": "",
    "initMarginChange": "1250.00",
    "maintMarginChange": "1200.00",
    "equityWithLoanChange": "-10000.00",
}

WHAT_IF_REJECTED_ORDER_STATE = {
    "status": "Inactive",
    "warningText": "Rejected: insufficient buying power",
    "initMarginChange": "0.00",
    "maintMarginChange": "0.00",
    "equityWithLoanChange": "0.00",
}
