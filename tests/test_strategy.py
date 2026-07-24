from app.models import Stage, StrategySettings, projected_minimum_profit_levels
from app.strategy import StrategyEngine


def settings() -> StrategySettings:
    return StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5,
        buy_rebound_trail_pct=2,
        rise_trigger_pct=3,  # UI label: Minimum profit %
        sell_trailing_stop_pct=1,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=False,
    )


def test_anchor_resets_upward_and_then_places_buy_trail_action():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, actions = StrategyEngine.on_price_update(cycle, 110.0)
    assert cycle.anchor_price == 110.0
    assert round(cycle.drop_trigger_price, 2) == 104.50
    assert actions == []

    cycle, actions = StrategyEngine.on_price_update(cycle, 104.0)
    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert cycle.quantity == 9
    assert len(actions) == 1
    assert actions[0].action_type == "PLACE_BUY_TRAIL"
    assert round(actions[0].payload["initial_stop_price"], 2) == 106.08


def test_partial_buy_fill_cancels_remaining_and_waits_for_terminal_buy_status():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle = StrategyEngine.on_order_submitted(cycle, cycle.buy_order_ref or "", 101, 1001, "Submitted")

    cycle, actions = StrategyEngine.on_buy_fill(cycle, filled_qty=3, avg_fill_price=95.0, status="Submitted")

    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert cycle.buy_filled_qty == 3
    # Minimum profit is protected versus the actual average buy fill.
    # Avg buy 95, min profit 3%, sell trail 1% => trigger 95 * 1.03 / 0.99.
    assert round(cycle.rise_trigger_price, 2) == 98.84
    assert len(actions) == 1
    assert actions[0].action_type == "CANCEL_ORDER"
    assert cycle.buy_remainder_cancel_requested is True

    cycle, actions = StrategyEngine.on_buy_fill(
        cycle,
        filled_qty=3,
        avg_fill_price=95.0,
        status="Cancelled",
    )
    assert cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert cycle.buy_remainder_cancel_requested is False
    assert actions == []


def test_minimum_profit_trigger_places_sell_trail_action():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled")

    cycle, actions = StrategyEngine.on_price_update(cycle, 98.85)

    assert cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert len(actions) == 1
    assert actions[0].action_type == "PLACE_SELL_TRAIL"
    assert actions[0].payload["quantity"] == 10
    assert round(actions[0].payload["initial_stop_price"], 2) == 97.86


def test_completed_cycle_calculates_net_pnl():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled", commission=1.0)
    cycle = StrategyEngine.on_sell_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled", commission=1.5)

    assert cycle.stage == Stage.CYCLE_COMPLETE
    assert cycle.gross_pnl == 50.0
    assert cycle.net_pnl == 47.5


def test_cycle_can_start_waiting_for_first_price_tick():
    cycle = StrategyEngine.start_cycle_waiting_for_price(settings(), 1, "", 0.0)
    assert cycle.stage == Stage.WAIT_INITIAL_DROP
    assert cycle.anchor_price is None
    assert cycle.last_price is None

    cycle, actions = StrategyEngine.on_price_update(cycle, 100.0)
    assert cycle.anchor_price == 100.0
    assert cycle.last_price == 100.0
    assert round(cycle.drop_trigger_price, 2) == 95.0
    assert actions == []


def test_minimum_profit_field_allows_low_positive_values_because_trigger_floats_upward():
    low = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5,
        buy_rebound_trail_pct=2,
        rise_trigger_pct=1.0,  # UI label: Minimum profit %
        sell_trailing_stop_pct=5.0,
        slippage_buffer_enabled=False,
        protective_sell_enabled=False,
    )
    assert low.validate() == []


