from app.models import (
    initial_sell_stop_price_for_min_profit,
    projected_minimum_profit_levels,
    required_market_rise_pct_for_min_profit,
    required_sell_trigger_price_for_min_profit,
)


def test_required_market_rise_includes_sell_trail_distance():
    required = required_market_rise_pct_for_min_profit(10.0, 1.0)
    assert round(required, 2) == 11.12


def test_required_sell_trigger_places_initial_stop_at_min_profit():
    avg_buy = 95.0
    min_profit = 10.0
    sell_trail = 1.0
    trigger = required_sell_trigger_price_for_min_profit(avg_buy, min_profit, sell_trail)
    stop = trigger * (1.0 - sell_trail / 100.0)

    assert round(initial_sell_stop_price_for_min_profit(avg_buy, min_profit), 2) == 104.50
    assert round(trigger, 2) == 105.56
    assert round(stop, 2) == 104.50


def test_projected_levels_scale_with_non_100_anchor():
    levels = projected_minimum_profit_levels(5.0, 2.0, 10.0, 1.0, anchor=250.0)

    assert round(levels["drop_trigger"], 2) == 237.50
    assert round(levels["projected_buy_stop"], 2) == 242.25
    assert round(levels["minimum_sell_stop"], 2) == 266.48
    assert round(levels["profit_vs_projected_buy_pct"], 2) == 10.00


def test_slippage_buffer_raises_sell_trigger_for_adverse_fill_buffer():
    avg_buy = 100.0
    trigger = required_sell_trigger_price_for_min_profit(
        avg_buy,
        minimum_profit_pct=10.0,
        sell_trailing_stop_pct=1.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=0.5,
    )
    first_stop = trigger * 0.99
    simulated_adverse_fill = first_stop * 0.995

    assert round(first_stop, 2) == round(100.0 * 1.10 / 0.995, 2)
    assert round(simulated_adverse_fill, 2) == 110.00


def test_market_data_does_not_rewrite_user_owned_hard_risk_defaults():
    from app.models import suggested_hard_risk_defaults

    calm = suggested_hard_risk_defaults(
        10_000,
        market_price=100.0,
        bid=99.99,
        ask=100.01,
        previous_close=99.75,
    )
    stressed = suggested_hard_risk_defaults(
        10_000,
        market_price=4.50,
        bid=4.40,
        ask=4.60,
        previous_close=5.50,
        recent_move_pct=8.0,
    )

    assert calm["max_daily_loss_ticker"] == 0.0
    assert stressed["max_daily_loss_ticker"] == 0.0
    assert stressed["max_cycles_per_ticker_day"] == calm["max_cycles_per_ticker_day"] == 0
    assert stressed["max_spread_pct"] == calm["max_spread_pct"] == 1.0
