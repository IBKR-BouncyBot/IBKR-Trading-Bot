"""Read-only diagnostics for broker-native trailing orders.

The GUI-selected strategy price can be marketPrice or bid/ask midpoint, while
submitted IBKR stop-style orders use an explicit broker trigger method. These
helpers explain the distinction without predicting or reproducing broker-side
trigger state.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from .models import Stage


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
        return result if isfinite(result) and result > 0 else None
    except Exception:
        return None


def last_trigger_value(fields: dict[str, Any]) -> tuple[float | None, str]:
    """Return the best app-visible approximation of IBKR triggerMethod=Last.

    IBKR's actual native/simulated order trigger is broker-side.  The app can
    only show the latest raw market-data fields it received.  Prefer live `last`
    over delayed `delayedLast` so the diagnostic matches the explicit
    triggerMethod=2 used by the app's native trailing orders as closely as the
    available feed allows.
    """
    live_last = _as_float(fields.get("last"))
    if live_last is not None:
        return live_last, "last"
    delayed_last = _as_float(fields.get("delayedLast"))
    if delayed_last is not None:
        return delayed_last, "delayedLast"
    return None, "last/delayedLast unavailable"


def native_trailing_order_diagnostics(
    *,
    stage: str | Stage | None,
    fields: dict[str, Any] | None,
    selected_price: Any,
    buy_initial_stop: Any = None,
    sell_initial_stop: Any = None,
    buy_order_ref: str | None = None,
    sell_order_ref: str | None = None,
    trigger_method: int = 2,
) -> dict[str, Any]:
    """Build a compact explanation for active native trailing orders.

    The app uses triggerMethod=2 (Last) for submitted native BUY/SELL TRAIL
    orders.  The selected GUI price may be marketPrice/midpoint, so crossing a
    displayed line is not, by itself, proof that TWS has triggered the order.
    """
    stage_value = stage.value if isinstance(stage, Stage) else str(stage or "")
    fields = fields or {}
    selected = _as_float(selected_price)
    raw_last, raw_last_source = last_trigger_value(fields)
    buy_stop = _as_float(buy_initial_stop)
    sell_stop = _as_float(sell_initial_stop)

    active_side = None
    initial_stop = None
    selected_crossed = None
    raw_last_crossed = None

    buy_ref = str(buy_order_ref or "")
    sell_ref = str(sell_order_ref or "")
    if stage_value == Stage.BUY_TRAIL_ACTIVE.value and "BUY_MARKET" in buy_ref:
        return {
            "active": False,
            "side": "BUY",
            "order_type": "MKT",
            "message": "BUY market order is active because BUY rebound/trail % is 0. No native trailing trigger is used; waiting for broker fill/status.",
        }
    if stage_value == Stage.SELL_TRAIL_ACTIVE.value and "SELL_MARKET" in sell_ref:
        return {
            "active": False,
            "side": "SELL",
            "order_type": "MKT",
            "message": "SELL market order is active because SELL trailing-stop % is 0. No native trailing trigger is used; waiting for broker fill/status.",
        }

    if stage_value == Stage.BUY_TRAIL_ACTIVE.value and buy_stop is not None:
        active_side = "BUY"
        initial_stop = buy_stop
        selected_crossed = bool(selected is not None and selected >= buy_stop)
        raw_last_crossed = bool(raw_last is not None and raw_last >= buy_stop)
    elif stage_value == Stage.SELL_TRAIL_ACTIVE.value and sell_stop is not None:
        active_side = "SELL"
        initial_stop = sell_stop
        selected_crossed = bool(selected is not None and selected <= sell_stop)
        raw_last_crossed = bool(raw_last is not None and raw_last <= sell_stop)

    if active_side is None:
        return {
            "active": False,
            "message": "No native trailing order is active.",
        }

    if trigger_method == 2:
        trigger_label = "Last"
    else:
        trigger_label = f"method {trigger_method}"

    if raw_last is None:
        message = (
            f"{active_side} TRAIL is broker-managed. App has no raw last/delayedLast field; "
            f"TWS order trigger uses {trigger_label}."
        )
    elif selected_crossed and not raw_last_crossed:
        message = (
            f"Selected app price crossed the displayed initial stop, but raw {raw_last_source} has not. "
            f"TWS order trigger uses {trigger_label}; waiting for broker fill/status."
        )
    elif raw_last_crossed:
        message = (
            f"Raw {raw_last_source} has crossed the displayed initial stop. If no fill appears, verify the "
            f"TWS order row/status because the actual native trailing stop may have moved inside TWS."
        )
    else:
        message = f"Waiting for raw {raw_last_source} to cross the native {active_side} TRAIL trigger."

    return {
        "active": True,
        "side": active_side,
        "trigger_method": trigger_method,
        "trigger_method_label": trigger_label,
        "raw_last_source": raw_last_source,
        "raw_last_value": raw_last,
        "selected_price": selected,
        "displayed_initial_stop": initial_stop,
        "selected_crossed_displayed_initial_stop": selected_crossed,
        "raw_last_crossed_displayed_initial_stop": raw_last_crossed,
        "message": message,
        "note": "Displayed stop is the app-submitted initial stop. The current native trailing stop can move inside TWS and is not guaranteed to equal this value.",
    }
