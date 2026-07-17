"""Small deterministic market simulator for strategy tests.

The production app relies on native IBKR trailing-stop orders after the initial
conditions are met. Unit tests cannot ask IBKR to trail an order, so this module
implements the minimum behavior needed for repeatable simulations:

* a BUY trailing stop tracks the lowest price after submission and fills when
  price rebounds by the configured trail percentage;
* a SELL trailing stop tracks the highest price after submission and fills when
  price falls by the configured trail percentage;
* the optional protective SELL trail can fill before the final minimum-profit
  SELL trail is submitted.

The simulator is intentionally simple. A supplied tick that crosses a stop is
used as the deterministic fill observation, including for explicit gap paths. It
does not model order-book depth, gap distributions, exchange queueing, arbitrary
partial exchange fills, or trading halts.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.models import CycleState, Stage, StrategyAction, StrategySettings
from app.strategy import StrategyEngine, round_price


@dataclass(slots=True)
class SimulatedEvent:
    """One event emitted by the deterministic strategy simulator."""

    kind: str
    price: float
    message: str
    stage: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class WorkingTrailOrder:
    """Minimal native trailing-stop stand-in used only by tests/simulations."""

    side: str
    quantity: int
    trailing_percent: float
    order_ref: str
    stop_price: float
    extreme_price: float

    def update(self, price: float) -> bool:
        """Update the simulated trail and return True when it triggers."""
        trail = self.trailing_percent / 100.0
        if self.side == "BUY":
            # A BUY trailing stop follows the market downward. A rebound through
            # the stop triggers the market buy.
            if price < self.extreme_price:
                self.extreme_price = price
                self.stop_price = round_price(price * (1.0 + trail))
            return price >= self.stop_price

        if self.side == "SELL":
            # A SELL trailing stop follows the market upward. A fall back through
            # the stop triggers the market sell.
            if price > self.extreme_price:
                self.extreme_price = price
                self.stop_price = round_price(price * (1.0 - trail))
            return price <= self.stop_price
        raise ValueError(f"Unsupported side: {self.side}")


@dataclass(slots=True)
class SimulationResult:
    """Final state and event log from a simulated one-cycle run."""

    cycle: CycleState
    events: list[SimulatedEvent]

    def event_kinds(self) -> list[str]:
        return [event.kind for event in self.events]


def load_prices_csv(path: str | Path) -> list[float]:
    """Read a CSV containing a required ``price`` column."""
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return [float(row["price"]) for row in rows]


def load_price_rows_csv(path: str | Path) -> list[dict[str, object]]:
    """Read richer simulation rows.

    Supported optional columns:
        rth_open: 1/0, true/false, yes/no
        buy_slippage_pct: per-tick simulated BUY fill slippage override
        sell_slippage_pct: per-tick simulated SELL fill slippage override
    """
    result: list[dict[str, object]] = []
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        for row in rows:
            text = str(row.get("rth_open", "1")).strip().lower()
            result.append({
                "price": float(row["price"]),
                "rth_open": text not in {"0", "false", "no", "closed"},
                "buy_slippage_pct": float(row.get("buy_slippage_pct") or 0.0),
                "sell_slippage_pct": float(row.get("sell_slippage_pct") or 0.0),
                "note": row.get("note", ""),
            })
    return result


def _new_trail_order(action: StrategyAction, price: float, side: str) -> WorkingTrailOrder:
    payload = action.payload
    return WorkingTrailOrder(
        side=side,
        quantity=int(payload["quantity"]),
        trailing_percent=float(payload["trailing_percent"]),
        order_ref=str(payload["order_ref"]),
        stop_price=float(payload["initial_stop_price"]),
        extreme_price=float(price),
    )


def run_one_cycle(
    settings: StrategySettings,
    prices: Iterable[float] | Iterable[dict[str, object]],
    *,
    account: str = "DU-SIM",
    realized_net_profit: float = 0.0,
    rth_open: bool = True,
    partial_buy_ratio: float = 1.0,
    buy_slippage_pct: float = 0.0,
    sell_slippage_pct: float = 0.0,
) -> SimulationResult:
    """Run a deterministic one-cycle simulation over a price series.

    ``prices`` may be plain floats or dictionaries from ``load_price_rows_csv``.
    The richer row format lets tests exercise per-tick RTH state and slippage.
    Native trailing orders are simulated locally; live trading still delegates
    trailing behavior to TWS/IBKR.
    """
    cycle = StrategyEngine.start_cycle_waiting_for_price(settings, 1, account, realized_net_profit)
    events: list[SimulatedEvent] = []
    buy_trail: WorkingTrailOrder | None = None
    sell_trail: WorkingTrailOrder | None = None
    sell_trail_role = "final"

    for row in prices:
        if isinstance(row, dict):
            price = float(row["price"])
            tick_rth_open = bool(row.get("rth_open", rth_open))
            tick_buy_slippage_pct = float(row.get("buy_slippage_pct", buy_slippage_pct) or buy_slippage_pct)
            tick_sell_slippage_pct = float(row.get("sell_slippage_pct", sell_slippage_pct) or sell_slippage_pct)
        else:
            price = float(row)
            tick_rth_open = bool(rth_open)
            tick_buy_slippage_pct = float(buy_slippage_pct)
            tick_sell_slippage_pct = float(sell_slippage_pct)

        # A protective SELL is active while the strategy is waiting for the final
        # minimum-profit trigger. Simulate that broker-side order before checking
        # whether a new app-side SELL order should be placed.
        if cycle.stage == Stage.WAIT_RISE_TRIGGER and sell_trail is not None and sell_trail_role == "protective":
            if sell_trail.update(price):
                fill_price = round_price(price * (1.0 - tick_sell_slippage_pct / 100.0))
                cycle = StrategyEngine.on_protective_sell_fill(
                    cycle,
                    filled_qty=sell_trail.quantity,
                    avg_fill_price=fill_price,
                    status="Filled",
                )
                events.append(SimulatedEvent("protective_sell_filled", fill_price, f"Protective SELL filled @ {fill_price:.4f}", cycle.stage.value, {"stop_price": sell_trail.stop_price}))
                sell_trail = None

        if cycle.stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}:
            cycle, actions = StrategyEngine.on_price_update(
                cycle,
                price,
                is_rth=tick_rth_open,
                rth_message="simulated RTH closed" if not tick_rth_open else "simulated RTH open",
            )
            for action in actions:
                if action.action_type == "PLACE_BUY_TRAIL":
                    buy_trail = _new_trail_order(action, price, "BUY")
                    events.append(SimulatedEvent("buy_trail_submitted", price, "BUY trail submitted", cycle.stage.value, dict(action.payload)))
                elif action.action_type == "PLACE_BUY_MARKET":
                    fill_price = round_price(price * (1.0 + tick_buy_slippage_pct / 100.0))
                    cycle = StrategyEngine.on_order_submitted(cycle, action.payload["order_ref"], 100, 200, "Submitted")
                    cycle, follow_actions = StrategyEngine.on_buy_fill(cycle, filled_qty=cycle.quantity, avg_fill_price=fill_price, status="Filled")
                    events.append(SimulatedEvent("buy_market_filled", fill_price, f"BUY market filled @ {fill_price:.4f}", cycle.stage.value, dict(action.payload)))
                    for follow in follow_actions:
                        if follow.action_type == "PLACE_PROTECTIVE_SELL_TRAIL":
                            sell_trail = _new_trail_order(follow, fill_price, "SELL")
                            sell_trail_role = "protective"
                            events.append(SimulatedEvent("protective_sell_submitted", fill_price, "Protective SELL trail submitted", cycle.stage.value, dict(follow.payload)))
                        elif follow.action_type == "CANCEL_ORDER":
                            events.append(SimulatedEvent("cancel_remainder", price, "Cancel remaining BUY quantity", cycle.stage.value, dict(follow.payload)))
                elif action.action_type == "PLACE_PROTECTIVE_SELL_TRAIL":
                    sell_trail = _new_trail_order(action, price, "SELL")
                    sell_trail_role = "protective"
                    events.append(SimulatedEvent("protective_sell_submitted", price, "Protective SELL trail submitted", cycle.stage.value, dict(action.payload)))
                elif action.action_type == "CANCEL_ORDER" and action.payload.get("role") == "protective_sell":
                    # Simulate successful broker cancellation before the next tick.
                    cycle.protective_sell_status = "Cancelled"
                    cycle.protective_sell_cancel_requested = False
                    sell_trail = None
                    events.append(SimulatedEvent("protective_sell_cancelled", price, "Protective SELL cancelled before final SELL trail", cycle.stage.value, dict(action.payload)))
                elif action.action_type == "PLACE_SELL_TRAIL":
                    sell_trail = _new_trail_order(action, price, "SELL")
                    sell_trail_role = "final"
                    events.append(SimulatedEvent("sell_trail_submitted", price, "SELL trail submitted", cycle.stage.value, dict(action.payload)))
                elif action.action_type == "PLACE_SELL_MARKET":
                    fill_price = round_price(price * (1.0 - tick_sell_slippage_pct / 100.0))
                    cycle = StrategyEngine.on_order_submitted(cycle, action.payload["order_ref"], 300, 400, "Submitted")
                    cycle = StrategyEngine.on_sell_fill(cycle, filled_qty=cycle.buy_filled_qty, avg_fill_price=fill_price, status="Filled")
                    events.append(SimulatedEvent("sell_market_filled", fill_price, f"SELL market filled @ {fill_price:.4f}", cycle.stage.value, dict(action.payload)))

        elif cycle.stage == Stage.BUY_TRAIL_ACTIVE and buy_trail is not None:
            cycle.last_price = price
            if buy_trail.update(price):
                fill_qty = max(1, int(buy_trail.quantity * max(0.0, min(1.0, partial_buy_ratio))))
                fill_price = round_price(price * (1.0 + tick_buy_slippage_pct / 100.0))
                cycle, actions = StrategyEngine.on_buy_fill(
                    cycle,
                    filled_qty=fill_qty,
                    avg_fill_price=fill_price,
                    status="Filled" if fill_qty == buy_trail.quantity else "Submitted",
                )
                events.append(
                    SimulatedEvent(
                        "buy_filled",
                        fill_price,
                        f"BUY filled {fill_qty} @ {fill_price:.4f}",
                        cycle.stage.value,
                        {"filled_qty": fill_qty, "avg_fill_price": fill_price, "stop_price": buy_trail.stop_price},
                    )
                )
                for action in actions:
                    if action.action_type == "PLACE_PROTECTIVE_SELL_TRAIL":
                        sell_trail = _new_trail_order(action, fill_price, "SELL")
                        sell_trail_role = "protective"
                        events.append(SimulatedEvent("protective_sell_submitted", fill_price, "Protective SELL trail submitted", cycle.stage.value, dict(action.payload)))
                    elif action.action_type == "CANCEL_ORDER":
                        events.append(SimulatedEvent("cancel_remainder", price, "Cancel remaining BUY quantity", cycle.stage.value, dict(action.payload)))
                buy_trail = None

        elif cycle.stage == Stage.SELL_TRAIL_ACTIVE and sell_trail is not None:
            cycle.last_price = price
            if sell_trail.update(price):
                fill_price = round_price(price * (1.0 - tick_sell_slippage_pct / 100.0))
                cycle = StrategyEngine.on_sell_fill(
                    cycle,
                    filled_qty=sell_trail.quantity,
                    avg_fill_price=fill_price,
                    status="Filled",
                )
                events.append(
                    SimulatedEvent(
                        "sell_filled",
                        fill_price,
                        f"SELL filled {sell_trail.quantity} @ {fill_price:.4f}",
                        cycle.stage.value,
                        {"filled_qty": sell_trail.quantity, "avg_fill_price": fill_price, "stop_price": sell_trail.stop_price},
                    )
                )
                sell_trail = None

        if cycle.stage == Stage.CYCLE_COMPLETE:
            break

    return SimulationResult(cycle=cycle, events=events)
