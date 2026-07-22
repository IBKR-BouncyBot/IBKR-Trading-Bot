"""Pure broker-neutral state machine for the five-stage trading cycle.

The engine never calls TWS/Gateway, Qt, or SQLite and never mutates the caller's
cycle object. Each transition returns a copied ``CycleState`` plus declarative
``StrategyAction`` values for the controller to validate and execute.

The pure boundary covers anchor/drop logic, zero or positive BUY/SELL trail
branches, whole-share sizing, minimum-profit activation, protective-exit state,
partial fills, cycle completion, and safe mid-cycle setting updates.
"""

from __future__ import annotations

from copy import copy
from math import floor
from typing import Optional

from .models import (
    APP_ORDER_PREFIX,
    CycleState,
    Stage,
    StrategyAction,
    StrategySettings,
    minimum_sell_stop_price_for_profit,
    utc_now_iso,
)

TERMINAL_ORDER_STATUSES = {"Filled", "Cancelled", "Inactive", "ApiCancelled"}
WORKING_ORDER_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending"}


def round_price(price: float, decimals: int = 4) -> float:
    return round(float(price), decimals)


def make_order_ref(ticker: str, cycle_number: int, cycle_id: str, leg: str) -> str:
    short_id = cycle_id.split("-")[0]
    return f"{APP_ORDER_PREFIX}|{ticker.upper()}|CYCLE-{cycle_number:06d}|{short_id}|{leg}"


def _minimum_profit_stop_price(cycle: CycleState) -> float:
    """Initial SELL stop required to protect minimum profit plus optional buffer.

    cycle.rise_trigger_pct is retained as the persisted compatibility field
    name; the UI presents it as Minimum profit %. When the optional
    slippage buffer is enabled, the model raises the initial stop so a
    hypothetical SELL fill worse than the stop by that buffer can still meet the
    configured minimum profit before commissions.
    """
    return round_price(
        minimum_sell_stop_price_for_profit(
            avg_buy_price=cycle.avg_buy_price,
            anchor_price=cycle.anchor_price,
            minimum_profit_pct=cycle.rise_trigger_pct,
            slippage_buffer_enabled=bool(getattr(cycle, "slippage_buffer_enabled", False)),
            slippage_buffer_pct=float(getattr(cycle, "slippage_buffer_pct", 0.0) or 0.0),
        )
    )


def _safe_rise_trigger_price(cycle: CycleState) -> float:
    if cycle.avg_buy_price is None or cycle.avg_buy_price <= 0:
        return 0.0
    stop_price = _minimum_profit_stop_price(cycle)
    s = max(0.0, float(cycle.sell_trailing_stop_pct) / 100.0)
    if s >= 1.0:
        return float("inf")
    return round_price(stop_price / (1.0 - s))


def _buy_sizing_price(cycle: CycleState, initial_buy_stop: float) -> float:
    """Return the conservative price used for BUY quantity sizing.

    The order sent to IBKR is still a native BUY trailing stop using
    ``initial_buy_stop``. The optional slippage buffer only lowers quantity so a
    market-triggered BUY fill is less likely to exceed the user's budget.
    """
    sizing = float(initial_buy_stop)
    if bool(getattr(cycle, "slippage_buffer_enabled", False)):
        sizing *= 1.0 + max(0.0, float(getattr(cycle, "slippage_buffer_pct", 0.0) or 0.0)) / 100.0
    return round_price(sizing)


def _protective_sell_is_working(cycle: CycleState) -> bool:
    ref = getattr(cycle, "protective_sell_order_ref", None)
    if not ref:
        return False
    status = str(getattr(cycle, "protective_sell_status", "") or "")
    if status in TERMINAL_ORDER_STATUSES:
        return False
    return not (getattr(cycle, "protective_sell_filled_qty", 0) > 0)


def _protective_sell_stop_price(cycle: CycleState) -> float:
    if cycle.avg_buy_price is None or cycle.avg_buy_price <= 0:
        return 0.0
    pct = max(0.01, float(getattr(cycle, "protective_sell_trailing_stop_pct", 0.0) or 0.0))
    return round_price(float(cycle.avg_buy_price) * (1.0 - pct / 100.0))


