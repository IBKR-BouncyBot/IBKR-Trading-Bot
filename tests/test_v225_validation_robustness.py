from app.models import ConnectionSettings, StrategySettings, atr_from_price_history


def test_connection_validation_reports_malformed_numeric_inputs_without_raising():
    settings = ConnectionSettings(host=None, port="not-a-port", client_id="bad-client", market_data_type="bad-market")

    errors = settings.validate()

    assert any("Host is required" in item for item in errors)
    assert any("Port" in item for item in errors)
    assert any("Client ID" in item for item in errors)
    assert any("Market data type" in item for item in errors)


def test_strategy_validation_reports_malformed_numeric_inputs_without_raising():
    settings = StrategySettings(
        ticker=None,
        investment_amount="bad-investment",
        initial_drop_pct="bad-drop",
        buy_rebound_trail_pct="bad-buy-trail",
        rise_trigger_pct="bad-profit",
        sell_trailing_stop_pct="bad-sell-trail",
        atr_adaptive_enabled=True,
        atr_period="bad-period",
        atr_bar_seconds="bad-bar",
        atr_initial_drop_multiplier="bad-initial-multiplier",
        atr_buy_rebound_multiplier="bad-buy-multiplier",
        atr_minimum_profit_multiplier="bad-profit-multiplier",
        atr_sell_trail_multiplier="bad-sell-multiplier",
        atr_min_pct="bad-min",
        atr_max_pct="bad-max",
        stale_data_guard_enabled=True,
        max_selected_price_age_seconds="bad-selected-age",
        max_bid_ask_age_seconds="bad-bid-ask-age",
        max_rth_status_age_seconds="bad-rth-age",
        volatility_filter_enabled=True,
        volatility_window_seconds="bad-window",
        max_recent_price_move_pct="bad-move",
        session_timing_guard_enabled=True,
        no_new_buy_first_minutes="bad-open",
        no_new_buy_last_minutes="bad-close",
        cancel_buy_before_close_minutes="bad-cancel",
    )

    errors = settings.validate()

    expected_fragments = [
        "Ticker is required",
        "Investment amount",
        "Initial drop %",
        "Buy rebound/trailing %",
        "Minimum profit %",
        "Sell trailing-stop %",
        "ATR period",
        "ATR bar size",
        "ATR initial-drop multiplier",
        "ATR buy-rebound multiplier",
        "ATR minimum-profit multiplier",
        "ATR sell-trail multiplier",
        "ATR min/max percentage bounds",
        "Stale-data guard ages",
        "Volatility window",
        "Max recent price move %",
        "Session timing guard minutes",
    ]
    for fragment in expected_fragments:
        assert any(fragment in item for item in errors), fragment


def test_atr_history_accepts_same_samples_without_extra_copy_behavior_change():
    points = [
        (0, 100.0),
        (1, "bad"),
        (60, 101.0),
        (120, 102.0),
        (180, 101.5),
        (240, 103.0),
    ]

    result = atr_from_price_history(points, period=2, bar_seconds=60)

    assert result["ready"] is True
    assert result["bars_available"] == 5
    assert result["true_ranges_used"] == 2
    assert round(result["atr"], 4) == 1.0
    assert round(result["atr_pct"], 4) == 0.9709
