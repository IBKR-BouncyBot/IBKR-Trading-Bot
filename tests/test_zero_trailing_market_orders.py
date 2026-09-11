from app.models import Stage, StrategySettings, strategy_with_atr_adaptive_percentages
from app.simulation import simulate_price_path
from app.strategy import StrategyEngine


def test_zero_buy_trailing_submits_market_buy_at_initial_drop():
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=0.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle, actions = StrategyEngine.on_price_update(cycle, 95.0)
    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert actions and actions[0].action_type == "PLACE_BUY_MARKET"
    assert actions[0].payload["trailing_percent"] == 0.0
    assert "BUY_MARKET" in actions[0].payload["order_ref"]
    assert cycle.quantity == 10


def test_zero_sell_trailing_submits_market_sell_at_minimum_profit():
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=0.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=0.0,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle, actions = StrategyEngine.on_price_update(cycle, 95.0)
    cycle = StrategyEngine.on_order_submitted(cycle, actions[0].payload["order_ref"], 1, 11, "Submitted")
    cycle, follow_actions = StrategyEngine.on_buy_fill(cycle, cycle.quantity, 95.0, "Filled")
    assert follow_actions == []
    cycle, actions = StrategyEngine.on_price_update(cycle, 96.89)
    assert actions == []
    cycle, actions = StrategyEngine.on_price_update(cycle, 96.90)
    assert cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert actions and actions[0].action_type == "PLACE_SELL_MARKET"
    assert actions[0].payload["trailing_percent"] == 0.0
    assert "SELL_MARKET" in actions[0].payload["order_ref"]


def test_zero_trailing_full_simulation_completes_with_market_orders():
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=0.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=0.0,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    result = simulate_price_path(settings, [100, 95, 96.9])
    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert any(a.action_type == "PLACE_BUY_MARKET" for a in result.actions)
    assert any(a.action_type == "PLACE_SELL_MARKET" for a in result.actions)


def test_strategy_validation_allows_zero_only_for_main_trailing_fields():
    settings = StrategySettings(ticker="AAPL", buy_rebound_trail_pct=0.0, sell_trailing_stop_pct=0.0)
    assert not settings.validate()
    settings.initial_drop_pct = 0.0
    assert any("Initial drop" in err for err in settings.validate())


def test_atr_zero_buy_and_sell_multipliers_write_zero_trailing_fields():
    settings = StrategySettings(
        ticker="AAPL",
        atr_adaptive_enabled=True,
        atr_buy_rebound_multiplier=0.0,
        atr_sell_trail_multiplier=0.0,
    )
    updated, adaptive = strategy_with_atr_adaptive_percentages(settings, 2.0)
    assert adaptive["buy_rebound_trail_pct"] == 0.0
    assert adaptive["sell_trailing_stop_pct"] == 0.0
    assert updated.buy_rebound_trail_pct == 0.0
    assert updated.sell_trailing_stop_pct == 0.0
    assert not updated.validate()