class StrategyEngine:
    """Pure strategy-state logic. Broker calls are represented as returned actions."""

    @staticmethod
    def start_cycle(settings: StrategySettings, cycle_number: int, account: str, last_price: float, realized_net_profit: float) -> CycleState:
        """Create a new cycle when a usable market price is already available.

        The first price becomes the initial anchor. While Stage 1 is waiting for
        the initial drop, later higher prices can reset this anchor upward.
        """
        if last_price <= 0:
            raise ValueError("Last price must be greater than zero to start a cycle.")
        ticker = settings.normalized_ticker()
        if not ticker:
            raise ValueError("Ticker is required.")
        usable_profit = max(realized_net_profit, 0.0) if settings.reinvest_profits else 0.0
        cycle = CycleState.new(settings=settings, cycle_number=cycle_number, account=account, last_price=float(last_price), reinvested_profit=usable_profit)
        cycle.touch()
        return cycle

    @staticmethod
    def start_cycle_waiting_for_price(settings: StrategySettings, cycle_number: int, account: str, realized_net_profit: float) -> CycleState:
        """Create a cycle before TWS has supplied the first usable price.

        This avoids hard-failing illiquid or delayed-data tickers. The first
        valid price received in on_price_update will set the anchor.
        """
        ticker = settings.normalized_ticker()
        if not ticker:
            raise ValueError("Ticker is required.")
        usable_profit = max(realized_net_profit, 0.0) if settings.reinvest_profits else 0.0
        cycle = CycleState.new_pending(settings=settings, cycle_number=cycle_number, account=account, reinvested_profit=usable_profit)
        cycle.touch()
        return cycle

    @staticmethod
    def pause_initial_drop_until_ready(cycle: CycleState, last_price: float, message: str) -> CycleState:
        """Pause Stage 1 without carrying a pre-warmup drop into live entry logic.

        ATR warm-up is different from an ordinary order-submission guard: the
        adaptive entry percentages are not ready yet. While that guard is active,
        each usable price becomes only the latest reference point. No initial-drop
        trigger is calculated or evaluated, so a price move observed during
        warm-up cannot arm a later BUY.
        """
        if last_price <= 0:
            return cycle
        next_cycle = copy(cycle)
        next_cycle.stage = Stage.WAIT_INITIAL_DROP
        next_cycle.last_price = float(last_price)
        next_cycle.anchor_price = float(last_price)
        next_cycle.drop_trigger_price = None
        next_cycle.quantity = 0
        next_cycle.buy_initial_trail_stop_price = None
        next_cycle.buy_order_ref = None
        next_cycle.buy_order_id = None
        next_cycle.buy_perm_id = None
        next_cycle.buy_status = None
        next_cycle.error_message = str(message)
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def restart_initial_drop_from_price(cycle: CycleState, last_price: float) -> CycleState:
        """Start Stage 1 from a fresh anchor after ATR warm-up clears.

        The readiness tick establishes the new anchor but deliberately does not
        evaluate the initial-drop condition. A later price update must create the
        configured drop from this post-warm-up reference.
        """
        if last_price <= 0:
            return cycle
        next_cycle = copy(cycle)
        next_cycle.stage = Stage.WAIT_INITIAL_DROP
        next_cycle.last_price = float(last_price)
        next_cycle.anchor_price = float(last_price)
        next_cycle.drop_trigger_price = round_price(
            float(last_price) * (1.0 - float(next_cycle.initial_drop_pct) / 100.0)
        )
        next_cycle.quantity = 0
        next_cycle.buy_initial_trail_stop_price = None
        next_cycle.buy_order_ref = None
        next_cycle.buy_order_id = None
        next_cycle.buy_perm_id = None
        next_cycle.buy_status = None
        next_cycle.error_message = None
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def on_price_update(cycle: CycleState, last_price: float, *, is_rth: bool = True, rth_message: str = "") -> tuple[CycleState, list[StrategyAction]]:
        """Advance a cycle from a new price tick.

        Returned StrategyAction objects are requests, not broker side effects.
        The controller decides whether and how to transmit them to IBKR.
        """
        if last_price <= 0:
            return cycle, []

        next_cycle = copy(cycle)
        next_cycle.last_price = float(last_price)
        actions: list[StrategyAction] = []
        rth_blocked = bool(getattr(next_cycle, "rth_only", True)) and not bool(is_rth)
        if not rth_blocked and next_cycle.error_message and str(next_cycle.error_message).startswith("RTH guard:"):
            next_cycle.error_message = None

        if next_cycle.stage == Stage.WAIT_INITIAL_DROP:
            anchor = next_cycle.anchor_price if next_cycle.anchor_price is not None else float(last_price)
            if last_price > anchor:
                # User confirmed: reset anchor upward before initial drop is reached.
                anchor = float(last_price)
            next_cycle.anchor_price = anchor
            next_cycle.drop_trigger_price = round_price(anchor * (1.0 - next_cycle.initial_drop_pct / 100.0))

            if last_price <= next_cycle.drop_trigger_price:
                if rth_blocked:
                    detail = rth_message or "regular trading hours are closed"
                    next_cycle.error_message = f"RTH guard: initial drop condition is met, but no BUY order will be submitted until RTH is open ({detail})."
                else:
                    buy_trail_pct = max(0.0, float(next_cycle.buy_rebound_trail_pct or 0.0))
                    # A 0% BUY rebound/trail disables trailing for the buy leg. In
                    # that mode the bot buys with a market order as soon as the
                    # initial-drop condition is met. Otherwise it submits the native
                    # IBKR BUY TRAIL order used by the normal strategy.
                    initial_buy_stop = round_price(float(last_price) * (1.0 + buy_trail_pct / 100.0))
                    # Size against the BUY trigger/reference price, not the lower
                    # drop price. This reduces the chance that the planned notional
                    # exceeds the configured budget when the order fills. The final
                    # fill is still a market-style fill and can exceed the reference
                    # in gaps or fast markets.
                    sizing_price = _buy_sizing_price(next_cycle, initial_buy_stop)
                    qty = floor(next_cycle.budget / sizing_price) if sizing_price > 0 else 0
                    next_cycle.quantity = int(qty)
                    if qty <= 0:
                        next_cycle.stage = Stage.ERROR
                        next_cycle.error_message = "Calculated quantity is zero. Investment amount is too low for the projected BUY reference/slippage sizing price."
                    else:
                        next_cycle.buy_initial_trail_stop_price = initial_buy_stop
                        if buy_trail_pct <= 0:
                            next_cycle.buy_order_ref = make_order_ref(next_cycle.ticker, next_cycle.cycle_number, next_cycle.id, "BUY_MARKET")
                            actions.append(
                                StrategyAction(
                                    "PLACE_BUY_MARKET",
                                    {
                                        "ticker": next_cycle.ticker,
                                        "quantity": int(qty),
                                        "order_type": "MKT",
                                        "trailing_percent": 0.0,
                                        "initial_stop_price": None,
                                        "reference_price": initial_buy_stop,
                                        "sizing_price": sizing_price,
                                        "slippage_buffer_enabled": bool(getattr(next_cycle, "slippage_buffer_enabled", False)),
                                        "slippage_buffer_pct": float(getattr(next_cycle, "slippage_buffer_pct", 0.0) or 0.0),
                                        "budget": next_cycle.budget,
                                        "order_ref": next_cycle.buy_order_ref,
                                    },
                                )
                            )
                        else:
                            next_cycle.buy_order_ref = make_order_ref(next_cycle.ticker, next_cycle.cycle_number, next_cycle.id, "BUY_TRAIL")
                            actions.append(
                                StrategyAction(
                                    "PLACE_BUY_TRAIL",
                                    {
                                        "ticker": next_cycle.ticker,
                                        "quantity": int(qty),
                                        "order_type": "TRAIL",
                                        "trailing_percent": buy_trail_pct,
                                        "initial_stop_price": initial_buy_stop,
                                        "reference_price": initial_buy_stop,
                                        "sizing_price": sizing_price,
                                        "slippage_buffer_enabled": bool(getattr(next_cycle, "slippage_buffer_enabled", False)),
                                        "slippage_buffer_pct": float(getattr(next_cycle, "slippage_buffer_pct", 0.0) or 0.0),
                                        "budget": next_cycle.budget,
                                        "order_ref": next_cycle.buy_order_ref,
                                    },
                                )
                            )
                        next_cycle.stage = Stage.BUY_TRAIL_ACTIVE

        elif next_cycle.stage == Stage.WAIT_RISE_TRIGGER:
            if next_cycle.avg_buy_price is None or next_cycle.buy_filled_qty <= 0:
                next_cycle.stage = Stage.MANUAL_REVIEW
                next_cycle.error_message = "Missing buy fill data while waiting for minimum-profit trigger."
            else:
                next_cycle.rise_trigger_price = _safe_rise_trigger_price(next_cycle)
                if last_price >= next_cycle.rise_trigger_price:
                    if rth_blocked:
                        detail = rth_message or "regular trading hours are closed"
                        next_cycle.error_message = f"RTH guard: minimum-profit condition is met, but no SELL order will be submitted until RTH is open ({detail})."
                    else:
                        sell_trail_pct = max(0.0, float(next_cycle.sell_trailing_stop_pct or 0.0))
                        initial_sell_stop = round_price(float(last_price) * (1.0 - sell_trail_pct / 100.0))
                        minimum_stop = _minimum_profit_stop_price(next_cycle)
                        if initial_sell_stop + 1e-9 < minimum_stop:
                            next_cycle.stage = Stage.WAIT_RISE_TRIGGER
                            next_cycle.error_message = (
                                "SELL order not placed because the initial stop/reference would not protect the configured minimum profit. "
                                "The app will wait for a higher price."
                            )
                        elif _protective_sell_is_working(next_cycle):
                            if not bool(getattr(next_cycle, "protective_sell_cancel_requested", False)):
                                next_cycle.protective_sell_cancel_requested = True
                                next_cycle.protective_sell_status = next_cycle.protective_sell_status or "CancelRequested"
                                next_cycle.error_message = "Minimum-profit condition reached. Cancelling protective SELL trail before placing the profit-protecting SELL order."
                                actions.append(
                                    StrategyAction(
                                        "CANCEL_ORDER",
                                        {
                                            "order_ref": next_cycle.protective_sell_order_ref,
                                            "order_id": next_cycle.protective_sell_order_id,
                                            "role": "protective_sell",
                                            "reason": "Minimum-profit condition reached; cancelling protective SELL trail before submitting the profit-protecting SELL order.",
                                        },
                                    )
                                )
                            else:
                                # Wait for controller/TWS to report Cancelled/ApiCancelled/Inactive before
                                # placing the final SELL. This prevents two SELL orders from being
                                # working at the same time during cancel/replace.
                                next_cycle.error_message = "Waiting for protective SELL cancellation confirmation before placing the profit-protecting SELL order."
                        else:
                            next_cycle.sell_initial_trail_stop_price = None if sell_trail_pct <= 0 else initial_sell_stop
                            if sell_trail_pct <= 0:
                                next_cycle.sell_order_ref = make_order_ref(next_cycle.ticker, next_cycle.cycle_number, next_cycle.id, "SELL_MARKET")
                                actions.append(
                                    StrategyAction(
                                        "PLACE_SELL_MARKET",
                                        {
                                            "ticker": next_cycle.ticker,
                                            "quantity": int(next_cycle.buy_filled_qty),
                                            "order_type": "MKT",
                                            "trailing_percent": 0.0,
                                            "initial_stop_price": None,
                                            "reference_price": float(last_price),
                                            "minimum_stop_price": minimum_stop,
                                            "order_ref": next_cycle.sell_order_ref,
                                        },
                                    )
                                )
                            else:
                                next_cycle.sell_order_ref = make_order_ref(next_cycle.ticker, next_cycle.cycle_number, next_cycle.id, "SELL_TRAIL")
                                actions.append(
                                    StrategyAction(
                                        "PLACE_SELL_TRAIL",
                                        {
                                            "ticker": next_cycle.ticker,
                                            "quantity": int(next_cycle.buy_filled_qty),
                                            "order_type": "TRAIL",
                                            "trailing_percent": sell_trail_pct,
                                            "initial_stop_price": initial_sell_stop,
                                            "reference_price": float(last_price),
                                            "order_ref": next_cycle.sell_order_ref,
                                        },
                                    )
                                )
                            next_cycle.stage = Stage.SELL_TRAIL_ACTIVE

        next_cycle.touch()
        return next_cycle, actions

    @staticmethod
    def on_order_submitted(cycle: CycleState, order_ref: str, order_id: Optional[int], perm_id: Optional[int], status: str) -> CycleState:
        next_cycle = copy(cycle)
        if order_ref == next_cycle.buy_order_ref:
            next_cycle.buy_order_id = order_id
            next_cycle.buy_perm_id = perm_id
            next_cycle.buy_status = status
            next_cycle.stage = Stage.BUY_TRAIL_ACTIVE
        elif order_ref == next_cycle.protective_sell_order_ref:
            next_cycle.protective_sell_order_id = order_id
            next_cycle.protective_sell_perm_id = perm_id
            next_cycle.protective_sell_status = status
            # Protective SELL works while the strategy remains in Stage 3.
            next_cycle.stage = Stage.WAIT_RISE_TRIGGER
        elif order_ref == next_cycle.sell_order_ref:
            next_cycle.sell_order_id = order_id
            next_cycle.sell_perm_id = perm_id
            next_cycle.sell_status = status
            next_cycle.stage = Stage.SELL_TRAIL_ACTIVE
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def on_buy_fill(cycle: CycleState, filled_qty: int, avg_fill_price: float, status: str, commission: float = 0.0) -> tuple[CycleState, list[StrategyAction]]:
        """Handle any positive BUY fill.

        Per requirement, the strategy works with whatever quantity filled and
        asks the controller to cancel any still-open remainder.
        """
        next_cycle = copy(cycle)
        actions: list[StrategyAction] = []
        if filled_qty <= 0 or avg_fill_price <= 0:
            next_cycle.buy_status = status
            next_cycle.touch()
            return next_cycle, actions

        next_cycle.buy_filled_qty = int(filled_qty)
        next_cycle.avg_buy_price = float(avg_fill_price)
        next_cycle.buy_status = status
        next_cycle.buy_commission = float(commission or next_cycle.buy_commission or 0.0)
        next_cycle.buy_filled_at = next_cycle.buy_filled_at or utc_now_iso()
        next_cycle.rise_trigger_price = _safe_rise_trigger_price(next_cycle)

        if next_cycle.buy_order_ref and next_cycle.quantity > filled_qty and status not in TERMINAL_ORDER_STATUSES:
            actions.append(
                StrategyAction(
                    "CANCEL_ORDER",
                    {
                        "order_ref": next_cycle.buy_order_ref,
                        "order_id": next_cycle.buy_order_id,
                        "role": "buy_remainder",
                        "reason": "Partial buy fill received; cancelling unfilled remainder per strategy settings.",
                    },
                )
            )
        next_cycle.stage = Stage.WAIT_RISE_TRIGGER
        if bool(getattr(next_cycle, "protective_sell_enabled", False)):
            stop_price = _protective_sell_stop_price(next_cycle)
            if stop_price > 0:
                next_cycle.protective_sell_initial_stop_price = stop_price
                next_cycle.protective_sell_order_ref = make_order_ref(next_cycle.ticker, next_cycle.cycle_number, next_cycle.id, "PROTECTIVE_SELL_TRAIL")
                next_cycle.protective_sell_cancel_requested = False
                actions.append(
                    StrategyAction(
                        "PLACE_PROTECTIVE_SELL_TRAIL",
                        {
                            "ticker": next_cycle.ticker,
                            "quantity": int(next_cycle.buy_filled_qty),
                            "trailing_percent": next_cycle.protective_sell_trailing_stop_pct,
                            "initial_stop_price": stop_price,
                            "order_ref": next_cycle.protective_sell_order_ref,
                        },
                    )
                )
        next_cycle.touch()
        return next_cycle, actions

    @staticmethod
    def on_protective_sell_fill(cycle: CycleState, filled_qty: int, avg_fill_price: float, status: str, commission: float = 0.0) -> CycleState:
        """Handle a protective SELL trailing-stop fill.

        A protective fill exits the position before the minimum-profit condition
        was reached. The cycle is still completed and P/L is recorded so hard
        risk limits and history can see the loss or small gain.
        """
        next_cycle = copy(cycle)
        if filled_qty <= 0 or avg_fill_price <= 0:
            next_cycle.protective_sell_status = status
            next_cycle.touch()
            return next_cycle
        next_cycle.protective_sell_filled_qty = int(filled_qty)
        next_cycle.protective_avg_sell_price = float(avg_fill_price)
        next_cycle.protective_sell_status = status
        next_cycle.protective_sell_commission = float(commission or next_cycle.protective_sell_commission or 0.0)
        next_cycle.protective_sell_filled_at = next_cycle.protective_sell_filled_at or utc_now_iso()
        # Reuse final sell fields for history/P&L because the position is closed.
        next_cycle.sell_order_ref = next_cycle.sell_order_ref or next_cycle.protective_sell_order_ref
        next_cycle.sell_order_id = next_cycle.sell_order_id or next_cycle.protective_sell_order_id
        next_cycle.sell_perm_id = next_cycle.sell_perm_id or next_cycle.protective_sell_perm_id
        next_cycle.sell_status = status
        next_cycle.sell_filled_qty = int(filled_qty)
        next_cycle.avg_sell_price = float(avg_fill_price)
        next_cycle.sell_commission = float(commission or next_cycle.sell_commission or 0.0)
        next_cycle.sell_filled_at = next_cycle.sell_filled_at or next_cycle.protective_sell_filled_at
        qty = min(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)
        if next_cycle.avg_buy_price is not None:
            next_cycle.gross_pnl = (next_cycle.avg_sell_price - next_cycle.avg_buy_price) * qty
            next_cycle.net_pnl = next_cycle.gross_pnl - next_cycle.buy_commission - next_cycle.sell_commission
        next_cycle.stage = Stage.CYCLE_COMPLETE
        next_cycle.close_position_market_requested = False
        next_cycle.close_before_rth_liquidation_requested = False
        next_cycle.close_before_rth_cancel_requested = False
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def on_sell_fill(cycle: CycleState, filled_qty: int, avg_fill_price: float, status: str, commission: float = 0.0) -> CycleState:
        """Handle a SELL fill and finalize P/L for the cycle.

        P/L is based on the overlapping bought/sold share count. This keeps the
        calculation conservative if IBKR reports a partial sell before the full
        order has completed.
        """
        next_cycle = copy(cycle)
        if filled_qty <= 0 or avg_fill_price <= 0:
            next_cycle.sell_status = status
            next_cycle.touch()
            return next_cycle

        next_cycle.sell_filled_qty = int(filled_qty)
        next_cycle.avg_sell_price = float(avg_fill_price)
        next_cycle.sell_status = status
        next_cycle.sell_commission = float(commission or next_cycle.sell_commission or 0.0)
        next_cycle.sell_filled_at = next_cycle.sell_filled_at or utc_now_iso()
        qty = min(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)
        if next_cycle.avg_buy_price is not None:
            next_cycle.gross_pnl = (next_cycle.avg_sell_price - next_cycle.avg_buy_price) * qty
            next_cycle.net_pnl = next_cycle.gross_pnl - next_cycle.buy_commission - next_cycle.sell_commission
        next_cycle.stage = Stage.CYCLE_COMPLETE
        next_cycle.close_position_market_requested = False
        next_cycle.close_before_rth_liquidation_requested = False
        next_cycle.close_before_rth_cancel_requested = False
        next_cycle.touch()
        return next_cycle


    @staticmethod
    def apply_editable_settings(
        cycle: CycleState,
        settings: StrategySettings,
        realized_net_profit: float = 0.0,
    ) -> tuple[CycleState, list[str]]:
        """Apply safe mid-cycle edits to the active cycle.

        Immutable while a cycle is active: ticker, contract identity, exchange,
        currency, and any parameters already transmitted to an active native
        TWS order. Editable fields are applied only where they can still affect
        a future decision without modifying an existing order.
        """
        next_cycle = copy(cycle)
        changed: list[str] = []
        stage = next_cycle.stage

        def update(name: str, value: object, label: str) -> None:
            if getattr(next_cycle, name) != value:
                setattr(next_cycle, name, value)
                changed.append(label)

        if stage == Stage.WAIT_INITIAL_DROP:
            reinvested = max(float(realized_net_profit or 0.0), 0.0) if settings.reinvest_profits else 0.0
            update("investment_amount", float(settings.investment_amount), "investment amount")
            update("reinvest_profits", bool(settings.reinvest_profits), "reinvest profits")
            update("reinvested_profit", float(reinvested), "reinvested profit")
            update("budget", float(settings.investment_amount + reinvested), "cycle budget")
            update("initial_drop_pct", float(settings.initial_drop_pct), "initial drop %")
            update("buy_rebound_trail_pct", float(settings.buy_rebound_trail_pct), "BUY rebound/trail %")
            update("rise_trigger_pct", float(settings.rise_trigger_pct), "minimum profit %")
            update("sell_trailing_stop_pct", float(settings.sell_trailing_stop_pct), "SELL trailing-stop %")
            update("atr_adaptive_enabled", bool(settings.atr_adaptive_enabled), "ATR adaptive mode")
            update("atr_adapt_minimum_profit_enabled", bool(getattr(settings, "atr_adapt_minimum_profit_enabled", True)), "ATR adapts minimum profit")
            update("atr_block_new_buy_until_ready", bool(getattr(settings, "atr_block_new_buy_until_ready", True)), "ATR warmup BUY block")
            update("atr_adapt_protective_sell_enabled", bool(getattr(settings, "atr_adapt_protective_sell_enabled", False)), "ATR adapts protective SELL")
            update("atr_protective_sell_multiplier", float(getattr(settings, "atr_protective_sell_multiplier", 3.0)), "ATR protective SELL multiplier")
            update("atr_period", int(settings.atr_period), "ATR period")
            update("atr_bar_seconds", int(settings.atr_bar_seconds), "ATR bar size")
            update("atr_initial_drop_multiplier", float(settings.atr_initial_drop_multiplier), "ATR initial-drop multiplier")
            update("atr_buy_rebound_multiplier", float(settings.atr_buy_rebound_multiplier), "ATR buy-rebound multiplier")
            update("atr_minimum_profit_multiplier", float(settings.atr_minimum_profit_multiplier), "ATR minimum-profit multiplier")
            update("atr_sell_trail_multiplier", float(settings.atr_sell_trail_multiplier), "ATR sell-trail multiplier")
            update("atr_min_pct", float(settings.atr_min_pct), "ATR min %")
            update("atr_max_pct", float(settings.atr_max_pct), "ATR max %")
            update("protective_sell_enabled", bool(settings.protective_sell_enabled), "protective sell enabled")
            update("protective_sell_trailing_stop_pct", float(settings.protective_sell_trailing_stop_pct), "protective sell trail %")
            update("slippage_buffer_enabled", bool(settings.slippage_buffer_enabled), "slippage buffer enabled")
            update("slippage_buffer_pct", float(settings.slippage_buffer_pct), "slippage buffer %")
            update("hard_risk_limits_enabled", bool(settings.hard_risk_limits_enabled), "hard risk limits enabled")
            update("max_daily_loss_ticker", float(settings.max_daily_loss_ticker), "max daily loss ticker")
            update("max_daily_loss_total", float(settings.max_daily_loss_total), "max daily loss total")
            update("max_cycles_per_ticker_day", int(settings.max_cycles_per_ticker_day), "max cycles")
            update("max_consecutive_losses", int(settings.max_consecutive_losses), "max consecutive losses")
            update("max_spread_pct", float(settings.max_spread_pct), "max spread %")
            update("min_trade_price", float(settings.min_trade_price), "minimum trade price")
            update("max_gap_from_prev_close_pct", float(settings.max_gap_from_prev_close_pct), "max gap from close %")
            update("block_delayed_data_in_live", bool(settings.block_delayed_data_in_live), "block delayed data in live")
            update("what_if_check_enabled", bool(settings.what_if_check_enabled), "what-if margin check")
            update("stale_data_guard_enabled", bool(settings.stale_data_guard_enabled), "stale-data guard")
            update("max_selected_price_age_seconds", float(settings.max_selected_price_age_seconds), "max selected-price age")
            update("max_bid_ask_age_seconds", float(settings.max_bid_ask_age_seconds), "max bid/ask age")
            update("max_rth_status_age_seconds", float(settings.max_rth_status_age_seconds), "max RTH-status age")
            update("volatility_filter_enabled", bool(settings.volatility_filter_enabled), "volatility filter")
            update("volatility_window_seconds", int(settings.volatility_window_seconds), "volatility window")
            update("max_recent_price_move_pct", float(settings.max_recent_price_move_pct), "max recent move %")
            update("session_timing_guard_enabled", bool(settings.session_timing_guard_enabled), "session timing guard")
            update("no_new_buy_first_minutes", int(settings.no_new_buy_first_minutes), "no-new-buy first minutes")
            update("no_new_buy_last_minutes", int(settings.no_new_buy_last_minutes), "no-new-buy last minutes")
            update("cancel_buy_before_close_minutes", int(settings.cancel_buy_before_close_minutes), "cancel BUY before close minutes")
            update(
                "cancel_sell_and_liquidate_before_close_enabled",
                bool(settings.cancel_sell_and_liquidate_before_close_enabled),
                "cancel SELL trail and liquidate before close",
            )
            update(
                "liquidate_before_close_minutes",
                int(settings.liquidate_before_close_minutes),
                "liquidate before close minutes",
            )
            if next_cycle.anchor_price is not None and next_cycle.anchor_price > 0:
                next_cycle.drop_trigger_price = round_price(next_cycle.anchor_price * (1.0 - next_cycle.initial_drop_pct / 100.0))
            next_cycle.quantity = 0
            next_cycle.buy_initial_trail_stop_price = None

        elif stage == Stage.BUY_TRAIL_ACTIVE:
            # BUY-side order parameters are locked because a native TWS BUY
            # trailing-stop is already working. Exit settings have not yet been
            # transmitted, so they can still be changed.
            update("rise_trigger_pct", float(settings.rise_trigger_pct), "minimum profit %")
            update("sell_trailing_stop_pct", float(settings.sell_trailing_stop_pct), "SELL trailing-stop %")
            update("atr_adaptive_enabled", bool(settings.atr_adaptive_enabled), "ATR adaptive mode")
            update("atr_adapt_minimum_profit_enabled", bool(getattr(settings, "atr_adapt_minimum_profit_enabled", True)), "ATR adapts minimum profit")
            update("atr_block_new_buy_until_ready", bool(getattr(settings, "atr_block_new_buy_until_ready", True)), "ATR warmup BUY block")
            update("atr_adapt_protective_sell_enabled", bool(getattr(settings, "atr_adapt_protective_sell_enabled", False)), "ATR adapts protective SELL")
            update("atr_protective_sell_multiplier", float(getattr(settings, "atr_protective_sell_multiplier", 3.0)), "ATR protective SELL multiplier")
            update("atr_period", int(settings.atr_period), "ATR period")
            update("atr_bar_seconds", int(settings.atr_bar_seconds), "ATR bar size")
            update("atr_initial_drop_multiplier", float(settings.atr_initial_drop_multiplier), "ATR initial-drop multiplier")
            update("atr_buy_rebound_multiplier", float(settings.atr_buy_rebound_multiplier), "ATR buy-rebound multiplier")
            update("atr_minimum_profit_multiplier", float(settings.atr_minimum_profit_multiplier), "ATR minimum-profit multiplier")
            update("atr_sell_trail_multiplier", float(settings.atr_sell_trail_multiplier), "ATR sell-trail multiplier")
            update("atr_min_pct", float(settings.atr_min_pct), "ATR min %")
            update("atr_max_pct", float(settings.atr_max_pct), "ATR max %")
            update("protective_sell_enabled", bool(settings.protective_sell_enabled), "protective sell enabled")
            update("protective_sell_trailing_stop_pct", float(settings.protective_sell_trailing_stop_pct), "protective sell trail %")
            update("slippage_buffer_enabled", bool(settings.slippage_buffer_enabled), "slippage buffer enabled")
            update("slippage_buffer_pct", float(settings.slippage_buffer_pct), "slippage buffer %")
            update(
                "cancel_sell_and_liquidate_before_close_enabled",
                bool(settings.cancel_sell_and_liquidate_before_close_enabled),
                "cancel SELL trail and liquidate before close",
            )
            update(
                "liquidate_before_close_minutes",
                int(settings.liquidate_before_close_minutes),
                "liquidate before close minutes",
            )

        elif stage == Stage.WAIT_RISE_TRIGGER:
            # Position is open; only the future SELL-trigger decision can be
            # safely changed without cancelling/replacing an active order.
            update("rise_trigger_pct", float(settings.rise_trigger_pct), "minimum profit %")
            update("sell_trailing_stop_pct", float(settings.sell_trailing_stop_pct), "SELL trailing-stop %")
            update("atr_adaptive_enabled", bool(settings.atr_adaptive_enabled), "ATR adaptive mode")
            update("atr_adapt_minimum_profit_enabled", bool(getattr(settings, "atr_adapt_minimum_profit_enabled", True)), "ATR adapts minimum profit")
            update("atr_block_new_buy_until_ready", bool(getattr(settings, "atr_block_new_buy_until_ready", True)), "ATR warmup BUY block")
            update("atr_adapt_protective_sell_enabled", bool(getattr(settings, "atr_adapt_protective_sell_enabled", False)), "ATR adapts protective SELL")
            update("atr_protective_sell_multiplier", float(getattr(settings, "atr_protective_sell_multiplier", 3.0)), "ATR protective SELL multiplier")
            update("atr_period", int(settings.atr_period), "ATR period")
            update("atr_bar_seconds", int(settings.atr_bar_seconds), "ATR bar size")
            update("atr_minimum_profit_multiplier", float(settings.atr_minimum_profit_multiplier), "ATR minimum-profit multiplier")
            update("atr_sell_trail_multiplier", float(settings.atr_sell_trail_multiplier), "ATR sell-trail multiplier")
            update("atr_min_pct", float(settings.atr_min_pct), "ATR min %")
            update("atr_max_pct", float(settings.atr_max_pct), "ATR max %")
            update("slippage_buffer_enabled", bool(settings.slippage_buffer_enabled), "slippage buffer enabled")
            update("slippage_buffer_pct", float(settings.slippage_buffer_pct), "slippage buffer %")
            update(
                "cancel_sell_and_liquidate_before_close_enabled",
                bool(settings.cancel_sell_and_liquidate_before_close_enabled),
                "cancel SELL trail and liquidate before close",
            )
            update(
                "liquidate_before_close_minutes",
                int(settings.liquidate_before_close_minutes),
                "liquidate before close minutes",
            )
            # Protective parameters are locked once the protective order exists;
            # otherwise they can still be applied before the BUY fill action places it.
            if not _protective_sell_is_working(next_cycle) and not next_cycle.protective_sell_order_ref:
                update("protective_sell_enabled", bool(settings.protective_sell_enabled), "protective sell enabled")
                update("protective_sell_trailing_stop_pct", float(settings.protective_sell_trailing_stop_pct), "protective sell trail %")
            next_cycle.rise_trigger_price = _safe_rise_trigger_price(next_cycle)

        # Stage.SELL_TRAIL_ACTIVE is locked: the SELL trailing-stop has already
        # been transmitted to TWS. Changes are saved as drafts but are not applied
        # to the active native order.
        if changed:
            next_cycle.touch()
        return next_cycle, changed

    @staticmethod
    def rollback_unsubmitted_order(cycle: CycleState, side: str, message: str) -> CycleState:
        """Return to the pre-submit waiting stage when a broker call failed before an order was accepted."""
        next_cycle = copy(cycle)
        side = side.upper().strip()
        if side == "BUY":
            next_cycle.stage = Stage.WAIT_INITIAL_DROP
            next_cycle.buy_order_ref = None
            next_cycle.buy_order_id = None
            next_cycle.buy_perm_id = None
            next_cycle.buy_status = "SubmitFailed"
            next_cycle.quantity = 0
            next_cycle.buy_initial_trail_stop_price = None
        elif side == "PROTECTIVE_SELL":
            next_cycle.stage = Stage.WAIT_RISE_TRIGGER
            next_cycle.protective_sell_order_ref = None
            next_cycle.protective_sell_order_id = None
            next_cycle.protective_sell_perm_id = None
            next_cycle.protective_sell_status = "SubmitFailed"
            next_cycle.protective_sell_initial_stop_price = None
            next_cycle.protective_sell_cancel_requested = False
        elif side == "SELL":
            next_cycle.stage = Stage.WAIT_RISE_TRIGGER
            next_cycle.sell_order_ref = None
            next_cycle.sell_order_id = None
            next_cycle.sell_perm_id = None
            next_cycle.sell_status = "SubmitFailed"
            next_cycle.sell_initial_trail_stop_price = None
        next_cycle.error_message = message
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def mark_error(cycle: CycleState, message: str) -> CycleState:
        next_cycle = copy(cycle)
        next_cycle.stage = Stage.ERROR
        next_cycle.error_message = message
        next_cycle.touch()
        return next_cycle

    @staticmethod
    def set_stop_after_current_cycle(cycle: CycleState) -> CycleState:
        next_cycle = copy(cycle)
        next_cycle.stop_after_current_cycle = True
        next_cycle.touch()
        return next_cycle
