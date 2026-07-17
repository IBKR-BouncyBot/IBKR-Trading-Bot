"""Differential checks between the two offline strategy simulators.

The production helper and the richer CSV-fixture runner are independent test
harnesses.  For their shared, no-slippage feature subset they must produce the
same fills and terminal trading state.
"""

from __future__ import annotations

import random

import pytest

from app.models import Stage, StrategySettings
from app.simulation import NativeTrailSimulator, simulate_price_path
from tests.simulated_strategy_runner import WorkingTrailOrder, run_one_cycle


def _settings(*, price_scale: float = 1.0, drop: float = 2.0, buy_trail: float = 1.0, rise: float = 3.0, sell_trail: float = 1.0) -> StrategySettings:
    return StrategySettings(
        ticker="DIFF",
        investment_amount=10_000.0 * price_scale,
        initial_drop_pct=drop,
        buy_rebound_trail_pct=buy_trail,
        rise_trigger_pct=rise,
        sell_trailing_stop_pct=sell_trail,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        protective_sell_enabled=False,
        hard_risk_limits_enabled=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        auto_repeat=False,
    )


def _completed_path(
    anchor: float,
    *,
    drop: float,
    buy_trail: float,
    rise: float,
    sell_trail: float,
) -> list[float]:
    """Construct a path with generous margins around every trigger."""
    peak = anchor * 1.02
    buy_submit = peak * (1.0 - (drop + 0.35) / 100.0)
    buy_low = buy_submit * 0.985
    buy_fill = buy_low * (1.0 + (buy_trail + 0.35) / 100.0)
    rise_submit = buy_fill * (1.0 + (rise + 0.45) / 100.0)
    sell_high = rise_submit * 1.0125
    sell_fill = sell_high * (1.0 - (sell_trail + 0.45) / 100.0)
    return [
        anchor,
        peak,
        peak * 0.995,
        buy_submit,
        buy_low,
        buy_fill,
        buy_fill * 1.005,
        rise_submit,
        sell_high,
        sell_high * 0.9975,
        sell_fill,
    ]


@pytest.mark.parametrize("seed", range(24))
def test_independent_simulators_agree_for_generated_complete_cycles(seed: int) -> None:
    rng = random.Random(seed)
    anchor = rng.uniform(20.0, 500.0)
    drop = rng.uniform(0.7, 4.0)
    buy_trail = rng.uniform(0.25, 1.5)
    rise = rng.uniform(1.0, 5.0)
    sell_trail = rng.uniform(0.25, 1.5)
    settings = _settings(
        price_scale=anchor / 100.0,
        drop=drop,
        buy_trail=buy_trail,
        rise=rise,
        sell_trail=sell_trail,
    )
    prices = _completed_path(
        anchor,
        drop=drop,
        buy_trail=buy_trail,
        rise=rise,
        sell_trail=sell_trail,
    )

    production = simulate_price_path(settings, prices, account="SIM", fill_at_stop=False)
    fixture = run_one_cycle(settings, prices, account="SIM")

    assert production.cycle.stage == Stage.CYCLE_COMPLETE
    assert fixture.cycle.stage == Stage.CYCLE_COMPLETE
    assert production.cycle.quantity == fixture.cycle.quantity
    assert production.cycle.buy_filled_qty == fixture.cycle.buy_filled_qty
    assert production.cycle.sell_filled_qty == fixture.cycle.sell_filled_qty
    assert production.cycle.avg_buy_price == pytest.approx(fixture.cycle.avg_buy_price)
    assert production.cycle.avg_sell_price == pytest.approx(fixture.cycle.avg_sell_price)
    rounding_tolerance = max(0.01, production.cycle.quantity * 0.0002)
    assert production.cycle.gross_pnl == pytest.approx(
        fixture.cycle.gross_pnl, abs=rounding_tolerance
    )
    assert production.cycle.net_pnl == pytest.approx(
        fixture.cycle.net_pnl, abs=rounding_tolerance
    )
    assert fixture.event_kinds() == [
        "buy_trail_submitted",
        "buy_filled",
        "sell_trail_submitted",
        "sell_filled",
    ]


@pytest.mark.parametrize(
    ("prices", "expected_stage"),
    [
        ([100.0, 101.0, 100.5], Stage.WAIT_INITIAL_DROP),
        ([100.0, 97.5, 97.0], Stage.BUY_TRAIL_ACTIVE),
        ([100.0, 97.5, 97.0, 98.2, 99.0], Stage.WAIT_RISE_TRIGGER),
    ],
)
def test_independent_simulators_agree_for_incomplete_paths(prices: list[float], expected_stage: Stage) -> None:
    settings = _settings()
    production = simulate_price_path(settings, prices, account="SIM", fill_at_stop=False)
    fixture = run_one_cycle(settings, prices, account="SIM")

    assert production.cycle.stage == expected_stage
    assert fixture.cycle.stage == expected_stage
    assert production.cycle.quantity == fixture.cycle.quantity
    assert production.cycle.buy_filled_qty == fixture.cycle.buy_filled_qty
    assert production.cycle.sell_filled_qty == fixture.cycle.sell_filled_qty


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("seed", range(10))
def test_native_trail_implementations_trigger_on_the_same_tick(side: str, seed: int) -> None:
    rng = random.Random((seed + 1) * (1 if side == "BUY" else 100))
    trail_pct = rng.uniform(0.2, 3.0)
    initial_price = rng.uniform(10.0, 500.0)
    if side == "BUY":
        initial_stop = initial_price * (1.0 + trail_pct / 100.0)
        prices = [initial_price, initial_price * 0.99, initial_price * 0.98]
        prices.extend(initial_price * factor for factor in (0.985, 0.99, 1.0, 1.02))
    else:
        initial_stop = initial_price * (1.0 - trail_pct / 100.0)
        prices = [initial_price, initial_price * 1.01, initial_price * 1.02]
        prices.extend(initial_price * factor for factor in (1.015, 1.01, 1.0, 0.98))

    production = NativeTrailSimulator(side, initial_stop, trail_pct)
    fixture = WorkingTrailOrder(
        side=side,
        quantity=1,
        trailing_percent=trail_pct,
        order_ref="IBKRBOT|DIFF|TEST",
        stop_price=initial_stop,
        extreme_price=initial_price,
    )

    production_results = [production.update(price) for price in prices]
    fixture_results = [fixture.update(price) for price in prices]
    assert production_results == fixture_results
