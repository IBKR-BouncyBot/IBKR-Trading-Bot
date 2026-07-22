"""Domain models and pure math helpers for the IBKR trading bot.

This module intentionally contains no Qt, no SQLite, and no live IBKR calls.
It defines the serializable dataclasses that are persisted to the portable
SQLite database and moved between the controller, GUI, storage layer, and tests.

Compatibility naming:
    ``rise_trigger_pct`` remains the persisted/internal field name for existing
    SQLite files. In the UI it means ``Minimum profit %``: the gross initial SELL
    stop level required above the executed average BUY fill.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .ib_platform import GATEWAY_PLATFORM, SUPPORTED_PLATFORMS, TWS_PLATFORM

APP_ORDER_PREFIX = "IBKRBOT"

# Minimum-profit guard used for the first SELL trailing-stop.
# Profit is measured against the executed average buy fill. The cycle anchor is
# displayed only as context in the GUI projection.
# This guard is before commissions, fees, gaps, and market-order slippage.
PROFIT_GUARD_EPSILON_PCT = 0.01
PCT_DECIMALS = 2

# All app-generated timestamps use UTC. SQLite cycle rows, audit events,
# broker-recovery snapshots, market-data capture rows, and GUI audit charts
# share this clock so records can be aligned without depending on the Windows
# workstation timezone.
APP_TIMEZONE_LABEL = "UTC"


def ceil_pct(value: float, decimals: int = PCT_DECIMALS) -> float:
    scale = 10 ** decimals
    return math.ceil(float(value) * scale) / scale


def floor_pct(value: float, decimals: int = PCT_DECIMALS) -> float:
    scale = 10 ** decimals
    return math.floor(float(value) * scale) / scale


def min_configured_profit_pct(value: float) -> float:
    """Clamp the user-facing Minimum profit % to a positive value."""
    return max(float(value), PROFIT_GUARD_EPSILON_PCT)


def initial_sell_stop_factor_for_min_profit(minimum_profit_pct: float) -> float:
    """Multiplier for the first SELL stop from the average buy fill."""
    return 1.0 + min_configured_profit_pct(minimum_profit_pct) / 100.0


def initial_sell_stop_price_for_min_profit(avg_buy_price: float, minimum_profit_pct: float) -> float:
    """Initial SELL trailing-stop floor from the actual average buy fill."""
    return float(avg_buy_price) * initial_sell_stop_factor_for_min_profit(minimum_profit_pct)


def slippage_factor(slippage_buffer_enabled: bool = False, slippage_buffer_pct: float = 0.0) -> float:
    """Return a positive planning multiplier for optional slippage buffering.

    The bot still submits native trailing-stop orders that trigger market orders.
    This buffer only changes planning math: BUY quantity is sized more
    conservatively and the minimum-profit SELL activation can require a higher
    last price. It cannot guarantee final execution price.
    """
    if not slippage_buffer_enabled:
        return 1.0
    try:
        pct = max(0.0, float(slippage_buffer_pct))
    except Exception:
        pct = 0.0
    return 1.0 + pct / 100.0


def effective_buy_sizing_price(projected_buy_stop_price: float, slippage_buffer_enabled: bool = False, slippage_buffer_pct: float = 0.0) -> float:
    """Price used to calculate rounded-down BUY quantity."""
    try:
        price = float(projected_buy_stop_price)
    except Exception:
        return 0.0
    if price <= 0 or not math.isfinite(price):
        return 0.0
    return price * slippage_factor(slippage_buffer_enabled, slippage_buffer_pct)


def minimum_sell_stop_price_for_profit(
    avg_buy_price: Any,
    anchor_price: Any,
    minimum_profit_pct: float,
    slippage_buffer_enabled: bool = False,
    slippage_buffer_pct: float = 0.0,
) -> float:
    """Initial SELL stop required by the Minimum profit % field.

    Profit is measured from the actual average buy fill. If optional slippage
    buffering is enabled, the initial stop is raised so that a hypothetical SELL
    fill worse than the stop by the slippage buffer still protects the
    configured minimum profit before commissions.

    ``anchor_price`` remains accepted for call-site and database compatibility.
    """
    try:
        buy = float(avg_buy_price)
    except Exception:
        return 0.0
    if buy <= 0 or not math.isfinite(buy):
        return 0.0
    base_stop = initial_sell_stop_price_for_min_profit(buy, minimum_profit_pct)
    if not slippage_buffer_enabled:
        return base_stop
    try:
        slip = max(0.0, min(99.0, float(slippage_buffer_pct))) / 100.0
    except Exception:
        slip = 0.0
    # If a triggered market SELL fills slip% below the stop, require:
    # stop * (1-slip) >= buy * (1+minimum profit).
    return base_stop / max(1e-12, 1.0 - slip)


def required_market_rise_pct_for_min_profit(minimum_profit_pct: float, sell_trailing_stop_pct: float) -> float:
    """Required market rise from average buy before placing the SELL trail.

    If m = minimum profit and s = sell trail, the app waits until:
        last_price * (1 - s) >= avg_buy_price * (1 + m)

    Therefore the required trigger above average buy is:
        ((1 + m) / (1 - s) - 1)
    """
    s = float(sell_trailing_stop_pct) / 100.0
    if s < 0:
        s = 0.0
    if s >= 1.0:
        return 999999.0
    m = min_configured_profit_pct(minimum_profit_pct) / 100.0
    return max(PROFIT_GUARD_EPSILON_PCT, ceil_pct((((1.0 + m) / (1.0 - s)) - 1.0) * 100.0))


def required_sell_trigger_price_for_min_profit(
    avg_buy_price: float,
    minimum_profit_pct: float,
    sell_trailing_stop_pct: float,
    slippage_buffer_enabled: bool = False,
    slippage_buffer_pct: float = 0.0,
) -> float:
    """Market price needed before the native SELL trailing-stop can be placed."""
    stop_price = minimum_sell_stop_price_for_profit(
        avg_buy_price=avg_buy_price,
        anchor_price=None,
        minimum_profit_pct=minimum_profit_pct,
        slippage_buffer_enabled=slippage_buffer_enabled,
        slippage_buffer_pct=slippage_buffer_pct,
    )
    s = float(sell_trailing_stop_pct) / 100.0
    if s >= 1.0:
        return float("inf")
    if s < 0:
        s = 0.0
    return stop_price / (1.0 - s)


def profit_trigger_price_for_sell_trail(
    avg_buy_price: Any,
    anchor_price: Any,
    minimum_profit_pct: float,
    sell_trailing_stop_pct: float,
    slippage_buffer_enabled: bool = False,
    slippage_buffer_pct: float = 0.0,
) -> float:
    """Last price required before placing the native SELL trailing-stop.

    Example without slippage buffer: minimum profit 10% and SELL trail 1% means
    the first stop must be avg_buy * 1.10, so last price must reach
    avg_buy * 1.10 / 0.99. If slippage buffering is enabled, the first stop is
    raised before this trailing-stop calculation is applied.
    """
    try:
        buy = float(avg_buy_price)
    except Exception:
        return 0.0
    if buy <= 0 or not math.isfinite(buy):
        return 0.0
    return required_sell_trigger_price_for_min_profit(
        buy,
        minimum_profit_pct,
        sell_trailing_stop_pct,
        slippage_buffer_enabled=slippage_buffer_enabled,
        slippage_buffer_pct=slippage_buffer_pct,
    )


def projected_buy_factor_from_anchor(initial_drop_pct: float, buy_rebound_trail_pct: float) -> float:
    """Immediate-path projected buy trigger factor versus cycle anchor."""
    d = float(initial_drop_pct) / 100.0
    b = float(buy_rebound_trail_pct) / 100.0
    return (1.0 - d) * (1.0 + b)






def clamp_pct(value: Any, lower: float = 0.01, upper: float = 99.99) -> float:
    """Return a sane percentage value with two decimals.

    ATR-adaptive mode uses this to turn a volatility estimate into the same
    user-facing percentage fields used by manual mode.  The clamp prevents a
    bad or tiny data sample from writing unusable order percentages.
    """
    try:
        number = float(value)
    except Exception:
        number = lower
    if not math.isfinite(number):
        number = lower
    return round(max(float(lower), min(float(upper), number)), PCT_DECIMALS)


def atr_from_price_history(
    points: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    *,
    period: int = 14,
    bar_seconds: int = 60,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Calculate an app-observed ATR estimate from subscribed API prices.

    The current app does not request separate historical or real-time bar feeds.
    It continuously reads the latest top-of-book/market-price snapshot from the
    active TWS subscription and stores a rolling price buffer.  This helper
    converts that buffer into fixed-time OHLC bars and applies the standard true
    range calculation:

        TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
        ATR = simple average of the latest ``period`` true ranges

    Because the source is sampled subscription data rather than exchange-native
    historical bars, the output is intentionally labelled ``app_observed``.
    """
    try:
        period = max(2, int(period))
    except Exception:
        period = 14
    try:
        bar_seconds = max(5, int(bar_seconds))
    except Exception:
        bar_seconds = 60
    clean: list[tuple[float, float]] = []
    for ts, price in list(points or []):
        try:
            t = float(ts)
            p = float(price)
        except Exception:
            continue
        if math.isfinite(t) and math.isfinite(p) and p > 0:
            clean.append((t, p))
    clean.sort(key=lambda row: row[0])
    if not clean:
        return {
            "ready": False,
            "source": "app_observed_api_prices",
            "reason": "no usable price observations",
            "atr": None,
            "atr_pct": None,
            "bars_available": 0,
            "bars_required": period + 1,
            "period": period,
            "bar_seconds": bar_seconds,
        }
    bars: list[dict[str, float]] = []
    current_bucket: Optional[int] = None
    current: Optional[dict[str, float]] = None
    for ts, price in clean:
        bucket = int(ts // bar_seconds)
        if current_bucket != bucket:
            if current is not None:
                bars.append(current)
            current_bucket = bucket
            current = {"bucket": float(bucket), "open": price, "high": price, "low": price, "close": price, "start_ts": ts, "end_ts": ts}
        else:
            assert current is not None
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["end_ts"] = ts
    if current is not None:
        bars.append(current)

    if len(bars) < period + 1:
        return {
            "ready": False,
            "source": "app_observed_api_prices",
            "reason": f"need at least {period + 1} bars; have {len(bars)}",
            "atr": None,
            "atr_pct": None,
            "bars_available": len(bars),
            "bars_required": period + 1,
            "period": period,
            "bar_seconds": bar_seconds,
            "latest_close": bars[-1]["close"] if bars else None,
        }
    true_ranges: list[float] = []
    for idx in range(1, len(bars)):
        high = bars[idx]["high"]
        low = bars[idx]["low"]
        prev_close = bars[idx - 1]["close"]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return {
            "ready": False,
            "source": "app_observed_api_prices",
            "reason": f"need at least {period} true ranges; have {len(true_ranges)}",
            "atr": None,
            "atr_pct": None,
            "bars_available": len(bars),
            "bars_required": period + 1,
            "period": period,
            "bar_seconds": bar_seconds,
            "latest_close": bars[-1]["close"],
        }
    used_tr = true_ranges[-period:]
    atr = sum(used_tr) / period
    latest_close = bars[-1]["close"]
    atr_pct = (atr / latest_close) * 100.0 if latest_close > 0 else None
    return {
        "ready": bool(atr_pct is not None and atr_pct > 0 and math.isfinite(float(atr_pct))),
        "source": "app_observed_api_prices",
        "reason": "ok",
        "atr": atr,
        "atr_pct": atr_pct,
        "bars_available": len(bars),
        "bars_required": period + 1,
        "period": period,
        "bar_seconds": bar_seconds,
        "latest_close": latest_close,
        "latest_bar_high": bars[-1]["high"],
        "latest_bar_low": bars[-1]["low"],
        "true_ranges_used": len(used_tr),
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _safe_positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except Exception:
        return None
    if number <= 0 or not math.isfinite(number):
        return None
    return number


def suggested_hard_risk_defaults(
    investment_amount: float,
    *,
    market_price: Any = None,
    bid: Any = None,
    ask: Any = None,
    previous_close: Any = None,
    recent_move_pct: Any = None,
) -> dict[str, float | int]:
    """Return deterministic startup defaults for optional hard-risk limits.

    These values initialize new settings only. They must not track quote data or
    overwrite a value entered by the operator. The market arguments remain in
    the public signature for compatibility with existing callers, but they do
    not change this result.

    Numeric zero disables an individual loss, cycle, price, or gap limit. The
    spread guard keeps its explicit 1.00% startup value and changes only when a
    saved setting or direct user edit supplies another value.
    """
    return {
        "max_daily_loss_ticker": 0.0,
        "max_daily_loss_total": 0.0,
        "max_cycles_per_ticker_day": 0,
        "max_consecutive_losses": 0,
        "max_spread_pct": 1.00,
        "min_trade_price": 0.00,
        "max_gap_from_prev_close_pct": 0.0,
    }


def suggested_broker_timing_defaults(
    investment_amount: float,
    *,
    market_price: Any = None,
    previous_close: Any = None,
    recent_move_pct: Any = None,
) -> dict[str, float | int]:
    """Return defaults for broker/data freshness and open/close timing guards.

    These defaults provide values when the optional GUI guards are enabled.
    Larger planned trades get slightly longer open/close no-entry windows.
    If market data shows a larger
    move from previous close or higher recent movement, the recent-volatility
    limit is widened enough to avoid blocking merely because the default was too
    narrow, while still keeping a cap.
    """
    try:
        amount = max(0.0, float(investment_amount))
    except Exception:
        amount = 0.0
    price = _safe_positive_float(market_price)
    close_value = _safe_positive_float(previous_close)
    gap_pct: Optional[float] = None
    if price is not None and close_value is not None and close_value > 0:
        gap_pct = abs(((price / close_value) - 1.0) * 100.0)
    move_pct: Optional[float] = None
    try:
        if recent_move_pct is not None:
            move_pct = abs(float(recent_move_pct))
    except Exception:
        move_pct = None

    volatility_reference = max([v for v in [gap_pct, move_pct, 2.5] if v is not None])
    max_recent_move_pct = round(_clamp(max(3.0, volatility_reference * 2.0), 3.0, 10.0), 2)
    larger_trade = amount >= 25_000
    return {
        "max_selected_price_age_seconds": 3.0,
        "max_bid_ask_age_seconds": 3.0,
        "max_rth_status_age_seconds": 60.0,
        "volatility_window_seconds": 300,
        "max_recent_price_move_pct": max_recent_move_pct,
        "no_new_buy_first_minutes": 10 if larger_trade else 5,
        "no_new_buy_last_minutes": 20 if larger_trade else 15,
        "cancel_buy_before_close_minutes": 10 if larger_trade else 5,
    }

def projected_minimum_profit_levels(
    initial_drop_pct: float,
    buy_rebound_trail_pct: float,
    minimum_profit_pct: float,
    sell_trailing_stop_pct: float,
    *,
    anchor: float = 100.0,
    slippage_buffer_enabled: bool = False,
    slippage_buffer_pct: float = 0.0,
) -> dict[str, float]:
    """Projection for the strategy-input graph using Minimum profit semantics."""
    anchor = float(anchor)
    drop_trigger = anchor * (1.0 - float(initial_drop_pct) / 100.0)
    projected_buy_stop = drop_trigger * (1.0 + float(buy_rebound_trail_pct) / 100.0)
    protected_reference = projected_buy_stop
    buy_sizing_price = effective_buy_sizing_price(projected_buy_stop, slippage_buffer_enabled, slippage_buffer_pct)
    minimum_sell_stop = minimum_sell_stop_price_for_profit(
        projected_buy_stop,
        anchor,
        minimum_profit_pct,
        slippage_buffer_enabled=slippage_buffer_enabled,
        slippage_buffer_pct=slippage_buffer_pct,
    )
    s = float(sell_trailing_stop_pct) / 100.0
    if s >= 1.0:
        required_last_price = float("inf")
    else:
        required_last_price = minimum_sell_stop / max(1e-12, 1.0 - max(0.0, s))
    profit_vs_projected_buy_pct = ((minimum_sell_stop / projected_buy_stop) - 1.0) * 100.0 if projected_buy_stop > 0 else 0.0
    profit_vs_anchor_pct = ((minimum_sell_stop / anchor) - 1.0) * 100.0 if anchor > 0 else 0.0
    required_market_rise_pct = ((required_last_price / projected_buy_stop) - 1.0) * 100.0 if projected_buy_stop > 0 and math.isfinite(required_last_price) else 0.0
    return {
        "anchor": anchor,
        "drop_trigger": drop_trigger,
        "projected_buy_stop": projected_buy_stop,
        "buy_sizing_price": buy_sizing_price,
        "protected_reference": protected_reference,
        "minimum_sell_stop": minimum_sell_stop,
        "required_last_price": required_last_price,
        "profit_vs_projected_buy_pct": profit_vs_projected_buy_pct,
        "profit_vs_anchor_pct": profit_vs_anchor_pct,
        "required_market_rise_pct": required_market_rise_pct,
    }


def projected_anchor_stop_factor(
    initial_drop_pct: float,
    buy_rebound_trail_pct: float,
    rise_trigger_pct: float,
    sell_trailing_stop_pct: float,
) -> float:
    return projected_buy_factor_from_anchor(initial_drop_pct, buy_rebound_trail_pct) * initial_sell_stop_factor_for_min_profit(rise_trigger_pct)


def protected_anchor_profit_pct(
    initial_drop_pct: float,
    buy_rebound_trail_pct: float,
    rise_trigger_pct: float,
    sell_trailing_stop_pct: float,
) -> float:
    return (projected_anchor_stop_factor(initial_drop_pct, buy_rebound_trail_pct, rise_trigger_pct, sell_trailing_stop_pct) - 1.0) * 100.0


def protected_gross_profit_pct(rise_trigger_pct: float, sell_trailing_stop_pct: float) -> float:
    # Persisted compatibility name. The configured minimum profit is the protected gross
    # profit at the initial SELL stop, before fees/slippage.
    return min_configured_profit_pct(rise_trigger_pct)


# Compatibility helpers retained for existing imports. With minimum-profit
# semantics the app raises the activation price automatically, so no dynamic
# min/max coupling is needed between profit % and sell trail %.
def min_rise_trigger_pct_for_sell_trail(sell_trailing_stop_pct: float) -> float:
    return PROFIT_GUARD_EPSILON_PCT


def max_sell_trailing_stop_pct_for_rise_trigger(rise_trigger_pct: float) -> float:
    return 99.99


def min_rise_trigger_pct_for_projected_anchor_guard(
    initial_drop_pct: float,
    buy_rebound_trail_pct: float,
    sell_trailing_stop_pct: float,
) -> float:
    return PROFIT_GUARD_EPSILON_PCT


def max_sell_trailing_stop_pct_for_projected_anchor_guard(
    initial_drop_pct: float,
    buy_rebound_trail_pct: float,
    rise_trigger_pct: float,
) -> float:
    return 99.99


def _validation_float(value: Any) -> Optional[float]:
    """Return a finite float for validation, or None for invalid input."""
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _validation_int(value: Any) -> Optional[int]:
    """Return an int for validation, or None for invalid input."""
    try:
        return int(value)
    except Exception:
        return None


def utc_now_iso() -> str:
    """Return the app-standard UTC timestamp used in SQLite and captures."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Stage(str, Enum):
    IDLE = "IDLE"
    WAIT_INITIAL_DROP = "1_WAIT_INITIAL_DROP"
    BUY_TRAIL_ACTIVE = "2_BUY_TRAIL_ACTIVE"
    WAIT_RISE_TRIGGER = "3_WAIT_RISE_TRIGGER"
    SELL_TRAIL_ACTIVE = "4_SELL_TRAIL_ACTIVE"
    CYCLE_COMPLETE = "5_CYCLE_COMPLETE"
    STOPPED = "STOPPED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    ERROR = "ERROR"


class StopAction(str, Enum):
    CANCEL_OPEN_BOT_ORDERS = "CANCEL_OPEN_BOT_ORDERS"
    SELL_APP_POSITION_MARKET = "SELL_APP_POSITION_MARKET"
    LEAVE_ORDERS_WORKING = "LEAVE_ORDERS_WORKING"
    STOP_AFTER_CURRENT_CYCLE = "STOP_AFTER_CURRENT_CYCLE"
    STOP_NOW_NO_BROKER_ACTION = "STOP_NOW_NO_BROKER_ACTION"


@dataclass(slots=True)
class ConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 11
    account: str = ""
    trading_mode: str = "live"  # paper or live; selected by the connection profile.
    platform: str = GATEWAY_PLATFORM  # tws or gateway; both use the TWS socket API.
    platform_path: str = ""  # Optional local .exe path used only by the Start helper button.
    market_data_type: int = 0  # 0=auto best available, 1=live, 2=frozen, 3=delayed, 4=delayed-frozen.

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not str(self.host or "").strip():
            errors.append("Host is required.")

        port = _validation_int(self.port)
        if port is None or not 1 <= port <= 65535:
            errors.append("Port must be between 1 and 65535.")

        client_id = _validation_int(self.client_id)
        if client_id is None or client_id < 0:
            errors.append("Client ID must be zero or higher.")

        if str(self.trading_mode or "") not in {"paper", "live"}:
            errors.append("Trading mode must be paper or live.")
        if str(self.platform or "").strip().lower() not in SUPPORTED_PLATFORMS:
            errors.append("Platform must be Trader Workstation or IB Gateway.")

        market_data_type = _validation_int(self.market_data_type)
        if market_data_type not in {0, 1, 2, 3, 4}:
            errors.append("Market data type must be 0 auto, 1 live, 2 frozen, 3 delayed, or 4 delayed-frozen.")
        return errors

    def normalized_platform(self) -> str:
        value = (self.platform or TWS_PLATFORM).strip().lower()
        return value if value in SUPPORTED_PLATFORMS else TWS_PLATFORM


@dataclass(slots=True)
class StrategySettings:
    ticker: str = ""
    investment_amount: float = 10000.0
    initial_drop_pct: float = 2.0
    buy_rebound_trail_pct: float = 1.0
    rise_trigger_pct: float = 3.0
    sell_trailing_stop_pct: float = 1.0

    # Optional ATR-adaptive mode. When enabled and enough app-observed API
    # price bars are available, the controller rewrites the normal percentage
    # fields above from ATR% * each multiplier. Minimum profit % can be kept
    # manual through atr_adapt_minimum_profit_enabled. Protective SELL can also
    # be ATR-derived through an explicit optional setting. By default, fresh BUY
    # entries wait until enough RTH-only ATR data is available.
    atr_adaptive_enabled: bool = True
    atr_adapt_minimum_profit_enabled: bool = True
    atr_block_new_buy_until_ready: bool = True
    atr_adapt_protective_sell_enabled: bool = False
    atr_protective_sell_multiplier: float = 3.00
    atr_period: int = 14
    atr_bar_seconds: int = 60
    atr_initial_drop_multiplier: float = 1.50
    atr_buy_rebound_multiplier: float = 0.75
    atr_minimum_profit_multiplier: float = 1.00
    atr_sell_trail_multiplier: float = 1.00
    atr_min_pct: float = 0.10
    atr_max_pct: float = 20.00

    # Optional protective-exit leg. When enabled, the app submits a native SELL
    # trailing-stop immediately after a BUY fill, then cancels/replaces it with
    # the profit-protecting SELL trail once the minimum-profit condition is met.
    protective_sell_enabled: bool = False
    protective_sell_trailing_stop_pct: float = 3.0

    # Optional BUY-sizing buffer. It does not change the order sent to IBKR; it
    # only sizes quantity more conservatively because the BUY TRAIL triggers a
    # market order and can fill above the displayed stop in fast markets.
    slippage_buffer_enabled: bool = False
    slippage_buffer_pct: float = 0.50

    # Optional hard guard rails. A zero value means that individual limit is off.
    hard_risk_limits_enabled: bool = False
    max_daily_loss_ticker: float = 0.0
    max_daily_loss_total: float = 0.0
    # Persisted under its compatibility database name. The GUI and runtime
    # treat this as a total completed-cycle cap for the selected ticker, not as
    # a per-day limit.
    max_cycles_per_ticker_day: int = 0
    max_consecutive_losses: int = 0
    max_spread_pct: float = 1.00
    min_trade_price: float = 0.00
    max_gap_from_prev_close_pct: float = 0.0
    block_delayed_data_in_live: bool = True

    # Broker/account guard. Runs an IBKR what-if margin check before a BUY order
    # is transmitted. Enabled by default because it does not place an order.
    what_if_check_enabled: bool = True

    # Fresh-data guard. Suggested defaults are enabled: selected strategy price
    # and bid/ask data should be current before a new BUY can be sent.
    stale_data_guard_enabled: bool = True
    max_selected_price_age_seconds: float = 3.0
    max_bid_ask_age_seconds: float = 3.0
    max_rth_status_age_seconds: float = 60.0

    # Volatility guard. Uses app-observed recent prices, not historical bars.
    # Default is off; enable it to block new BUY orders when the recent price
    # range is too wide.
    volatility_filter_enabled: bool = False
    volatility_window_seconds: int = 300
    max_recent_price_move_pct: float = 5.0

    # Session-timing guard. Default is on. It blocks fresh entries near the
    # contract regular-session open/close and can cancel unfilled BUY trails shortly before close.
    session_timing_guard_enabled: bool = True
    no_new_buy_first_minutes: int = 5
    no_new_buy_last_minutes: int = 15
    cancel_buy_before_close_minutes: int = 5

    # Optional Stage-4 close policy. When enabled, the controller requests
    # cancellation of the active final SELL trail before the contract's RTH
    # close, waits for broker confirmation, then submits a DAY market SELL for
    # only the remaining app-owned quantity. It is independent of the BUY
    # session-timing guard and is disabled by default.
    cancel_sell_and_liquidate_before_close_enabled: bool = False
    liquidate_before_close_minutes: int = 5

    reinvest_profits: bool = True
    auto_repeat: bool = True
    rth_only: bool = True  # Safety guard: submit/activate strategy orders only during regular trading hours.
    exchange: str = "SMART"
    primary_exchange: str = ""  # Optional native exchange for contract disambiguation, e.g. NASDAQ.
    contract_con_id: Optional[int] = None  # Optional exact IBKR conId selected from API contract search.
    currency: str = "USD"
    sec_type: str = "STK"
    tif: str = "GTC"

    def normalized_ticker(self) -> str:
        return str(self.ticker or "").strip().upper()

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.normalized_ticker():
            errors.append("Ticker is required.")

        investment_amount = _validation_float(self.investment_amount)
        if investment_amount is None or investment_amount <= 0:
            errors.append("Investment amount must be greater than zero.")

        # Buy rebound/trailing % and SELL trailing-stop % intentionally allow 0.
        # A zero value disables the broker-native trailing leg for that side:
        # the BUY becomes a market order immediately after the initial-drop
        # condition, and the final SELL becomes a market order once the
        # minimum-profit condition is reached. Other percentages still require
        # positive values because they define actual guard levels.
        positive_pct_fields = [
            ("Initial drop %", self.initial_drop_pct),
            ("Minimum profit %", self.rise_trigger_pct),
            ("Protective sell trailing-stop %", self.protective_sell_trailing_stop_pct),
            ("Slippage buffer %", self.slippage_buffer_pct if self.slippage_buffer_enabled else 0.01),
        ]
        zero_allowed_pct_fields = [
            ("Buy rebound/trailing %", self.buy_rebound_trail_pct),
            ("Sell trailing-stop %", self.sell_trailing_stop_pct),
        ]
        pct_values: dict[str, Optional[float]] = {}
        for field_name, value in positive_pct_fields:
            number = _validation_float(value)
            pct_values[field_name] = number
            if number is None:
                errors.append(f"{field_name} must be a finite number.")
                continue
            if number <= 0:
                errors.append(f"{field_name} must be greater than zero.")
            if number >= 100:
                errors.append(f"{field_name} must be less than 100%.")
        for field_name, value in zero_allowed_pct_fields:
            number = _validation_float(value)
            pct_values[field_name] = number
            if number is None:
                errors.append(f"{field_name} must be a finite number.")
                continue
            if number < 0:
                errors.append(f"{field_name} must be zero or greater.")
            if number >= 100:
                errors.append(f"{field_name} must be less than 100%.")

        rise_trigger_pct = pct_values.get("Minimum profit %")
        if rise_trigger_pct is not None and 0 < rise_trigger_pct < PROFIT_GUARD_EPSILON_PCT:
            errors.append(f"Minimum profit % must be at least {PROFIT_GUARD_EPSILON_PCT:.2f}%.")

        if self.atr_adaptive_enabled:
            atr_period = _validation_int(self.atr_period)
            if atr_period is None or atr_period < 2:
                errors.append("ATR period must be at least 2 bars.")
            atr_bar_seconds = _validation_int(self.atr_bar_seconds)
            if atr_bar_seconds is None or atr_bar_seconds < 5:
                errors.append("ATR bar size must be at least 5 seconds.")
            for field_name, value in [
                ("ATR initial-drop multiplier", self.atr_initial_drop_multiplier),
                ("ATR buy-rebound multiplier", self.atr_buy_rebound_multiplier),
                ("ATR minimum-profit multiplier", self.atr_minimum_profit_multiplier),
                ("ATR sell-trail multiplier", self.atr_sell_trail_multiplier),
                ("ATR protective SELL multiplier", self.atr_protective_sell_multiplier),
            ]:
                if field_name == "ATR minimum-profit multiplier" and not bool(getattr(self, "atr_adapt_minimum_profit_enabled", True)):
                    continue
                if field_name == "ATR protective SELL multiplier" and not bool(getattr(self, "atr_adapt_protective_sell_enabled", False)):
                    continue
                number = _validation_float(value)
                if number is None:
                    errors.append(f"{field_name} must be a finite number.")
                    continue
                # Buy-rebound and sell-trail multipliers may be 0 to write 0%
                # into the normal trailing fields, disabling trailing for that
                # side while ATR adaptive mode remains active for the other fields.
                if field_name in {"ATR buy-rebound multiplier", "ATR sell-trail multiplier"}:
                    if number < 0:
                        errors.append(f"{field_name} must be zero or greater.")
                    continue
                if number <= 0:
                    errors.append(f"{field_name} must be greater than zero.")
            atr_min_pct = _validation_float(self.atr_min_pct)
            atr_max_pct = _validation_float(self.atr_max_pct)
            if atr_min_pct is None or atr_max_pct is None or atr_min_pct <= 0 or atr_max_pct <= 0 or atr_min_pct >= atr_max_pct:
                errors.append("ATR min/max percentage bounds must be positive and min must be below max.")

        if self.hard_risk_limits_enabled:
            max_daily_loss_ticker = _validation_float(self.max_daily_loss_ticker)
            max_daily_loss_total = _validation_float(self.max_daily_loss_total)
            if max_daily_loss_ticker is None or max_daily_loss_total is None or max_daily_loss_ticker < 0 or max_daily_loss_total < 0:
                errors.append("Daily loss limits must be zero or greater.")
            max_cycles_per_ticker_day = _validation_int(self.max_cycles_per_ticker_day)
            max_consecutive_losses = _validation_int(self.max_consecutive_losses)
            if max_cycles_per_ticker_day is None or max_consecutive_losses is None or max_cycles_per_ticker_day < 0 or max_consecutive_losses < 0:
                errors.append("Cycle and consecutive-loss limits must be zero or greater.")
            max_spread_pct = _validation_float(self.max_spread_pct)
            max_gap_from_prev_close_pct = _validation_float(self.max_gap_from_prev_close_pct)
            if max_spread_pct is None or max_gap_from_prev_close_pct is None or max_spread_pct < 0 or max_gap_from_prev_close_pct < 0:
                errors.append("Spread and gap limits must be zero or greater.")
            min_trade_price = _validation_float(self.min_trade_price)
            if min_trade_price is None or min_trade_price < 0:
                errors.append("Minimum trade price must be zero or greater.")
        if self.what_if_check_enabled not in {True, False}:
            errors.append("What-if margin check setting must be true or false.")
        if self.stale_data_guard_enabled:
            selected_age = _validation_float(self.max_selected_price_age_seconds)
            bid_ask_age = _validation_float(self.max_bid_ask_age_seconds)
            rth_age = _validation_float(self.max_rth_status_age_seconds)
            if selected_age is None or bid_ask_age is None or rth_age is None or selected_age <= 0 or bid_ask_age <= 0 or rth_age <= 0:
                errors.append("Stale-data guard ages must be greater than zero seconds.")
        if self.volatility_filter_enabled:
            volatility_window = _validation_int(self.volatility_window_seconds)
            if volatility_window is None or volatility_window <= 0:
                errors.append("Volatility window must be greater than zero seconds.")
            max_recent_move = _validation_float(self.max_recent_price_move_pct)
            if max_recent_move is None or max_recent_move <= 0:
                errors.append("Max recent price move % must be greater than zero.")
        if self.session_timing_guard_enabled:
            first_minutes = _validation_int(self.no_new_buy_first_minutes)
            last_minutes = _validation_int(self.no_new_buy_last_minutes)
            cancel_minutes = _validation_int(self.cancel_buy_before_close_minutes)
            if first_minutes is None or last_minutes is None or cancel_minutes is None or first_minutes < 0 or last_minutes < 0 or cancel_minutes < 0:
                errors.append("Session timing guard minutes must be zero or greater.")
        if self.cancel_sell_and_liquidate_before_close_enabled not in {True, False}:
            errors.append("Cancel-and-liquidate before close setting must be true or false.")
        if self.cancel_sell_and_liquidate_before_close_enabled:
            liquidate_minutes = _validation_int(self.liquidate_before_close_minutes)
            if liquidate_minutes is None or not 1 <= liquidate_minutes <= 240:
                errors.append("Liquidate-before-close minutes must be between 1 and 240.")

        if str(self.exchange or "").upper() != "SMART":
            errors.append("V1 supports SMART routing only.")
        primary_exchange = str(self.primary_exchange or "")
        if primary_exchange and not primary_exchange.strip().replace("-", "").isalnum():
            errors.append("Primary exchange may contain only letters/numbers/hyphen, or leave it blank.")
        if self.contract_con_id is not None:
            try:
                if int(self.contract_con_id) <= 0:
                    errors.append("IBKR conId must be blank or a positive integer.")
            except Exception:
                errors.append("IBKR conId must be blank or a positive integer.")
        if str(self.currency or "").upper() != "USD":
            errors.append("V1 supports USD stocks only.")
        if str(self.sec_type or "").upper() != "STK":
            errors.append("V1 supports STK contracts only.")
        return errors


def strategy_with_atr_adaptive_percentages(settings: StrategySettings, atr_pct: Any) -> tuple[StrategySettings, dict[str, float]]:
    """Return a StrategySettings copy with ATR-derived manual percentage fields.

    The returned settings object keeps ``atr_adaptive_enabled`` set. The
    order-driving percentage fields are overwritten so the rest of the strategy
    engine continues to use one code path. Minimum profit % and Protective SELL
    trailing-stop % remain manual unless their specific ATR toggles are enabled.
    """
    try:
        base_atr_pct = float(atr_pct)
    except Exception:
        base_atr_pct = 0.0
    if base_atr_pct <= 0 or not math.isfinite(base_atr_pct):
        return settings, {}
    lower = max(0.01, float(getattr(settings, "atr_min_pct", 0.10) or 0.10))
    upper = max(lower, min(99.99, float(getattr(settings, "atr_max_pct", 20.0) or 20.0)))

    def calc(mult: Any, *, allow_zero: bool = False) -> float:
        try:
            multiplier = float(mult)
        except Exception:
            multiplier = 0.0 if allow_zero else 1.0
        if allow_zero and multiplier <= 0:
            return 0.0
        value = base_atr_pct * multiplier
        return clamp_pct(value, lower, upper)

    adapt_minimum_profit = bool(getattr(settings, "atr_adapt_minimum_profit_enabled", True))
    adapt_protective_sell = bool(getattr(settings, "atr_adapt_protective_sell_enabled", False))
    manual_minimum_profit = float(getattr(settings, "rise_trigger_pct", 0.0) or 0.0)
    manual_protective_sell = float(getattr(settings, "protective_sell_trailing_stop_pct", 0.0) or 0.0)
    adaptive = {
        "atr_pct": round(base_atr_pct, 4),
        "initial_drop_pct": calc(getattr(settings, "atr_initial_drop_multiplier", 1.50)),
        "buy_rebound_trail_pct": calc(getattr(settings, "atr_buy_rebound_multiplier", 0.75), allow_zero=True),
        "rise_trigger_pct": calc(getattr(settings, "atr_minimum_profit_multiplier", 1.00)) if adapt_minimum_profit else manual_minimum_profit,
        "sell_trailing_stop_pct": calc(getattr(settings, "atr_sell_trail_multiplier", 1.00), allow_zero=True),
        "protective_sell_trailing_stop_pct": calc(getattr(settings, "atr_protective_sell_multiplier", 3.00)) if adapt_protective_sell else manual_protective_sell,
        "atr_adapt_minimum_profit_enabled": adapt_minimum_profit,
        "atr_adapt_protective_sell_enabled": adapt_protective_sell,
    }
    replace_kwargs = dict(
        initial_drop_pct=adaptive["initial_drop_pct"],
        buy_rebound_trail_pct=adaptive["buy_rebound_trail_pct"],
        rise_trigger_pct=adaptive["rise_trigger_pct"],
        sell_trailing_stop_pct=adaptive["sell_trailing_stop_pct"],
    )
    if adapt_protective_sell:
        replace_kwargs["protective_sell_trailing_stop_pct"] = adaptive["protective_sell_trailing_stop_pct"]
    updated = replace(settings, **replace_kwargs)
    return updated, adaptive


@dataclass(slots=True)
class CycleState:
    id: str
    cycle_number: int
    ticker: str
    stage: Stage
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    account: str = ""
    con_id: Optional[int] = None
    exchange: str = "SMART"
    primary_exchange: str = ""
    currency: str = "USD"
    rth_only: bool = True

    investment_amount: float = 0.0
    budget: float = 0.0
    reinvest_profits: bool = True
    reinvested_profit: float = 0.0

    initial_drop_pct: float = 0.0
    buy_rebound_trail_pct: float = 0.0
    rise_trigger_pct: float = 0.0
    sell_trailing_stop_pct: float = 0.0
    atr_adaptive_enabled: bool = True
    atr_adapt_minimum_profit_enabled: bool = True
    atr_block_new_buy_until_ready: bool = True
    atr_adapt_protective_sell_enabled: bool = False
    atr_protective_sell_multiplier: float = 3.00
    atr_period: int = 14
    atr_bar_seconds: int = 60
    atr_initial_drop_multiplier: float = 1.50
    atr_buy_rebound_multiplier: float = 0.75
    atr_minimum_profit_multiplier: float = 1.00
    atr_sell_trail_multiplier: float = 1.00
    atr_min_pct: float = 0.10
    atr_max_pct: float = 20.00

    protective_sell_enabled: bool = False
    protective_sell_trailing_stop_pct: float = 0.0
    slippage_buffer_enabled: bool = False
    slippage_buffer_pct: float = 0.0
    hard_risk_limits_enabled: bool = False
    max_daily_loss_ticker: float = 0.0
    max_daily_loss_total: float = 0.0
    max_cycles_per_ticker_day: int = 0
    max_consecutive_losses: int = 0
    max_spread_pct: float = 1.00
    min_trade_price: float = 0.00
    max_gap_from_prev_close_pct: float = 0.0
    block_delayed_data_in_live: bool = True
    what_if_check_enabled: bool = True
    stale_data_guard_enabled: bool = True
    max_selected_price_age_seconds: float = 3.0
    max_bid_ask_age_seconds: float = 3.0
    max_rth_status_age_seconds: float = 60.0
    volatility_filter_enabled: bool = False
    volatility_window_seconds: int = 300
    max_recent_price_move_pct: float = 5.0
    session_timing_guard_enabled: bool = True
    no_new_buy_first_minutes: int = 5
    no_new_buy_last_minutes: int = 15
    cancel_buy_before_close_minutes: int = 5
    cancel_sell_and_liquidate_before_close_enabled: bool = False
    liquidate_before_close_minutes: int = 5
    recovery_required: bool = False
    close_position_market_requested: bool = False
    close_before_rth_liquidation_requested: bool = False
    close_before_rth_cancel_requested: bool = False

    anchor_price: Optional[float] = None
    last_price: Optional[float] = None
    drop_trigger_price: Optional[float] = None
    buy_initial_trail_stop_price: Optional[float] = None
    rise_trigger_price: Optional[float] = None
    sell_initial_trail_stop_price: Optional[float] = None

    quantity: int = 0
    buy_order_id: Optional[int] = None
    buy_perm_id: Optional[int] = None
    buy_order_ref: Optional[str] = None
    buy_status: Optional[str] = None
    buy_filled_qty: int = 0
    avg_buy_price: Optional[float] = None
    buy_commission: float = 0.0
    buy_filled_at: Optional[str] = None

    protective_sell_order_id: Optional[int] = None
    protective_sell_perm_id: Optional[int] = None
    protective_sell_order_ref: Optional[str] = None
    protective_sell_status: Optional[str] = None
    protective_sell_initial_stop_price: Optional[float] = None
    protective_sell_cancel_requested: bool = False
    protective_sell_filled_qty: int = 0
    protective_avg_sell_price: Optional[float] = None
    protective_sell_commission: float = 0.0
    protective_sell_filled_at: Optional[str] = None

    sell_order_id: Optional[int] = None
    sell_perm_id: Optional[int] = None
    sell_order_ref: Optional[str] = None
    sell_status: Optional[str] = None
    sell_filled_qty: int = 0
    avg_sell_price: Optional[float] = None
    sell_commission: float = 0.0
    sell_filled_at: Optional[str] = None

    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    stop_after_current_cycle: bool = False
    error_message: Optional[str] = None

    @classmethod
    def new(cls, settings: StrategySettings, cycle_number: int, account: str, last_price: float, reinvested_profit: float) -> "CycleState":
        cycle = cls.new_pending(settings=settings, cycle_number=cycle_number, account=account, reinvested_profit=reinvested_profit)
        cycle.anchor_price = float(last_price)
        cycle.last_price = float(last_price)
        cycle.drop_trigger_price = float(last_price) * (1.0 - settings.initial_drop_pct / 100.0)
        return cycle

    @classmethod
    def new_pending(cls, settings: StrategySettings, cycle_number: int, account: str, reinvested_profit: float) -> "CycleState":
        ticker = settings.normalized_ticker()
        budget = settings.investment_amount + (reinvested_profit if settings.reinvest_profits else 0.0)
        return cls(
            id=str(uuid.uuid4()),
            cycle_number=cycle_number,
            ticker=ticker,
            stage=Stage.WAIT_INITIAL_DROP,
            account=account,
            exchange=settings.exchange.upper(),
            primary_exchange=settings.primary_exchange.strip().upper(),
            currency=settings.currency.upper(),
            rth_only=bool(getattr(settings, "rth_only", True)),
            investment_amount=settings.investment_amount,
            budget=budget,
            reinvest_profits=settings.reinvest_profits,
            reinvested_profit=reinvested_profit if settings.reinvest_profits else 0.0,
            initial_drop_pct=settings.initial_drop_pct,
            buy_rebound_trail_pct=settings.buy_rebound_trail_pct,
            rise_trigger_pct=settings.rise_trigger_pct,
            sell_trailing_stop_pct=settings.sell_trailing_stop_pct,
            atr_adaptive_enabled=bool(settings.atr_adaptive_enabled),
            atr_adapt_minimum_profit_enabled=bool(getattr(settings, 'atr_adapt_minimum_profit_enabled', True)),
            atr_block_new_buy_until_ready=bool(getattr(settings, 'atr_block_new_buy_until_ready', True)),
            atr_adapt_protective_sell_enabled=bool(getattr(settings, 'atr_adapt_protective_sell_enabled', False)),
            atr_protective_sell_multiplier=float(getattr(settings, 'atr_protective_sell_multiplier', 3.0)),
            atr_period=int(settings.atr_period),
            atr_bar_seconds=int(settings.atr_bar_seconds),
            atr_initial_drop_multiplier=float(settings.atr_initial_drop_multiplier),
            atr_buy_rebound_multiplier=float(settings.atr_buy_rebound_multiplier),
            atr_minimum_profit_multiplier=float(settings.atr_minimum_profit_multiplier),
            atr_sell_trail_multiplier=float(settings.atr_sell_trail_multiplier),
            atr_min_pct=float(settings.atr_min_pct),
            atr_max_pct=float(settings.atr_max_pct),
            protective_sell_enabled=bool(settings.protective_sell_enabled),
            protective_sell_trailing_stop_pct=float(settings.protective_sell_trailing_stop_pct),
            slippage_buffer_enabled=bool(settings.slippage_buffer_enabled),
            slippage_buffer_pct=float(settings.slippage_buffer_pct),
            hard_risk_limits_enabled=bool(settings.hard_risk_limits_enabled),
            max_daily_loss_ticker=float(settings.max_daily_loss_ticker),
            max_daily_loss_total=float(settings.max_daily_loss_total),
            max_cycles_per_ticker_day=int(settings.max_cycles_per_ticker_day),
            max_consecutive_losses=int(settings.max_consecutive_losses),
            max_spread_pct=float(settings.max_spread_pct),
            min_trade_price=float(settings.min_trade_price),
            max_gap_from_prev_close_pct=float(settings.max_gap_from_prev_close_pct),
            block_delayed_data_in_live=bool(settings.block_delayed_data_in_live),
            what_if_check_enabled=bool(settings.what_if_check_enabled),
            stale_data_guard_enabled=bool(settings.stale_data_guard_enabled),
            max_selected_price_age_seconds=float(settings.max_selected_price_age_seconds),
            max_bid_ask_age_seconds=float(settings.max_bid_ask_age_seconds),
            max_rth_status_age_seconds=float(settings.max_rth_status_age_seconds),
            volatility_filter_enabled=bool(settings.volatility_filter_enabled),
            volatility_window_seconds=int(settings.volatility_window_seconds),
            max_recent_price_move_pct=float(settings.max_recent_price_move_pct),
            session_timing_guard_enabled=bool(settings.session_timing_guard_enabled),
            no_new_buy_first_minutes=int(settings.no_new_buy_first_minutes),
            no_new_buy_last_minutes=int(settings.no_new_buy_last_minutes),
            cancel_buy_before_close_minutes=int(settings.cancel_buy_before_close_minutes),
            cancel_sell_and_liquidate_before_close_enabled=bool(
                settings.cancel_sell_and_liquidate_before_close_enabled
            ),
            liquidate_before_close_minutes=int(settings.liquidate_before_close_minutes),
        )

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stage"] = self.stage.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CycleState":
        values = dict(data)
        values["stage"] = Stage(values["stage"])
        # Existing portable SQLite files and compatibility fixtures may omit
        # additive fields. Dataclass defaults fill missing values; unknown columns
        # are ignored so forward/additive migrations remain safe.
        allowed = set(cls.__dataclass_fields__)
        filtered = {key: value for key, value in values.items() if key in allowed}
        return cls(**filtered)

    def snapshot(self) -> dict[str, Any]:
        return self.to_dict()


def recovery_cycle_signature(cycle: Any) -> dict[str, Any]:
    """Return only local facts that affect broker reconciliation freshness.

    Price observations and editable strategy percentages are intentionally
    excluded. A probe becomes stale when identity, stage, recovery, order, or
    fill facts change, not merely because another market-data tick was stored.
    """
    if cycle is None:
        return {}
    fields = (
        "id",
        "ticker",
        "account",
        "stage",
        "recovery_required",
        "buy_order_ref",
        "buy_order_id",
        "buy_perm_id",
        "buy_status",
        "buy_filled_qty",
        "avg_buy_price",
        "buy_filled_at",
        "protective_sell_order_ref",
        "protective_sell_order_id",
        "protective_sell_perm_id",
        "protective_sell_status",
        "protective_sell_filled_qty",
        "protective_avg_sell_price",
        "protective_sell_filled_at",
        "sell_order_ref",
        "sell_order_id",
        "sell_perm_id",
        "sell_status",
        "sell_filled_qty",
        "avg_sell_price",
        "sell_filled_at",
    )
    if isinstance(cycle, dict):
        signature = {field_name: cycle.get(field_name) for field_name in fields}
    else:
        signature = {field_name: getattr(cycle, field_name, None) for field_name in fields}
    signature["stage"] = getattr(signature.get("stage"), "value", signature.get("stage"))
    signature["recovery_required"] = bool(signature.get("recovery_required"))
    return signature


@dataclass(slots=True)
class StrategyAction:
    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppSnapshot:
    connected: bool = False
    status: str = "Disconnected"
    db_path: str = ""
    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    active_cycle: Optional[CycleState] = None
    last_events: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        def default(obj: Any) -> Any:
            if isinstance(obj, Enum):
                return obj.value
            if hasattr(obj, "to_dict"):
                return obj.to_dict()
            if hasattr(obj, "__dict__"):
                return obj.__dict__
            return str(obj)

        return json.dumps(self, default=default)