def test_minimum_profit_10_percent_sets_initial_sell_stop_to_10_percent_profit():
    cfg = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5,
        buy_rebound_trail_pct=2,
        rise_trigger_pct=10.0,  # UI label: Minimum profit %
        sell_trailing_stop_pct=1.0,
        slippage_buffer_enabled=False,
        protective_sell_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(cfg, 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled")

    # Minimum initial sell stop is avg buy 95 * 1.10 = 104.50.
    # With a 1% trail, the app waits for last price >= 104.50 / 0.99.
    assert round(cycle.rise_trigger_price, 2) == 105.56

    early, actions = StrategyEngine.on_price_update(cycle, 105.00)
    assert early.stage == Stage.WAIT_RISE_TRIGGER
    assert actions == []

    triggered, actions = StrategyEngine.on_price_update(cycle, 105.56)
    assert triggered.stage == Stage.SELL_TRAIL_ACTIVE
    assert len(actions) == 1
    assert actions[0].action_type == "PLACE_SELL_TRAIL"
    assert actions[0].payload["initial_stop_price"] >= 104.50


def test_minimum_profit_uses_average_buy_when_buy_is_above_anchor():
    cfg = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=1,
        buy_rebound_trail_pct=1,
        rise_trigger_pct=5.0,
        sell_trailing_stop_pct=2.0,
        slippage_buffer_enabled=False,
        protective_sell_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(cfg, 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=105.0, status="Filled")

    # Protected reference is avg buy 105 because it is above the anchor.
    # Min stop = 110.25; trigger = 110.25 / 0.98.
    assert round(cycle.rise_trigger_price, 2) == 112.50


def test_projected_minimum_profit_levels_use_projected_buy_reference():
    levels = projected_minimum_profit_levels(5, 2, 10, 1, anchor=100.0)
    assert round(levels["projected_buy_stop"], 2) == 96.90
    assert round(levels["protected_reference"], 2) == 96.90
    assert round(levels["minimum_sell_stop"], 2) == 106.59
    assert round(levels["required_last_price"], 2) == 107.67
    assert round(levels["profit_vs_projected_buy_pct"], 2) == 10.00
    assert round(levels["profit_vs_anchor_pct"], 2) == 6.59


def test_connection_settings_accepts_auto_market_data_mode():
    from app.models import ConnectionSettings

    cfg = ConnectionSettings(market_data_type=0)
    assert cfg.validate() == []


def test_buy_quantity_sizes_from_projected_buy_stop_not_drop_price():
    cfg = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=1.0,
        buy_rebound_trail_pct=10.0,
        rise_trigger_pct=3.0,
        sell_trailing_stop_pct=1.0,
        slippage_buffer_enabled=False,
        protective_sell_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(cfg, 1, "", 100.0, 0.0)
    cycle, actions = StrategyEngine.on_price_update(cycle, 99.0)

    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert round(cycle.buy_initial_trail_stop_price, 2) == 108.90
    assert cycle.quantity == 9
    assert actions[0].payload["quantity"] == 9
    assert actions[0].payload["sizing_price"] == cycle.buy_initial_trail_stop_price


def test_editable_settings_update_wait_initial_drop_cycle():
    base = StrategySettings(ticker="AAPL", investment_amount=1000, initial_drop_pct=5, buy_rebound_trail_pct=2, protective_sell_enabled=False, slippage_buffer_enabled=False, hard_risk_limits_enabled=False, block_delayed_data_in_live=False)
    cycle = StrategyEngine.start_cycle(base, 1, "", 100.0, 0.0)
    edited = StrategySettings(
        ticker="AAPL",
        investment_amount=2000,
        initial_drop_pct=10,
        buy_rebound_trail_pct=3,
        rise_trigger_pct=4,
        sell_trailing_stop_pct=2,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=True,
    )

    updated, changed = StrategyEngine.apply_editable_settings(cycle, edited, realized_net_profit=50.0)

    assert "cycle budget" in changed
    assert updated.budget == 2050.0
    assert updated.investment_amount == 2000.0
    assert updated.initial_drop_pct == 10
    assert updated.buy_rebound_trail_pct == 3
    assert updated.rise_trigger_pct == 4
    assert updated.sell_trailing_stop_pct == 2
    assert round(updated.drop_trigger_price, 2) == 90.00


def test_editable_settings_do_not_change_entry_after_buy_order_submitted():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _actions = StrategyEngine.on_price_update(cycle, 94.0)
    edited = StrategySettings(
        ticker="AAPL",
        investment_amount=5000,
        initial_drop_pct=20,
        buy_rebound_trail_pct=20,
        rise_trigger_pct=7,
        sell_trailing_stop_pct=2,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=True,
    )

    updated, changed = StrategyEngine.apply_editable_settings(cycle, edited, realized_net_profit=500.0)

    assert "minimum profit %" in changed
    assert updated.investment_amount == cycle.investment_amount
    assert updated.initial_drop_pct == cycle.initial_drop_pct
    assert updated.buy_rebound_trail_pct == cycle.buy_rebound_trail_pct
    assert updated.rise_trigger_pct == 7
    assert updated.sell_trailing_stop_pct == 2


def test_editable_settings_recalculate_minimum_profit_trigger_while_waiting_to_sell():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled")
    edited = StrategySettings(ticker="AAPL", rise_trigger_pct=10.0, sell_trailing_stop_pct=1.0, protective_sell_enabled=False, slippage_buffer_enabled=False, hard_risk_limits_enabled=False, block_delayed_data_in_live=False)

    updated, changed = StrategyEngine.apply_editable_settings(cycle, edited)

    assert "minimum profit %" in changed
    assert round(updated.rise_trigger_price, 2) == 111.11


def test_strategy_price_prefers_live_quote_over_stale_last():
    from app.ib_adapter import IbAsyncTwsAdapter

    price, source = IbAsyncTwsAdapter._choose_price({
        "last": 100.00,
        "bidAskMidpoint": 101.25,
        "bid": 101.20,
        "ask": 101.30,
        "marketPrice": None,
    })

    assert price == 101.25
    assert source == "bidAskMidpoint"


def test_strategy_price_prefers_market_price_when_available():
    from app.ib_adapter import IbAsyncTwsAdapter

    price, source = IbAsyncTwsAdapter._choose_price({
        "last": 100.00,
        "bidAskMidpoint": 101.25,
        "marketPrice": 101.30,
    })

    assert price == 101.30
    assert source == "marketPrice"


def test_rth_guard_blocks_buy_order_submission_when_closed():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)

    blocked, actions = StrategyEngine.on_price_update(
        cycle,
        94.0,
        is_rth=False,
        rth_message="closed for test",
    )

    assert actions == []
    assert blocked.stage == Stage.WAIT_INITIAL_DROP
    assert blocked.error_message and blocked.error_message.startswith("RTH guard:")
    assert blocked.buy_order_ref is None


