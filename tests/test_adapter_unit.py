from types import SimpleNamespace

from app.ib_adapter import IbAsyncTwsAdapter


def test_fill_matching_requires_expected_side():
    adapter = IbAsyncTwsAdapter()
    fill = SimpleNamespace(
        execution=SimpleNamespace(orderRef="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL", orderId=1, permId=2, side="SLD"),
        contract=SimpleNamespace(symbol="AAPL"),
        order=SimpleNamespace(orderRef="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL"),
    )

    assert adapter._fill_matches_order(
        fill,
        order_ref="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL",
        order_id=1,
        perm_id=2,
        ticker="AAPL",
        action="BUY",
    ) is False


def test_fill_matching_accepts_order_ref_even_without_order_id():
    adapter = IbAsyncTwsAdapter()
    fill = SimpleNamespace(
        execution=SimpleNamespace(orderRef="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL", orderId=0, permId=0, side="BOT"),
        contract=SimpleNamespace(symbol="AAPL"),
        order=SimpleNamespace(orderRef=""),
    )

    assert adapter._fill_matches_order(
        fill,
        order_ref="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL",
        order_id=None,
        perm_id=None,
        ticker="AAPL",
        action="BUY",
    ) is True


def test_polled_state_from_multiple_fills_uses_weighted_average_and_commission():
    adapter = IbAsyncTwsAdapter()
    fills = [
        SimpleNamespace(execution=SimpleNamespace(shares=2, price=100.0, avgPrice=100.0, execId="E1", side="BOT", time="T1"), commissionReport=SimpleNamespace(commission=0.5, currency="USD")),
        SimpleNamespace(execution=SimpleNamespace(shares=3, price=110.0, avgPrice=106.0, execId="E2", side="BOT", time="T2"), commissionReport=SimpleNamespace(commission=0.75, currency="USD")),
    ]

    state = adapter._polled_state_from_fills(
        fills,
        order_ref="IBKRBOT|AAPL|CYCLE-000001|X|BUY_TRAIL",
        order_id=1,
        perm_id=2,
        action="BUY",
    )

    assert state is not None
    assert state.filled == 5
    assert round(state.avg_fill_price, 2) == 106.00
    assert state.commission == 1.25


def test_price_signature_ignores_nan_like_missing_values_by_choose_price():
    price, source = IbAsyncTwsAdapter._choose_price({
        "marketPrice": None,
        "bidAskMidpoint": None,
        "last": 0.0,
        "delayedLast": 12.34,
    })

    # _choose_price receives cleaned fields in production. This test documents
    # that non-None values win in priority order at this layer.
    assert price == 0.0
    assert source == "last"
