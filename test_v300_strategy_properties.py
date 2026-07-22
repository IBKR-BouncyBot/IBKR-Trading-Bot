from __future__ import annotations

import random
from copy import copy

from app.models import Stage, StrategySettings, minimum_sell_stop_price_for_profit
from app.strategy import StrategyEngine


def _settings(rng: random.Random) -> StrategySettings:
    return StrategySettings(
        ticker="AAPL",
        investment_amount=rng.uniform(500.0, 50000.0),
        initial_drop_pct=rng.uniform(0.25, 15.0),
        buy_rebound_trail_pct=rng.choice([0.0, rng.uniform(0.05, 5.0)]),
        rise_trigger_pct=rng.uniform(0.05, 15.0),
        sell_trailing_stop_pct=rng.choice([0.0, rng.uniform(0.05, 5.0)]),
        protective_sell_enabled=False,
        slippage_buffer_enabled=rng.choice([False, True]),
        slippage_buffer_pct=rng.uniform(0.0, 2.0),
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        reinvest_profits=False,
    )


def test_price_update_properties_do_not_mutate_input_and_keep_stage_invariants():
    rng = random.Random(3001)
    for case in range(200):
        settings = _settings(rng)
        anchor = rng.uniform(5.0, 600.0)
        cycle = StrategyEngine.start_cycle(settings, case + 1, "SIM", anchor, 0.0)
        original = cycle.to_dict()

        if rng.random() < 0.5:
            price = anchor * rng.uniform(1.0001, 1.50)
            updated, actions = StrategyEngine.on_price_update(cycle, price)
            assert cycle.to_dict() == original
            assert actions == []
            assert updated.stage == Stage.WAIT_INITIAL_DROP
            assert updated.anchor_price == price
            assert round(updated.drop_trigger_price, 4) == round(price * (1.0 - settings.initial_drop_pct / 100.0), 4)
        else:
            price = anchor * (1.0 - settings.initial_drop_pct / 100.0) * rng.uniform(0.80, 1.0)
            updated, actions = StrategyEngine.on_price_update(cycle, price)
            assert cycle.to_dict() == original
            if updated.quantity <= 0:
                assert updated.stage == Stage.ERROR
                assert actions == []
            else:
                assert updated.stage == Stage.BUY_TRAIL_ACTIVE
                assert len(actions) == 1
                expected_type = "PLACE_BUY_MARKET" if settings.buy_rebound_trail_pct <= 0 else "PLACE_BUY_TRAIL"
                assert actions[0].action_type == expected_type
                assert actions[0].payload["quantity"] == updated.quantity
                assert actions[0].payload["order_ref"] == updated.buy_order_ref
                assert updated.quantity * float(actions[0].payload["sizing_price"]) <= updated.budget + float(actions[0].payload["sizing_price"])


def test_buy_fill_and_sell_trigger_properties_preserve_minimum_profit_floor():
    rng = random.Random(3002)
    for case in range(200):
        settings = _settings(rng)
        settings.buy_rebound_trail_pct = rng.uniform(0.1, 4.0)
        settings.sell_trailing_stop_pct = rng.uniform(0.1, 6.0)
        cycle = StrategyEngine.start_cycle(settings, case + 1, "SIM", rng.uniform(20.0, 500.0), 0.0)
        buy_price = rng.uniform(5.0, 600.0)
        filled_qty = rng.randint(1, 500)
        cycle.quantity = filled_qty + rng.randint(0, 20)
        cycle.buy_order_ref = f"IBKRBOT|AAPL|CYCLE-{case:06d}|TEST|BUY_TRAIL"
        before_fill = copy(cycle)

        filled, actions = StrategyEngine.on_buy_fill(cycle, filled_qty, buy_price, "Submitted", commission=rng.uniform(0.0, 10.0))

        assert cycle.to_dict() == before_fill.to_dict()
        assert filled.stage == Stage.WAIT_RISE_TRIGGER
        assert filled.buy_filled_qty == filled_qty
        assert filled.avg_buy_price == buy_price
        minimum_stop = minimum_sell_stop_price_for_profit(
            buy_price,
            filled.anchor_price,
            filled.rise_trigger_pct,
            slippage_buffer_enabled=filled.slippage_buffer_enabled,
            slippage_buffer_pct=filled.slippage_buffer_pct,
        )
        assert filled.rise_trigger_price >= minimum_stop
        if cycle.quantity > filled_qty:
            assert any(action.action_type == "CANCEL_ORDER" for action in actions)

        triggered, sell_actions = StrategyEngine.on_price_update(filled, filled.rise_trigger_price * 1.0001)
        assert triggered.stage == Stage.SELL_TRAIL_ACTIVE
        assert len(sell_actions) == 1
        payload = sell_actions[0].payload
        assert payload["quantity"] == filled_qty
        if payload["order_type"] == "TRAIL":
            assert float(payload["initial_stop_price"]) + 1e-8 >= minimum_stop


def test_terminal_fill_properties_never_report_more_closed_quantity_than_overlap():
    rng = random.Random(3003)
    for case in range(200):
        settings = _settings(rng)
        cycle = StrategyEngine.start_cycle(settings, case + 1, "SIM", 100.0, 0.0)
        cycle.stage = Stage.SELL_TRAIL_ACTIVE
        cycle.buy_filled_qty = rng.randint(1, 500)
        cycle.avg_buy_price = rng.uniform(5.0, 600.0)
        cycle.buy_commission = rng.uniform(0.0, 10.0)
        sell_qty = rng.randint(1, 700)
        sell_price = rng.uniform(5.0, 700.0)
        sell_commission = rng.uniform(0.0, 10.0)

        complete = StrategyEngine.on_sell_fill(cycle, sell_qty, sell_price, "Filled", sell_commission)

        overlap = min(cycle.buy_filled_qty, sell_qty)
        assert complete.stage == Stage.CYCLE_COMPLETE
        assert complete.sell_filled_qty == sell_qty
        assert complete.gross_pnl == (sell_price - cycle.avg_buy_price) * overlap
        assert complete.net_pnl == complete.gross_pnl - cycle.buy_commission - sell_commission