def test_rth_guard_blocks_sell_order_submission_when_closed():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled")

    blocked, actions = StrategyEngine.on_price_update(
        cycle,
        99.0,
        is_rth=False,
        rth_message="closed for test",
    )

    assert actions == []
    assert blocked.stage == Stage.WAIT_RISE_TRIGGER
    assert blocked.error_message and blocked.error_message.startswith("RTH guard:")
    assert blocked.sell_order_ref is None


def test_rollback_unsubmitted_buy_order_clears_active_order_fields():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, actions = StrategyEngine.on_price_update(cycle, 94.0)
    assert actions
    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert cycle.buy_order_ref

    rolled = StrategyEngine.rollback_unsubmitted_order(cycle, "BUY", "submit failed")

    assert rolled.stage == Stage.WAIT_INITIAL_DROP
    assert rolled.buy_order_ref is None
    assert rolled.buy_order_id is None
    assert rolled.quantity == 0
    assert rolled.buy_initial_trail_stop_price is None
    assert rolled.buy_status == "SubmitFailed"


def test_rollback_unsubmitted_sell_order_returns_to_waiting_for_profit():
    cycle = StrategyEngine.start_cycle(settings(), 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled")
    cycle, actions = StrategyEngine.on_price_update(cycle, 99.0)
    assert actions
    assert cycle.stage == Stage.SELL_TRAIL_ACTIVE

    rolled = StrategyEngine.rollback_unsubmitted_order(cycle, "SELL", "submit failed")

    assert rolled.stage == Stage.WAIT_RISE_TRIGGER
    assert rolled.sell_order_ref is None
    assert rolled.sell_order_id is None
    assert rolled.sell_initial_trail_stop_price is None
    assert rolled.sell_status == "SubmitFailed"


def test_slippage_buffer_lifts_minimum_profit_sell_trigger():
    cfg = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5,
        buy_rebound_trail_pct=2,
        rise_trigger_pct=10.0,
        sell_trailing_stop_pct=1.0,
        protective_sell_enabled=False,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=0.5,
    )
    cycle = StrategyEngine.start_cycle(cfg, 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled")

    # 10% min profit plus a 0.5% adverse SELL slippage buffer:
    # first stop = 100 * 1.10 / (1 - 0.005); trigger then accounts for the 1% SELL trail.
    expected = (100.0 * 1.10 / 0.995) / 0.99
    assert round(cycle.rise_trigger_price, 2) == round(expected, 2)
