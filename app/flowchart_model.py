"""Build a parameterized, code-aligned strategy flowchart model.

The GUI flowchart tab renders these cards. Keeping card construction outside Qt
makes the diagram testable and keeps displayed numbers aligned with the same
helpers used by the strategy engine. The model intentionally shows the five
business stages even when optional risk controls add additional order activity
inside a stage, such as the optional protective SELL trail in Stage 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Any, Optional

from .models import Stage, StrategySettings, minimum_sell_stop_price_for_profit, projected_minimum_profit_levels


@dataclass(frozen=True, slots=True)
class FlowchartStageCard:
    """One visible section of the five-stage strategy flowchart."""

    stage: Stage
    title: str
    order_summary: str
    trigger_summary: str
    details: tuple[str, ...]
    active: bool = False


def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except Exception:
        return None
    if not isfinite(result):
        return None
    return result


def _money(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"${number:,.4f}"


def _money2(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"${number:,.2f}"


def _pct(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "-"
    return f"{number:.2f}%"


def _stage_from_cycle(cycle: dict[str, Any] | None) -> Optional[Stage]:
    if not cycle:
        return None
    try:
        return Stage(str(cycle.get("stage") or ""))
    except Exception:
        return None


def _reference_price(strategy: StrategySettings, cycle: dict[str, Any] | None, price_snapshot: dict[str, Any] | None) -> tuple[float, str]:
    cycle = cycle or {}
    price_snapshot = price_snapshot or {}
    for key, label in [
        ("anchor_price", "cycle anchor"),
        ("last_price", "cycle last price"),
    ]:
        value = _num(cycle.get(key))
        if value and value > 0:
            return value, label
    value = _num(price_snapshot.get("price"))
    if value and value > 0:
        return value, "current API price"
    return 100.0, "normalized baseline"


def _sizing_price(projected_buy_stop: float, strategy: StrategySettings, cycle: dict[str, Any]) -> float:
    cycle_enabled = bool(cycle.get("slippage_buffer_enabled", getattr(strategy, "slippage_buffer_enabled", False)))
    buffer_pct = _num(cycle.get("slippage_buffer_pct"))
    if buffer_pct is None:
        buffer_pct = float(getattr(strategy, "slippage_buffer_pct", 0.0) or 0.0)
    if cycle_enabled:
        return projected_buy_stop * (1.0 + max(0.0, buffer_pct) / 100.0)
    return projected_buy_stop


def _bool_text(value: bool) -> str:
    return "ON" if value else "OFF"


def build_strategy_flowchart_cards(
    strategy: StrategySettings,
    cycle: dict[str, Any] | None = None,
    price_snapshot: dict[str, Any] | None = None,
) -> list[FlowchartStageCard]:
    """Return the five flowchart stage cards for the current parameters.

    The calculations mirror the implemented strategy:
    * no broker order while waiting for the initial drop;
    * native BUY TRAIL order after the drop;
    * optional protective SELL TRAIL immediately after a BUY fill;
    * native profit-protecting SELL TRAIL after the minimum-profit condition;
    * cycle ledger/update and optional auto-repeat after position exit.
    """
    cycle = cycle or {}
    price_snapshot = price_snapshot or {}
    active_stage = _stage_from_cycle(cycle)
    reference, reference_label = _reference_price(strategy, cycle, price_snapshot)
    slippage_enabled = bool(cycle.get("slippage_buffer_enabled", getattr(strategy, "slippage_buffer_enabled", False)))
    slippage_pct = _num(cycle.get("slippage_buffer_pct"))
    if slippage_pct is None:
        slippage_pct = float(getattr(strategy, "slippage_buffer_pct", 0.0) or 0.0)
    levels = projected_minimum_profit_levels(
        strategy.initial_drop_pct,
        strategy.buy_rebound_trail_pct,
        strategy.rise_trigger_pct,
        strategy.sell_trailing_stop_pct,
        anchor=reference,
        slippage_buffer_enabled=slippage_enabled,
        slippage_buffer_pct=slippage_pct,
    )

    anchor = _num(cycle.get("anchor_price")) or levels["anchor"]
    drop_trigger = _num(cycle.get("drop_trigger_price")) or levels["drop_trigger"]
    buy_stop = _num(cycle.get("buy_initial_trail_stop_price")) or levels["projected_buy_stop"]
    avg_buy = _num(cycle.get("avg_buy_price")) or buy_stop
    min_sell_stop = minimum_sell_stop_price_for_profit(
        avg_buy,
        anchor,
        strategy.rise_trigger_pct,
        slippage_buffer_enabled=slippage_enabled,
        slippage_buffer_pct=slippage_pct,
    )
    required_last = min_sell_stop / max(1e-12, 1.0 - strategy.sell_trailing_stop_pct / 100.0)

    stored_sell_stop = _num(cycle.get("sell_initial_trail_stop_price"))
    stored_trigger = _num(cycle.get("rise_trigger_price"))
    protective_stop = _num(cycle.get("protective_sell_initial_stop_price"))
    if stored_sell_stop:
        min_sell_stop = stored_sell_stop
    if stored_trigger:
        required_last = stored_trigger

    budget = _num(cycle.get("budget")) or strategy.investment_amount
    sizing = _sizing_price(buy_stop, strategy, cycle)
    projected_qty = floor(budget / sizing) if budget and sizing > 0 else 0
    ticker = str(cycle.get("ticker") or strategy.normalized_ticker() or "TICKER")
    order_ref = str(cycle.get("buy_order_ref") or cycle.get("protective_sell_order_ref") or cycle.get("sell_order_ref") or "IBKRBOT|ticker|cycle|...")
    selected_price = price_snapshot.get("price")

    protective_enabled = bool(cycle.get("protective_sell_enabled", getattr(strategy, "protective_sell_enabled", False)))
    protective_trail = _num(cycle.get("protective_sell_trailing_stop_pct"))
    if protective_trail is None:
        protective_trail = float(getattr(strategy, "protective_sell_trailing_stop_pct", 0.0) or 0.0)
    risk_enabled = bool(cycle.get("hard_risk_limits_enabled", getattr(strategy, "hard_risk_limits_enabled", False)))
    rth_text = "RTH guard ON; outsideRth=False" if getattr(strategy, "rth_only", True) else "RTH guard OFF"

    risk_lines = []
    if risk_enabled:
        if getattr(strategy, "max_daily_loss_ticker", 0.0):
            risk_lines.append(f"ticker loss <= ${strategy.max_daily_loss_ticker:,.2f}/day")
        if getattr(strategy, "max_daily_loss_total", 0.0):
            risk_lines.append(f"total loss <= ${strategy.max_daily_loss_total:,.2f}/day")
        if getattr(strategy, "max_cycles_per_ticker_day", 0):
            risk_lines.append(f"max cycles <= {strategy.max_cycles_per_ticker_day} total")
        if getattr(strategy, "max_consecutive_losses", 0):
            risk_lines.append(f"consecutive losses <= {strategy.max_consecutive_losses}")
        if getattr(strategy, "max_spread_pct", 0.0):
            risk_lines.append(f"spread <= {_pct(strategy.max_spread_pct)}")
        if getattr(strategy, "min_trade_price", 0.0):
            risk_lines.append(f"price >= ${strategy.min_trade_price:,.2f}")
        if getattr(strategy, "max_gap_from_prev_close_pct", 0.0):
            risk_lines.append(f"gap <= {_pct(strategy.max_gap_from_prev_close_pct)}")
    if getattr(strategy, "block_delayed_data_in_live", False):
        risk_lines.append("live profile requires live data")
    safety_lines: list[str] = []
    if getattr(strategy, "what_if_check_enabled", False):
        safety_lines.append("IBKR what-if margin check before BUY")
    if getattr(strategy, "stale_data_guard_enabled", False):
        safety_lines.append(
            f"fresh price <= {float(getattr(strategy, 'max_selected_price_age_seconds', 3.0)):.1f}s; "
            f"bid/ask <= {float(getattr(strategy, 'max_bid_ask_age_seconds', 3.0)):.1f}s"
        )
    if getattr(strategy, "volatility_filter_enabled", False):
        safety_lines.append(
            f"recent move <= {_pct(getattr(strategy, 'max_recent_price_move_pct', 5.0))} / "
            f"{int(getattr(strategy, 'volatility_window_seconds', 300))}s"
        )
    if getattr(strategy, "session_timing_guard_enabled", False):
        safety_lines.append(
            f"no BUY first {int(getattr(strategy, 'no_new_buy_first_minutes', 5))}m / "
            f"last {int(getattr(strategy, 'no_new_buy_last_minutes', 15))}m; "
            f"cancel BUY {int(getattr(strategy, 'cancel_buy_before_close_minutes', 5))}m before close"
        )

    risk_summary = "; ".join(risk_lines) if risk_lines else "OFF"
    safety_summary_short = "; ".join(safety_lines[:2]) if safety_lines else "OFF"
    atr_lines: list[str] = []
    if getattr(strategy, "atr_adaptive_enabled", False):
        atr = price_snapshot.get("atr") or {}
        atr_pct = price_snapshot.get("atr_pct") or atr.get("atr_pct")
        atr_lines.append(
            f"ATR adaptive ON: ATR {_pct(atr_pct)}; period {int(getattr(strategy, 'atr_period', 14))} bars x {int(getattr(strategy, 'atr_bar_seconds', 60))}s."
        )
        profit_part = (
            f"profit {float(getattr(strategy, 'atr_minimum_profit_multiplier', 1.0)):.2f}x"
            if bool(getattr(strategy, "atr_adapt_minimum_profit_enabled", True))
            else f"profit manual {_pct(getattr(strategy, 'rise_trigger_pct', 0.0))}"
        )
        protective_part = (
            f"protective {float(getattr(strategy, 'atr_protective_sell_multiplier', 3.0)):.2f}x"
            if bool(getattr(strategy, "atr_adapt_protective_sell_enabled", False))
            else "protective manual"
        )
        warmup_part = "BUY waits for ATR" if bool(getattr(strategy, "atr_block_new_buy_until_ready", True)) else "BUY may use current manual values before ATR ready"
        atr_lines.append(
            f"ATR multipliers: drop {float(getattr(strategy, 'atr_initial_drop_multiplier', 1.5)):.2f}x, "
            f"buy {float(getattr(strategy, 'atr_buy_rebound_multiplier', 0.75)):.2f}x, "
            f"{profit_part}, "
            f"sell {float(getattr(strategy, 'atr_sell_trail_multiplier', 1.0)):.2f}x, "
            f"{protective_part}. {warmup_part}."
        )
    else:
        atr_lines.append("ATR adaptive OFF: manual percentage fields are used.")

    return [
        FlowchartStageCard(
            stage=Stage.WAIT_INITIAL_DROP,
            title="Stage 1 - Watch for initial drop",
            order_summary="IBKR order: none",
            trigger_summary=f"Price <= {_money(drop_trigger)}",
            details=(
                f"Ticker {ticker}; reference {_money(anchor)} ({reference_label}).",
                "Anchor resets upward while price moves above the current anchor.",
                f"Initial drop {_pct(strategy.initial_drop_pct)}; selected API price {_money(selected_price)}.",
                atr_lines[0],
                f"Hard risk {_bool_text(risk_enabled)}; live-data guard {_bool_text(bool(getattr(strategy, 'block_delayed_data_in_live', False)))}.",
                f"Risk details: {risk_summary}.",
                f"Safety checks: {safety_summary_short}.",
            ),
            active=active_stage == Stage.WAIT_INITIAL_DROP,
        ),
        FlowchartStageCard(
            stage=Stage.BUY_TRAIL_ACTIVE,
            title="Stage 2 - BUY order",
            order_summary=("IBKR order: BUY MKT" if float(strategy.buy_rebound_trail_pct or 0.0) <= 0 else "IBKR order: BUY TRAIL"),
            trigger_summary=(f"BUY market at drop reference {_money(buy_stop)}; trailing disabled" if float(strategy.buy_rebound_trail_pct or 0.0) <= 0 else f"Initial BUY stop {_money(buy_stop)}; trail {_pct(strategy.buy_rebound_trail_pct)}"),
            details=(
                "Submitted only after Stage 1 reaches the drop trigger.",
                ("BUY trailing is disabled by a 0.00% setting, so the app submits a market BUY immediately." if float(strategy.buy_rebound_trail_pct or 0.0) <= 0 else "TWS/IB Gateway trails the BUY stop down as price keeps falling."),
                ("The market order is monitored until TWS/IB Gateway reports a fill." if float(strategy.buy_rebound_trail_pct or 0.0) <= 0 else "A rebound to the trailing stop triggers a market BUY."),
                f"Sizing: floor({_money2(budget)} / {_money(sizing)}) = {projected_qty} shares.",
                f"Slippage buffer {_bool_text(slippage_enabled)}" + (f" ({_pct(slippage_pct)})." if slippage_enabled else "."),
                atr_lines[1] if len(atr_lines) > 1 else atr_lines[0],
                f"Pre-BUY checks: {safety_summary_short}.",
                rth_text,
            ),
            active=active_stage == Stage.BUY_TRAIL_ACTIVE,
        ),
        FlowchartStageCard(
            stage=Stage.WAIT_RISE_TRIGGER,
            title="Stage 3 - Position open / wait for minimum profit",
            order_summary="IBKR order: optional protective SELL TRAIL" if protective_enabled else "IBKR order: none",
            trigger_summary=f"Wait until last price >= {_money(required_last)}",
            details=(
                f"Average BUY reference {_money(avg_buy)}.",
                f"Minimum profit {_pct(strategy.rise_trigger_pct)}; slippage buffer {_pct(slippage_pct) if slippage_enabled else 'OFF'}.",
                f"Buffered first SELL stop >= {_money(min_sell_stop)}; required last >= {_money(required_last)}.",
                (f"Protective trail ON: stop about {_money(protective_stop or avg_buy * (1.0 - protective_trail / 100.0))}, trail {_pct(protective_trail)}." if protective_enabled else "Protective trail OFF: position can remain unprotected until Stage 4."),
                f"Safety checks remain active for new BUY entries: {safety_summary_short}.",
            ),
            active=active_stage == Stage.WAIT_RISE_TRIGGER,
        ),
        FlowchartStageCard(
            stage=Stage.SELL_TRAIL_ACTIVE,
            title="Stage 4 - Profit-protecting SELL order",
            order_summary=("IBKR order: SELL MKT" if float(strategy.sell_trailing_stop_pct or 0.0) <= 0 else "IBKR order: SELL TRAIL"),
            trigger_summary=(f"SELL market once minimum stop/reference {_money(min_sell_stop)} is protected" if float(strategy.sell_trailing_stop_pct or 0.0) <= 0 else f"Initial SELL stop {_money(min_sell_stop)}; trail {_pct(strategy.sell_trailing_stop_pct)}"),
            details=(
                "Submitted after Stage 3 required last price is reached.",
                "If protective SELL is working, the app cancels it before submitting the final SELL order.",
                ("SELL trailing is disabled by a 0.00% setting, so the app submits a market SELL immediately." if float(strategy.sell_trailing_stop_pct or 0.0) <= 0 else "TWS/IB Gateway trails the SELL stop upward as price rises."),
                ("The market order is monitored until TWS/IB Gateway reports a fill." if float(strategy.sell_trailing_stop_pct or 0.0) <= 0 else "A fall back to the trailing stop triggers a market SELL."),
                f"Order ownership marker: {order_ref}",
                rth_text,
            ),
            active=active_stage == Stage.SELL_TRAIL_ACTIVE,
        ),
        FlowchartStageCard(
            stage=Stage.CYCLE_COMPLETE,
            title="Stage 5 - Record cycle and repeat",
            order_summary="IBKR order: none after exit fill",
            trigger_summary="Cycle completes when an app-owned SELL/protective SELL fills",
            details=(
                "SQLite records orders, executions, commissions, net/gross P/L, and recovery events.",
                f"Auto-repeat {_bool_text(strategy.auto_repeat)}; ticker-profit reinvest {_bool_text(strategy.reinvest_profits)}.",
                "Next cycle starts from Stage 1 after completion if auto-repeat remains enabled.",
                "On restart the app reconciles SQLite, open app orders, recent executions, and position size.",
            ),
            active=active_stage == Stage.CYCLE_COMPLETE,
        ),
    ]
