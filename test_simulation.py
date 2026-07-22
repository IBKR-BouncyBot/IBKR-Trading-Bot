"""Deterministic simulations that exercise the strategy without IBKR/TWS.

These tests intentionally use fixed price paths instead of mock broker callbacks.
They are fast, reproducible, and validate the pieces that must remain correct
before a native order is handed to TWS or after a fill is reported back.
"""

import csv
from pathlib import Path

import pytest

from app.models import Stage, StrategySettings
from app.simulation import NativeTrailSimulator, simulate_price_path

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _prices_from_csv(name: str) -> list[float]:
    with (FIXTURE_DIR / name).open(newline="", encoding="utf-8") as f:
        return [float(row["price"]) for row in csv.DictReader(f)]


def _rth_mask_from_csv(name: str) -> list[bool]:
    with (FIXTURE_DIR / name).open(newline="", encoding="utf-8") as f:
        return [row["rth_open"].strip() == "1" for row in csv.DictReader(f)]


def simulation_settings(**overrides) -> StrategySettings:
    data = dict(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        rise_trigger_pct=3.0,
        sell_trailing_stop_pct=1.0,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        reinvest_profits=False,
        auto_repeat=False,
    )
    data.update(overrides)
    return StrategySettings(**data)


def test_native_buy_trail_follows_lower_lows_before_triggering():
    trail = NativeTrailSimulator("BUY", initial_stop=102.0, trail_pct=2.0)

    assert not trail.update(100.0)
    assert round(trail.stop_price, 2) == 102.00
    assert not trail.update(95.0)
    assert round(trail.stop_price, 2) == 96.90
    assert not trail.update(94.0)
    assert round(trail.stop_price, 2) == 95.88
    assert trail.update(95.88)


def test_native_sell_trail_follows_higher_highs_before_triggering():
    trail = NativeTrailSimulator("SELL", initial_stop=99.0, trail_pct=1.0)

    assert not trail.update(100.0)
    assert round(trail.stop_price, 2) == 99.00
    assert not trail.update(104.0)
    assert round(trail.stop_price, 2) == 102.96
    assert not trail.update(103.5)
    assert trail.update(102.96)


def test_full_cycle_simulation_from_fixture_completes_with_positive_pnl():
    prices = _prices_from_csv("full_cycle_prices.csv")
    result = simulate_price_path(simulation_settings(), prices)

    assert result.completed
    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert result.cycle.buy_filled_qty == result.cycle.quantity
    assert result.cycle.sell_filled_qty == result.cycle.buy_filled_qty
    assert result.cycle.net_pnl > 0
    assert result.cycle.anchor_price == 102.0
    assert round(result.cycle.avg_buy_price or 0.0, 2) == 93.84
    assert round(result.cycle.avg_sell_price or 0.0, 2) == 102.96
    assert any("submitted BUY" in event.message for event in result.events)
    assert any("submitted SELL" in event.message for event in result.events)


def test_rth_closed_fixture_blocks_buy_until_open():
    prices = _prices_from_csv("rth_closed_prices.csv")
    rth_mask = _rth_mask_from_csv("rth_closed_prices.csv")
    result = simulate_price_path(simulation_settings(), prices, rth_mask=rth_mask)

    # The first off-hours drop sets the RTH guard message and does not create an order.
    blocked_events = [event for event in result.events if event.index in {1, 2}]
    assert all(event.stage == Stage.WAIT_INITIAL_DROP.value for event in blocked_events)
    assert result.cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert result.cycle.buy_order_ref is not None


def test_simulation_rejects_empty_and_non_positive_price_paths():
    with pytest.raises(ValueError):
        simulate_price_path(simulation_settings(), [])
    with pytest.raises(ValueError):
        simulate_price_path(simulation_settings(), [100.0, 0.0])


def test_simulated_quantity_uses_projected_buy_stop_for_wide_rebound():
    settings = simulation_settings(initial_drop_pct=1.0, buy_rebound_trail_pct=10.0, investment_amount=1000.0)
    result = simulate_price_path(settings, [100.0, 99.0])

    assert result.cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert round(result.cycle.buy_initial_trail_stop_price or 0.0, 2) == 108.90
    assert result.cycle.quantity == 9
    assert result.actions[0].payload["sizing_price"] == result.cycle.buy_initial_trail_stop_price


def test_simulation_can_use_input_price_as_fill_to_model_slippage():
    prices = _prices_from_csv("full_cycle_prices.csv")
    result = simulate_price_path(simulation_settings(), prices, fill_at_stop=False)

    assert result.completed
    assert round(result.cycle.avg_buy_price or 0.0, 2) == 94.00
    assert round(result.cycle.avg_sell_price or 0.0, 2) == 102.50
