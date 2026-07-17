"""Validated catalog for deterministic CSV trading simulations.

The catalog is test-only. It binds each human-readable CSV price path to the
settings, run options, and exact lifecycle outcome that the strategy is expected
to produce. Both pytest and ``scripts/run_all_simulations.py`` use this module so
the build gate cannot count a CSV as passing merely because it ended in a known
stage.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from app.models import APP_ORDER_PREFIX, Stage, StrategySettings
from tests.simulated_strategy_runner import SimulationResult, load_price_rows_csv, run_one_cycle

DATA_DIR = Path(__file__).resolve().parent / "simulated_data"

PnlExpectation = Literal["positive", "negative", "zero"]
NotionalExpectation = Literal["above_budget", "at_or_below_budget"]


@dataclass(frozen=True, slots=True)
class EventExpectation:
    """Selected exact values expected on one emitted simulator event."""

    kind: str
    price: float | None = None
    payload: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class CsvScenario:
    """One CSV path plus the independent outcome contract it must satisfy."""

    name: str
    csv_name: str
    category: str
    description: str
    expected_stage: Stage
    expected_events: tuple[str, ...]
    expected_pnl: PnlExpectation = "zero"
    settings_overrides: tuple[tuple[str, object], ...] = ()
    run_overrides: tuple[tuple[str, object], ...] = ()
    expected_cycle: tuple[tuple[str, object], ...] = ()
    event_expectations: tuple[EventExpectation, ...] = ()
    error_contains: str | None = None
    buy_notional: NotionalExpectation | None = None
    minimum_profit_protected: bool = False


BASE_SETTINGS: dict[str, Any] = {
    "ticker": "AAPL",
    "investment_amount": 1000.0,
    "initial_drop_pct": 5.0,
    "buy_rebound_trail_pct": 2.0,
    "rise_trigger_pct": 10.0,
    "sell_trailing_stop_pct": 1.0,
    "atr_adaptive_enabled": False,
    "atr_block_new_buy_until_ready": False,
    "protective_sell_enabled": False,
    "slippage_buffer_enabled": False,
    "hard_risk_limits_enabled": False,
    "block_delayed_data_in_live": False,
    "what_if_check_enabled": False,
    "stale_data_guard_enabled": False,
    "volatility_filter_enabled": False,
    "session_timing_guard_enabled": False,
    "reinvest_profits": False,
    "auto_repeat": False,
}

TRAIL_COMPLETE = (
    "buy_trail_submitted",
    "buy_filled",
    "sell_trail_submitted",
    "sell_filled",
)
TRAIL_POSITION_OPEN = ("buy_trail_submitted", "buy_filled")
PROTECTIVE_FILL = (
    "buy_trail_submitted",
    "buy_filled",
    "protective_sell_submitted",
    "protective_sell_filled",
)
PROTECTIVE_REPLACE = (
    "buy_trail_submitted",
    "buy_filled",
    "protective_sell_submitted",
    "protective_sell_cancelled",
    "sell_trail_submitted",
    "sell_filled",
)


def _values(**values: object) -> tuple[tuple[str, object], ...]:
    return tuple(values.items())


def _event(kind: str, price: float | None = None, **payload: object) -> EventExpectation:
    return EventExpectation(kind=kind, price=price, payload=_values(**payload))


CSV_SCENARIOS: tuple[CsvScenario, ...] = (
    # Existing fixture set, now with exact outcome contracts.
    CsvScenario(
        name="anchor_reset_multiple",
        csv_name="anchor_reset_multiple.csv",
        category="anchor-entry",
        description="Multiple higher prices reset the Stage 1 anchor before one BUY trail is submitted.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(rise_trigger_pct=3.0),
        expected_cycle=_values(anchor_price=108.0, quantity=9, buy_filled_qty=9, avg_buy_price=103.02),
        event_expectations=(
            _event("buy_trail_submitted", 102.60, initial_stop_price=104.652, quantity=9),
            _event("buy_filled", 103.02, stop_price=103.02, filled_qty=9),
        ),
    ),
    CsvScenario(
        name="buy_trail_keeps_falling",
        csv_name="buy_trail_keeps_falling.csv",
        category="buy-execution",
        description="A native BUY trail follows successive lows and fills only at its final rebound stop.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(rise_trigger_pct=3.0),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, avg_buy_price=94.86),
        event_expectations=(_event("buy_filled", 94.86, stop_price=94.86),),
    ),
    CsvScenario(
        name="full_profitable_cycle",
        csv_name="full_profitable_cycle.csv",
        category="lifecycle",
        description="A normal trailing BUY, profit activation, trailing SELL, and profitable completion.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(
            anchor_price=101.0,
            quantity=10,
            buy_filled_qty=10,
            sell_filled_qty=10,
            avg_buy_price=94.86,
            avg_sell_price=108.80,
        ),
    ),
    CsvScenario(
        name="long_anchor_reset_then_drop",
        csv_name="long_anchor_reset_then_drop.csv",
        category="anchor-entry",
        description="A longer rising anchor sequence eventually drops, enters, and holds the filled position.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(anchor_price=105.0, quantity=9, buy_filled_qty=9, avg_buy_price=99.96),
    ),
    CsvScenario(
        name="long_flat_runtime",
        csv_name="long_flat_runtime.csv",
        category="endurance",
        description="Hundreds of quiet ticks create no order and leave only the latest upward anchor.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=103.59, quantity=0, buy_filled_qty=0, sell_filled_qty=0),
    ),
    CsvScenario(
        name="no_initial_drop",
        csv_name="no_initial_drop.csv",
        category="anchor-entry",
        description="A monotonically rising path never satisfies the initial drop condition.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=104.0, quantity=0, buy_filled_qty=0),
    ),
    CsvScenario(
        name="no_sell_trigger_holds_position",
        csv_name="no_sell_trigger_holds_position.csv",
        category="sell-execution",
        description="A filled position remains open while every later price stays below the safe SELL trigger.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=0, rise_trigger_price=105.4),
    ),
    CsvScenario(
        name="prolonged_no_order_anchor_reset",
        csv_name="prolonged_no_order_anchor_reset.csv",
        category="endurance",
        description="A prolonged slow rise repeatedly resets the anchor without accumulating orders or fills.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=106.0, quantity=0, buy_filled_qty=0),
    ),
    CsvScenario(
        name="protective_cancel_then_profit_sell",
        csv_name="protective_cancel_then_profit_sell.csv",
        category="protective-exit",
        description="The protective SELL is cancelled before a lower-profit final SELL trail is installed.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_REPLACE,
        expected_pnl="positive",
        settings_overrides=_values(protective_sell_enabled=True, rise_trigger_pct=3.0),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, avg_sell_price=99.0),
    ),
    CsvScenario(
        name="protective_replaced_by_profit_sell",
        csv_name="protective_replaced_by_profit_sell.csv",
        category="protective-exit",
        description="A working protective trail is cancelled and replaced before the final profitable exit.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_REPLACE,
        expected_pnl="positive",
        settings_overrides=_values(protective_sell_enabled=True),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, avg_sell_price=105.8),
    ),
    CsvScenario(
        name="protective_sell_exits_before_profit",
        csv_name="protective_sell_exits_before_profit.csv",
        category="protective-exit",
        description="The protective trail closes the position before the configured minimum-profit trigger.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_FILL,
        expected_pnl="negative",
        settings_overrides=_values(protective_sell_enabled=True),
        expected_cycle=_values(
            quantity=10,
            buy_filled_qty=10,
            protective_sell_filled_qty=10,
            sell_filled_qty=10,
            avg_sell_price=93.0,
        ),
    ),
    CsvScenario(
        name="protective_sell_loss",
        csv_name="protective_sell_loss.csv",
        category="protective-exit",
        description="A falling market exercises the protective-loss completion and P/L path.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_FILL,
        expected_pnl="negative",
        settings_overrides=_values(protective_sell_enabled=True),
        expected_cycle=_values(protective_sell_filled_qty=10, sell_filled_qty=10, avg_sell_price=92.0),
    ),
    CsvScenario(
        name="rth_reopens_after_drop",
        csv_name="rth_reopens_after_drop.csv",
        category="rth",
        description="A qualifying drop is blocked while RTH is closed and submitted only after RTH reopens.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, error_message=None),
        event_expectations=(_event("buy_trail_submitted", 93.0),),
    ),
    CsvScenario(
        name="slippage_buffer_budget",
        csv_name="slippage_buffer_budget.csv",
        category="sizing-slippage",
        description="A five-percent sizing buffer lowers quantity and raises the safe final SELL trigger.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(slippage_buffer_enabled=True, slippage_buffer_pct=5.0),
        expected_cycle=_values(quantity=9, buy_filled_qty=9, sell_filled_qty=0, rise_trigger_price=110.9474),
        event_expectations=(_event("buy_trail_submitted", 95.0, sizing_price=101.745, quantity=9),),
    ),
    CsvScenario(
        name="slippage_sizing_wide_rebound",
        csv_name="slippage_sizing_wide_rebound.csv",
        category="sizing-slippage",
        description="A wide rebound fills above the stop while buffered sizing keeps actual notional within budget.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(initial_drop_pct=1.0, slippage_buffer_enabled=True, slippage_buffer_pct=1.0),
        expected_cycle=_values(quantity=9, buy_filled_qty=9, avg_buy_price=108.35, rise_trigger_price=121.6049),
        event_expectations=(_event("buy_trail_submitted", 99.0, sizing_price=101.9898, quantity=9),),
        buy_notional="at_or_below_budget",
    ),
    # Exact entry and anchor boundaries.
    CsvScenario(
        name="initial_drop_exact_boundary",
        csv_name="initial_drop_exact_boundary.csv",
        category="anchor-entry",
        description="The initial drop comparison is inclusive at the exact configured threshold.",
        expected_stage=Stage.BUY_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted",),
        expected_cycle=_values(anchor_price=100.0, drop_trigger_price=95.0, quantity=10, buy_filled_qty=0),
        event_expectations=(_event("buy_trail_submitted", 95.0, initial_stop_price=96.9, quantity=10),),
    ),
    CsvScenario(
        name="initial_drop_just_above_boundary",
        csv_name="initial_drop_just_above_boundary.csv",
        category="anchor-entry",
        description="A price one ten-thousandth above the drop threshold must not create a BUY order.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=100.0, drop_trigger_price=95.0, quantity=0),
    ),
    CsvScenario(
        name="anchor_reset_exact_boundary",
        csv_name="anchor_reset_exact_boundary.csv",
        category="anchor-entry",
        description="The exact drop is calculated from the latest higher anchor, not the original price.",
        expected_stage=Stage.BUY_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted",),
        expected_cycle=_values(anchor_price=110.0, drop_trigger_price=104.5, quantity=9),
        event_expectations=(_event("buy_trail_submitted", 104.5, quantity=9),),
    ),
    CsvScenario(
        name="anchor_reset_just_above_boundary",
        csv_name="anchor_reset_just_above_boundary.csv",
        category="anchor-entry",
        description="A reset-anchor price just above the inclusive boundary remains order-free.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=110.0, drop_trigger_price=104.5, quantity=0),
    ),
    CsvScenario(
        name="gap_below_drop_full_cycle",
        csv_name="gap_below_drop_full_cycle.csv",
        category="lifecycle",
        description="A gap through the drop threshold still produces one coherent BUY-to-SELL lifecycle.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, avg_buy_price=90.78, avg_sell_price=100.98),
    ),
    CsvScenario(
        name="choppy_pre_entry_single_order",
        csv_name="choppy_pre_entry_single_order.csv",
        category="anchor-entry",
        description="Choppy pre-entry prices reset the anchor yet emit only one BUY submission.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(anchor_price=104.0, quantity=9, buy_filled_qty=9, avg_buy_price=98.94),
        event_expectations=(_event("buy_trail_submitted", 98.8, quantity=9),),
    ),
    # BUY trail trigger and execution behavior.
    CsvScenario(
        name="buy_rebound_exact_boundary",
        csv_name="buy_rebound_exact_boundary.csv",
        category="buy-execution",
        description="The BUY trail fills at the exact ratcheted stop price.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(quantity=10, buy_filled_qty=10, avg_buy_price=94.86),
        event_expectations=(_event("buy_filled", 94.86, stop_price=94.86, filled_qty=10),),
    ),
    CsvScenario(
        name="buy_rebound_just_below_boundary",
        csv_name="buy_rebound_just_below_boundary.csv",
        category="buy-execution",
        description="The BUY trail remains working one ten-thousandth below its stop.",
        expected_stage=Stage.BUY_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted",),
        expected_cycle=_values(quantity=10, buy_filled_qty=0, avg_buy_price=None),
    ),
    CsvScenario(
        name="buy_trail_multiple_lows_exact_fill",
        csv_name="buy_trail_multiple_lows_exact_fill.csv",
        category="buy-execution",
        description="Several lower lows ratchet the BUY stop down without moving it in the adverse direction.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(quantity=10, buy_filled_qty=10, avg_buy_price=91.8, rise_trigger_price=102.0),
        event_expectations=(_event("buy_filled", 91.8, stop_price=91.8),),
    ),
    CsvScenario(
        name="buy_gap_above_stop",
        csv_name="buy_gap_above_stop.csv",
        category="sizing-slippage",
        description="A gap above a native BUY stop records the observed fill and bases profit protection on that fill.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, avg_buy_price=100.0, rise_trigger_price=111.1111, avg_sell_price=111.87),
        event_expectations=(_event("buy_filled", 100.0, stop_price=94.86),),
    ),
    CsvScenario(
        name="buy_trail_long_hold",
        csv_name="buy_trail_long_hold.csv",
        category="endurance",
        description="A long sequence that never reaches the rebound stop leaves one BUY trail active.",
        expected_stage=Stage.BUY_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted",),
        expected_cycle=_values(quantity=10, buy_filled_qty=0, sell_filled_qty=0),
    ),
    CsvScenario(
        name="zero_buy_trail_market_entry",
        csv_name="zero_buy_trail_market_entry.csv",
        category="buy-execution",
        description="A zero-percent BUY trail uses the market-order branch and completes normally.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=("buy_market_filled", "sell_trail_submitted", "sell_filled"),
        expected_pnl="positive",
        settings_overrides=_values(buy_rebound_trail_pct=0.0),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, avg_buy_price=95.0, avg_sell_price=105.93),
        event_expectations=(_event("buy_market_filled", 95.0, order_type="MKT", quantity=10),),
    ),
    CsvScenario(
        name="partial_buy_40_percent_cycle",
        csv_name="partial_buy_40_percent_cycle.csv",
        category="buy-execution",
        description="A partial BUY cancels the remainder and sizes the final SELL to the four filled shares.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=(
            "buy_trail_submitted",
            "buy_filled",
            "cancel_remainder",
            "sell_trail_submitted",
            "sell_filled",
        ),
        expected_pnl="positive",
        run_overrides=_values(partial_buy_ratio=0.4),
        expected_cycle=_values(quantity=10, buy_filled_qty=4, sell_filled_qty=4, avg_buy_price=94.86, avg_sell_price=104.94),
        event_expectations=(
            _event("buy_filled", 94.86, filled_qty=4),
            _event("sell_trail_submitted", 105.4, quantity=4),
            _event("sell_filled", 104.94, filled_qty=4),
        ),
    ),
    CsvScenario(
        name="insufficient_budget_zero_quantity",
        csv_name="insufficient_budget_zero_quantity.csv",
        category="sizing-slippage",
        description="A budget below one projected share enters ERROR without emitting an order.",
        expected_stage=Stage.ERROR,
        expected_events=(),
        settings_overrides=_values(investment_amount=50.0),
        expected_cycle=_values(budget=50.0, quantity=0, buy_filled_qty=0, sell_filled_qty=0),
        error_contains="Calculated quantity is zero",
    ),
    CsvScenario(
        name="one_share_high_price_cycle",
        csv_name="one_share_high_price_cycle.csv",
        category="numeric",
        description="A high-priced instrument correctly sizes and completes a one-share cycle.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=1, buy_filled_qty=1, sell_filled_qty=1, avg_buy_price=765.0, avg_sell_price=851.4),
    ),
    CsvScenario(
        name="low_price_large_quantity_cycle",
        csv_name="low_price_large_quantity_cycle.csv",
        category="numeric",
        description="A low-priced instrument exercises four-decimal triggers and a four-digit whole-share quantity.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=1031, buy_filled_qty=1031, sell_filled_qty=1031, avg_buy_price=0.918, avg_sell_price=1.0395),
    ),
    CsvScenario(
        name="four_decimal_rounding_cycle",
        csv_name="four_decimal_rounding_cycle.csv",
        category="numeric",
        description="Non-round prices verify four-decimal trigger and stop rounding throughout a cycle.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(
            anchor_price=12.3456,
            drop_trigger_price=11.7283,
            quantity=83,
            avg_buy_price=11.3333,
            rise_trigger_price=12.5925,
            avg_sell_price=12.6499,
        ),
    ),
    # Minimum-profit and final SELL boundaries.
    CsvScenario(
        name="rise_trigger_exact_boundary",
        csv_name="rise_trigger_exact_boundary.csv",
        category="sell-execution",
        description="The final SELL trail is submitted at the exact safe minimum-profit activation price.",
        expected_stage=Stage.SELL_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted", "buy_filled", "sell_trail_submitted"),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=0, rise_trigger_price=105.4),
        event_expectations=(_event("sell_trail_submitted", 105.4, initial_stop_price=104.346, quantity=10),),
    ),
    CsvScenario(
        name="rise_trigger_just_below_boundary",
        csv_name="rise_trigger_just_below_boundary.csv",
        category="sell-execution",
        description="No final SELL order is created one ten-thousandth below the safe activation price.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=0, rise_trigger_price=105.4),
    ),
    CsvScenario(
        name="sell_stop_exact_boundary",
        csv_name="sell_stop_exact_boundary.csv",
        category="sell-execution",
        description="The native SELL trail fills at the exact ratcheted stop.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, sell_filled_qty=10, avg_sell_price=104.94),
        event_expectations=(_event("sell_filled", 104.94, stop_price=104.94, filled_qty=10),),
        minimum_profit_protected=True,
    ),
    CsvScenario(
        name="sell_stop_just_above_boundary",
        csv_name="sell_stop_just_above_boundary.csv",
        category="sell-execution",
        description="The native SELL trail remains working one ten-thousandth above the stop.",
        expected_stage=Stage.SELL_TRAIL_ACTIVE,
        expected_events=("buy_trail_submitted", "buy_filled", "sell_trail_submitted"),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=0),
    ),
    CsvScenario(
        name="sell_trail_multiple_highs_exact_fill",
        csv_name="sell_trail_multiple_highs_exact_fill.csv",
        category="sell-execution",
        description="Successive highs ratchet the SELL stop upward before an exact-boundary exit.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, avg_buy_price=94.86, avg_sell_price=108.9),
        event_expectations=(_event("sell_filled", 108.9, stop_price=108.9),),
        minimum_profit_protected=True,
    ),
    CsvScenario(
        name="sell_gap_below_stop",
        csv_name="sell_gap_below_stop.csv",
        category="sizing-slippage",
        description="A gap below the SELL stop fills at the observed lower price while preserving coherent P/L.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, avg_buy_price=94.86, avg_sell_price=100.0),
        event_expectations=(_event("sell_filled", 100.0, stop_price=108.9),),
    ),
    CsvScenario(
        name="zero_sell_trail_market_exit",
        csv_name="zero_sell_trail_market_exit.csv",
        category="sell-execution",
        description="A zero-percent final SELL trail uses a market order at the exact profit threshold.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=("buy_trail_submitted", "buy_filled", "sell_market_filled"),
        expected_pnl="positive",
        settings_overrides=_values(sell_trailing_stop_pct=0.0),
        expected_cycle=_values(quantity=10, avg_buy_price=94.86, rise_trigger_price=104.346, avg_sell_price=104.346),
        event_expectations=(_event("sell_market_filled", 104.346, order_type="MKT", quantity=10),),
        minimum_profit_protected=True,
    ),
    CsvScenario(
        name="zero_both_trails_market_cycle",
        csv_name="zero_both_trails_market_cycle.csv",
        category="lifecycle",
        description="Both zero-trail branches use market orders without duplicating or skipping a leg.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=("buy_market_filled", "sell_market_filled"),
        expected_pnl="positive",
        settings_overrides=_values(buy_rebound_trail_pct=0.0, sell_trailing_stop_pct=0.0),
        expected_cycle=_values(quantity=10, avg_buy_price=95.0, rise_trigger_price=104.5, avg_sell_price=104.5),
        minimum_profit_protected=True,
    ),
    # Protective SELL boundary and partial-fill coverage.
    CsvScenario(
        name="protective_exact_stop",
        csv_name="protective_exact_stop.csv",
        category="protective-exit",
        description="The initial protective SELL stop is inclusive at its exact three-percent boundary.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_FILL,
        expected_pnl="negative",
        settings_overrides=_values(protective_sell_enabled=True),
        expected_cycle=_values(
            quantity=10,
            protective_sell_filled_qty=10,
            sell_filled_qty=10,
            protective_avg_sell_price=92.0142,
            avg_sell_price=92.0142,
        ),
        event_expectations=(_event("protective_sell_filled", 92.0142, stop_price=92.0142),),
    ),
    CsvScenario(
        name="protective_ratchet_gain",
        csv_name="protective_ratchet_gain.csv",
        category="protective-exit",
        description="A protective trail ratchets above the BUY fill and exits with a small gain before final activation.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=PROTECTIVE_FILL,
        expected_pnl="positive",
        settings_overrides=_values(protective_sell_enabled=True),
        expected_cycle=_values(protective_sell_filled_qty=10, sell_filled_qty=10, avg_sell_price=97.0),
        event_expectations=(_event("protective_sell_filled", 97.0, stop_price=97.0),),
    ),
    CsvScenario(
        name="protective_partial_fill_quantity",
        csv_name="protective_partial_fill_quantity.csv",
        category="protective-exit",
        description="After a forty-percent BUY fill, the protective order and exit use exactly four shares.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=(
            "buy_trail_submitted",
            "buy_filled",
            "cancel_remainder",
            "protective_sell_submitted",
            "protective_sell_filled",
        ),
        expected_pnl="negative",
        settings_overrides=_values(protective_sell_enabled=True),
        run_overrides=_values(partial_buy_ratio=0.4),
        expected_cycle=_values(
            quantity=10,
            buy_filled_qty=4,
            protective_sell_filled_qty=4,
            sell_filled_qty=4,
            avg_sell_price=93.0,
        ),
        event_expectations=(
            _event("buy_filled", 94.86, filled_qty=4),
            _event("protective_sell_submitted", 94.86, quantity=4),
            _event("protective_sell_filled", 93.0),
        ),
    ),
    # RTH transition behavior. Existing native trails continue broker-side.
    CsvScenario(
        name="rth_closed_entire_path",
        csv_name="rth_closed_entire_path.csv",
        category="rth",
        description="A qualifying path entirely outside RTH creates no app-side BUY order.",
        expected_stage=Stage.WAIT_INITIAL_DROP,
        expected_events=(),
        expected_cycle=_values(anchor_price=100.0, quantity=0, buy_filled_qty=0, sell_filled_qty=0),
        error_contains="RTH guard: initial drop condition is met",
    ),
    CsvScenario(
        name="rth_guard_disabled_closed_cycle",
        csv_name="rth_guard_disabled_closed_cycle.csv",
        category="rth",
        description="When rth_only is explicitly disabled, closed flags do not block an otherwise complete cycle.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(rth_only=False),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, error_message=None),
    ),
    CsvScenario(
        name="rth_closed_sell_trigger_then_reopen",
        csv_name="rth_closed_sell_trigger_then_reopen.csv",
        category="rth",
        description="A new SELL is blocked while closed, then submitted once RTH reopens without duplicating it.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, error_message=None),
        event_expectations=(_event("sell_trail_submitted", 105.5),),
    ),
    CsvScenario(
        name="rth_closed_existing_sell_trail",
        csv_name="rth_closed_existing_sell_trail.csv",
        category="rth",
        description="An already-working native SELL trail continues and fills after the RTH flag closes.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, avg_sell_price=108.9),
    ),
    # Slippage, budget, and reinvestment behavior.
    CsvScenario(
        name="combined_slippage_positive",
        csv_name="combined_slippage_positive.csv",
        category="sizing-slippage",
        description="One-percent adverse BUY and SELL slippage still produces a correctly calculated positive result.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(quantity=10, avg_buy_price=95.8086, rise_trigger_price=106.454, avg_sell_price=106.8309),
        event_expectations=(
            _event("buy_filled", 95.8086),
            _event("sell_filled", 106.8309),
        ),
    ),
    CsvScenario(
        name="severe_unbuffered_slippage_loss",
        csv_name="severe_unbuffered_slippage_loss.csv",
        category="sizing-slippage",
        description="Severe unbuffered adverse fills can turn a nominal profit path into a small realized loss.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="negative",
        expected_cycle=_values(quantity=10, avg_buy_price=104.346, rise_trigger_price=115.94, avg_sell_price=104.247),
        event_expectations=(
            _event("buy_filled", 104.346),
            _event("sell_filled", 104.247),
        ),
    ),
    CsvScenario(
        name="reinvest_profit_quantity_cycle",
        csv_name="reinvest_profit_quantity_cycle.csv",
        category="sizing-slippage",
        description="Positive realized profit is added to the budget and increases planned quantity from ten to fifteen.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(reinvest_profits=True),
        run_overrides=_values(realized_net_profit=500.0),
        expected_cycle=_values(budget=1500.0, reinvested_profit=500.0, quantity=15, buy_filled_qty=15, sell_filled_qty=15),
    ),
    CsvScenario(
        name="slippage_buffer_delays_sell",
        csv_name="slippage_buffer_delays_sell.csv",
        category="sizing-slippage",
        description="The buffered safe SELL trigger remains unarmed just below its computed boundary.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(slippage_buffer_enabled=True, slippage_buffer_pct=5.0),
        expected_cycle=_values(quantity=9, buy_filled_qty=9, sell_filled_qty=0, rise_trigger_price=110.9474),
    ),
    CsvScenario(
        name="slippage_buffer_protects_profit",
        csv_name="slippage_buffer_protects_profit.csv",
        category="sizing-slippage",
        description="A five-percent buffer preserves the ten-percent target despite a five-percent adverse SELL fill.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(slippage_buffer_enabled=True, slippage_buffer_pct=5.0),
        expected_cycle=_values(quantity=9, buy_filled_qty=9, sell_filled_qty=9, rise_trigger_price=110.9474, avg_sell_price=105.336),
        minimum_profit_protected=True,
    ),
    CsvScenario(
        name="gap_fill_budget_exposure",
        csv_name="gap_fill_budget_exposure.csv",
        category="sizing-slippage",
        description="Without a buffer, a BUY gap can make actual fill notional exceed the configured budget.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        expected_cycle=_values(budget=1000.0, quantity=10, avg_buy_price=110.0, avg_sell_price=122.76),
        buy_notional="above_budget",
    ),
    CsvScenario(
        name="minimum_profit_epsilon_cycle",
        csv_name="minimum_profit_epsilon_cycle.csv",
        category="numeric",
        description="The minimum allowed 0.01 percent profit target completes through the zero-SELL-trail branch.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=("buy_trail_submitted", "buy_filled", "sell_market_filled"),
        expected_pnl="positive",
        settings_overrides=_values(rise_trigger_pct=0.01, sell_trailing_stop_pct=0.0),
        expected_cycle=_values(quantity=10, avg_buy_price=94.86, rise_trigger_price=94.8695, avg_sell_price=94.8695),
        minimum_profit_protected=True,
    ),
    CsvScenario(
        name="reinvest_disabled_ignores_profit",
        csv_name="reinvest_profit_quantity_cycle.csv",
        category="sizing-slippage",
        description="A positive realized result is ignored when reinvest_profits is disabled.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(reinvest_profits=False),
        run_overrides=_values(realized_net_profit=500.0),
        expected_cycle=_values(budget=1000.0, reinvested_profit=0.0, quantity=10, buy_filled_qty=10, sell_filled_qty=10),
    ),
    CsvScenario(
        name="negative_realized_profit_ignored",
        csv_name="reinvest_profit_quantity_cycle.csv",
        category="sizing-slippage",
        description="A negative realized result never reduces the next cycle budget, even with reinvestment enabled.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(reinvest_profits=True),
        run_overrides=_values(realized_net_profit=-500.0),
        expected_cycle=_values(budget=1000.0, reinvested_profit=0.0, quantity=10, buy_filled_qty=10, sell_filled_qty=10),
    ),
    CsvScenario(
        name="gap_fill_with_15pct_sizing_buffer",
        csv_name="gap_fill_budget_exposure.csv",
        category="sizing-slippage",
        description="A fifteen-percent sizing buffer reduces gap exposure to eight shares and stays within budget.",
        expected_stage=Stage.WAIT_RISE_TRIGGER,
        expected_events=TRAIL_POSITION_OPEN,
        settings_overrides=_values(slippage_buffer_enabled=True, slippage_buffer_pct=15.0),
        expected_cycle=_values(budget=1000.0, quantity=8, buy_filled_qty=8, avg_buy_price=110.0, rise_trigger_price=143.7908),
        event_expectations=(_event("buy_trail_submitted", 95.0, sizing_price=111.435, quantity=8),),
        buy_notional="at_or_below_budget",
    ),
    CsvScenario(
        name="slippage_buffer_budget_disabled_control",
        csv_name="slippage_buffer_budget.csv",
        category="sizing-slippage",
        description="The unbuffered control path keeps ten shares and reaches its lower final SELL trigger.",
        expected_stage=Stage.CYCLE_COMPLETE,
        expected_events=TRAIL_COMPLETE,
        expected_pnl="positive",
        settings_overrides=_values(slippage_buffer_enabled=False),
        expected_cycle=_values(quantity=10, buy_filled_qty=10, sell_filled_qty=10, rise_trigger_price=105.4, avg_sell_price=105.8),
    ),
)


def settings_for_scenario(scenario: CsvScenario) -> StrategySettings:
    """Build an isolated settings object for one catalog entry."""
    values = dict(BASE_SETTINGS)
    values.update(dict(scenario.settings_overrides))
    return StrategySettings(**values)


def scenario_path(scenario: CsvScenario, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / scenario.csv_name


def validate_csv_file(path: Path) -> int:
    """Validate CSV structure and return its number of price rows."""
    if not path.is_file():
        raise AssertionError(f"Missing CSV fixture: {path}")

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        if "price" not in headers:
            raise AssertionError(f"{path.name}: required price column is missing")
        rows = list(reader)

    if not rows:
        raise AssertionError(f"{path.name}: scenario contains no price rows")

    allowed_rth = {"", "0", "1", "false", "true", "no", "yes", "closed", "open"}
    for index, row in enumerate(rows, start=2):
        raw_price = str(row.get("price", "")).strip()
        try:
            price = float(raw_price)
        except ValueError as exc:
            raise AssertionError(f"{path.name}:{index}: invalid price {raw_price!r}") from exc
        if not math.isfinite(price) or price <= 0:
            raise AssertionError(f"{path.name}:{index}: price must be finite and greater than zero")

        raw_time = str(row.get("time", "")).strip()
        if raw_time:
            try:
                datetime.strptime(raw_time, "%H:%M:%S")
            except ValueError as exc:
                raise AssertionError(f"{path.name}:{index}: invalid HH:MM:SS time {raw_time!r}") from exc

        raw_rth = str(row.get("rth_open", "")).strip().lower()
        if raw_rth not in allowed_rth:
            raise AssertionError(f"{path.name}:{index}: unsupported rth_open value {raw_rth!r}")

        for column in ("buy_slippage_pct", "sell_slippage_pct"):
            raw_slippage = str(row.get(column, "")).strip()
            if not raw_slippage:
                continue
            try:
                slippage = float(raw_slippage)
            except ValueError as exc:
                raise AssertionError(f"{path.name}:{index}: invalid {column} value {raw_slippage!r}") from exc
            if not math.isfinite(slippage) or not 0.0 <= slippage < 100.0:
                raise AssertionError(f"{path.name}:{index}: {column} must be in [0, 100)")

    return len(rows)


def run_scenario(scenario: CsvScenario, data_dir: Path = DATA_DIR) -> SimulationResult:
    """Load, validate, and execute one catalog scenario."""
    path = scenario_path(scenario, data_dir)
    validate_csv_file(path)
    settings = settings_for_scenario(scenario)
    return run_one_cycle(
        settings,
        load_price_rows_csv(path),
        **dict(scenario.run_overrides),
    )


def _assert_equal(label: str, actual: object, expected: object) -> None:
    if isinstance(expected, float):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            raise AssertionError(f"{label}: expected numeric {expected!r}, got {actual!r}")
        if not math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-4):
            raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
        return
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _event_by_kind(result: SimulationResult, kind: str) -> object:
    matches = [event for event in result.events if event.kind == kind]
    if len(matches) != 1:
        raise AssertionError(f"event {kind!r}: expected exactly one match, got {len(matches)}")
    return matches[0]


def _assert_generic_invariants(scenario: CsvScenario, result: SimulationResult, settings: StrategySettings) -> None:
    cycle = result.cycle
    prefix = f"{scenario.name}: "

    for field_name in ("quantity", "buy_filled_qty", "protective_sell_filled_qty", "sell_filled_qty"):
        value = int(getattr(cycle, field_name))
        if value < 0:
            raise AssertionError(f"{prefix}{field_name} may not be negative")
    if cycle.buy_filled_qty > cycle.quantity:
        raise AssertionError(f"{prefix}BUY filled quantity exceeds planned quantity")
    if cycle.sell_filled_qty > cycle.buy_filled_qty:
        raise AssertionError(f"{prefix}SELL filled quantity exceeds bought quantity")

    event_kinds = result.event_kinds()
    if len(event_kinds) != len(set(event_kinds)):
        raise AssertionError(f"{prefix}duplicate lifecycle events detected: {event_kinds}")

    buy_fill_kinds = {"buy_filled", "buy_market_filled"}
    sell_fill_kinds = {"sell_filled", "sell_market_filled", "protective_sell_filled"}
    buy_fill_positions = [index for index, kind in enumerate(event_kinds) if kind in buy_fill_kinds]
    sell_fill_positions = [index for index, kind in enumerate(event_kinds) if kind in sell_fill_kinds]
    if sell_fill_positions and (not buy_fill_positions or min(sell_fill_positions) <= min(buy_fill_positions)):
        raise AssertionError(f"{prefix}a SELL fill occurred before a BUY fill")

    for event in result.events:
        payload = event.payload
        order_ref = payload.get("order_ref")
        if order_ref is not None and not str(order_ref).startswith(f"{APP_ORDER_PREFIX}|"):
            raise AssertionError(f"{prefix}{event.kind} used a non-app order reference: {order_ref!r}")

        if event.kind in {"buy_trail_submitted", "buy_market_filled"}:
            quantity = int(payload["quantity"])
            sizing_price = float(payload["sizing_price"])
            budget = float(payload["budget"])
            if quantity != cycle.quantity:
                raise AssertionError(f"{prefix}BUY payload quantity does not equal planned cycle quantity")
            if quantity * sizing_price > budget + 1e-6:
                raise AssertionError(f"{prefix}planned BUY notional exceeds budget at its sizing price")
            if (quantity + 1) * sizing_price <= budget + 1e-6:
                raise AssertionError(f"{prefix}BUY quantity was not rounded down to the maximum whole-share amount")

    if cycle.stage == Stage.CYCLE_COMPLETE:
        if cycle.buy_filled_qty <= 0 or cycle.sell_filled_qty <= 0:
            raise AssertionError(f"{prefix}complete cycle lacks positive BUY/SELL fills")
        if cycle.sell_filled_qty != cycle.buy_filled_qty:
            raise AssertionError(f"{prefix}complete cycle did not close the full simulated position")
        if cycle.avg_buy_price is None or cycle.avg_sell_price is None:
            raise AssertionError(f"{prefix}complete cycle lacks average fill prices")
        overlap = min(cycle.buy_filled_qty, cycle.sell_filled_qty)
        expected_gross = (cycle.avg_sell_price - cycle.avg_buy_price) * overlap
        if not math.isclose(cycle.gross_pnl, expected_gross, rel_tol=1e-9, abs_tol=1e-6):
            raise AssertionError(f"{prefix}gross P/L does not match overlapping filled quantity")
        expected_net = expected_gross - cycle.buy_commission - cycle.sell_commission
        if not math.isclose(cycle.net_pnl, expected_net, rel_tol=1e-9, abs_tol=1e-6):
            raise AssertionError(f"{prefix}net P/L does not match fills and commissions")

    if scenario.minimum_profit_protected:
        if cycle.avg_buy_price is None or cycle.avg_sell_price is None:
            raise AssertionError(f"{prefix}minimum-profit assertion requires completed fills")
        target = cycle.avg_buy_price * (1.0 + float(settings.rise_trigger_pct) / 100.0)
        if cycle.avg_sell_price + 1e-4 < target:
            raise AssertionError(
                f"{prefix}SELL fill {cycle.avg_sell_price:.4f} did not protect target {target:.4f}"
            )

    if scenario.buy_notional is not None:
        if cycle.avg_buy_price is None or cycle.buy_filled_qty <= 0:
            raise AssertionError(f"{prefix}BUY-notional assertion requires a positive fill")
        actual_notional = cycle.avg_buy_price * cycle.buy_filled_qty
        if scenario.buy_notional == "above_budget" and not actual_notional > cycle.budget + 1e-6:
            raise AssertionError(f"{prefix}actual BUY notional was expected to exceed budget")
        if scenario.buy_notional == "at_or_below_budget" and actual_notional > cycle.budget + 1e-6:
            raise AssertionError(f"{prefix}actual BUY notional exceeded budget despite the expected buffer")


def assert_scenario(scenario: CsvScenario, result: SimulationResult) -> None:
    """Assert the exact scenario contract plus common trading invariants."""
    prefix = f"{scenario.name}: "
    cycle = result.cycle
    settings = settings_for_scenario(scenario)

    _assert_equal(f"{prefix}final stage", cycle.stage, scenario.expected_stage)
    _assert_equal(f"{prefix}event sequence", tuple(result.event_kinds()), scenario.expected_events)

    for field_name, expected in scenario.expected_cycle:
        _assert_equal(f"{prefix}cycle.{field_name}", getattr(cycle, field_name), expected)

    for expectation in scenario.event_expectations:
        event = _event_by_kind(result, expectation.kind)
        if expectation.price is not None:
            _assert_equal(f"{prefix}{expectation.kind}.price", event.price, expectation.price)
        for field_name, expected in expectation.payload:
            _assert_equal(
                f"{prefix}{expectation.kind}.payload[{field_name!r}]",
                event.payload.get(field_name),
                expected,
            )

    if scenario.error_contains is not None:
        if scenario.error_contains not in str(cycle.error_message or ""):
            raise AssertionError(
                f"{prefix}expected error containing {scenario.error_contains!r}, got {cycle.error_message!r}"
            )

    if scenario.expected_pnl == "positive" and not cycle.net_pnl > 0:
        raise AssertionError(f"{prefix}expected positive net P/L, got {cycle.net_pnl}")
    if scenario.expected_pnl == "negative" and not cycle.net_pnl < 0:
        raise AssertionError(f"{prefix}expected negative net P/L, got {cycle.net_pnl}")
    if scenario.expected_pnl == "zero" and not math.isclose(cycle.net_pnl, 0.0, abs_tol=1e-9):
        raise AssertionError(f"{prefix}expected zero net P/L, got {cycle.net_pnl}")

    _assert_generic_invariants(scenario, result, settings)


def assert_catalog_integrity(data_dir: Path = DATA_DIR) -> None:
    """Require unique names, valid settings, valid CSVs, and full fixture registration."""
    names = [scenario.name for scenario in CSV_SCENARIOS]
    if len(names) != len(set(names)):
        raise AssertionError("CSV scenario names must be unique")

    registered_files = {scenario.csv_name for scenario in CSV_SCENARIOS}
    disk_files = {path.name for path in data_dir.glob("*.csv")}
    if registered_files != disk_files:
        missing = sorted(disk_files - registered_files)
        absent = sorted(registered_files - disk_files)
        raise AssertionError(f"CSV catalog mismatch: unregistered={missing}, missing={absent}")

    for scenario in CSV_SCENARIOS:
        if not scenario.category.strip() or not scenario.description.strip():
            raise AssertionError(f"{scenario.name}: category and description are required")
        errors = settings_for_scenario(scenario).validate()
        if errors:
            raise AssertionError(f"{scenario.name}: invalid strategy settings: {errors}")
        validate_csv_file(scenario_path(scenario, data_dir))


def category_counts() -> dict[str, int]:
    """Return stable counts for the simulation-runner summary."""
    counts: dict[str, int] = {}
    for scenario in CSV_SCENARIOS:
        counts[scenario.category] = counts.get(scenario.category, 0) + 1
    return dict(sorted(counts.items()))
