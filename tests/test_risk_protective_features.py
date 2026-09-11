from app.models import Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine


def _base_settings(**overrides):
    values = dict(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        rise_trigger_pct=3.0,  # UI label: Minimum profit %
        sell_trailing_stop_pct=1.0,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=False,
    )
    values.update(overrides)
    return StrategySettings(**values)


def test_slippage_buffer_sizes_buy_quantity_more_conservatively():
    cfg = _base_settings(
        initial_drop_pct=1.0,
        buy_rebound_trail_pct=10.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=10.0,
    )
    cycle = StrategyEngine.start_cycle(cfg, 1, "SIM", 100.0, 0.0)

    cycle, actions = StrategyEngine.on_price_update(cycle, 99.0)

    assert cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert round(cycle.buy_initial_trail_stop_price, 2) == 108.90
    assert round(actions[0].payload["sizing_price"], 2) == 119.79
    assert cycle.quantity == 8
    assert actions[0].payload["quantity"] == 8


def test_protective_sell_order_is_requested_after_buy_fill():
    cfg = _base_settings(protective_sell_enabled=True, protective_sell_trailing_stop_pct=4.0)
    cycle = StrategyEngine.start_cycle(cfg, 1, "SIM", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 95.0)

    cycle, actions = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled")

    assert cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert cycle.protective_sell_order_ref is not None
    assert round(cycle.protective_sell_initial_stop_price, 2) == 96.00
    assert [action.action_type for action in actions] == ["PLACE_PROTECTIVE_SELL_TRAIL"]
    assert actions[0].payload["quantity"] == 10
    assert actions[0].payload["trailing_percent"] == 4.0


def test_minimum_profit_trigger_cancels_working_protective_sell_before_final_sell():
    cfg = _base_settings(protective_sell_enabled=True, protective_sell_trailing_stop_pct=4.0, rise_trigger_pct=3.0, sell_trailing_stop_pct=1.0)
    cycle = StrategyEngine.start_cycle(cfg, 1, "SIM", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 95.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled")
    cycle = StrategyEngine.on_order_submitted(cycle, cycle.protective_sell_order_ref or "", 201, 2201, "Submitted")

    cycle, actions = StrategyEngine.on_price_update(cycle, cycle.rise_trigger_price or 104.1)

    assert cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert cycle.protective_sell_cancel_requested is True
    assert [action.action_type for action in actions] == ["CANCEL_ORDER"]
    assert actions[0].payload["role"] == "protective_sell"


def test_final_sell_places_after_protective_sell_is_cancelled():
    cfg = _base_settings(protective_sell_enabled=True, protective_sell_trailing_stop_pct=4.0, rise_trigger_pct=3.0, sell_trailing_stop_pct=1.0)
    cycle = StrategyEngine.start_cycle(cfg, 1, "SIM", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 95.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled")
    cycle = StrategyEngine.on_order_submitted(cycle, cycle.protective_sell_order_ref or "", 201, 2201, "Cancelled")

    cycle, actions = StrategyEngine.on_price_update(cycle, cycle.rise_trigger_price or 104.1)

    assert cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert [action.action_type for action in actions] == ["PLACE_SELL_TRAIL"]
    assert actions[0].payload["quantity"] == 10
    assert round(actions[0].payload["initial_stop_price"], 2) >= 103.00


def test_protective_sell_fill_completes_cycle_and_records_loss():
    cfg = _base_settings(protective_sell_enabled=True, protective_sell_trailing_stop_pct=4.0)
    cycle = StrategyEngine.start_cycle(cfg, 1, "SIM", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 95.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=100.0, status="Filled", commission=1.0)
    cycle = StrategyEngine.on_order_submitted(cycle, cycle.protective_sell_order_ref or "", 201, 2201, "Submitted")

    cycle = StrategyEngine.on_protective_sell_fill(cycle, filled_qty=10, avg_fill_price=96.0, status="Filled", commission=1.5)

    assert cycle.stage == Stage.CYCLE_COMPLETE
    assert cycle.avg_sell_price == 96.0
    assert cycle.sell_filled_qty == 10
    assert cycle.gross_pnl == -40.0
    assert cycle.net_pnl == -42.5


def test_storage_daily_risk_metrics_and_consecutive_losses(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    day = utc_now_iso()[:10]

    for cycle_number, pnl in [(1, -30.0), (2, -20.0)]:
        cfg = _base_settings()
        cycle = StrategyEngine.start_cycle(cfg, cycle_number, "SIM", 100.0, 0.0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.net_pnl = pnl
        cycle.updated_at = f"{day}T10:0{cycle_number}:00+00:00"
        storage.upsert_cycle(cycle)

    winning = StrategyEngine.start_cycle(_base_settings(ticker="MSFT"), 3, "SIM", 100.0, 0.0)
    winning.stage = Stage.CYCLE_COMPLETE
    winning.net_pnl = 15.0
    winning.updated_at = f"{day}T11:00:00+00:00"
    storage.upsert_cycle(winning)

    assert storage.get_daily_net_pnl_for_ticker("AAPL", day) == -50.0
    assert storage.get_daily_net_pnl_total(day) == -35.0
    assert storage.get_completed_cycle_count_today("AAPL", day) == 2
    assert storage.get_consecutive_loss_count("AAPL") == 2


def test_editable_settings_can_enable_risk_controls_before_drop():
    cycle = StrategyEngine.start_cycle(_base_settings(), 1, "SIM", 100.0, 0.0)
    edited = _base_settings(
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=6.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=1.5,
        hard_risk_limits_enabled=True,
        max_daily_loss_ticker=100.0,
        max_spread_pct=0.25,
        block_delayed_data_in_live=True,
    )

    updated, changed = StrategyEngine.apply_editable_settings(cycle, edited)

    assert updated.protective_sell_enabled is True
    assert updated.protective_sell_trailing_stop_pct == 6.0
    assert updated.slippage_buffer_enabled is True
    assert updated.slippage_buffer_pct == 1.5
    assert updated.hard_risk_limits_enabled is True
    assert updated.max_daily_loss_ticker == 100.0
    assert updated.max_spread_pct == 0.25
    assert updated.block_delayed_data_in_live is True
    assert "protective sell enabled" in changed
    assert "slippage buffer enabled" in changed
    assert "hard risk limits enabled" in changed
