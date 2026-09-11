"""Run the deterministic strategy simulation from the command line.

Usage from the project root:
    python scripts/run_simulation.py

The script uses ``tests/fixtures/full_cycle_prices.csv``. It is a local
development demonstration only and never connects to TWS or IB Gateway.
"""

from __future__ import annotations

import csv
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import StrategySettings  # noqa: E402
from app.simulation import simulate_price_path  # noqa: E402


def main() -> int:
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "full_cycle_prices.csv"
    with fixture.open(newline="", encoding="utf-8") as f:
        prices = [float(row["price"]) for row in csv.DictReader(f)]

    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        rise_trigger_pct=3.0,
        sell_trailing_stop_pct=1.0,
        auto_repeat=False,
    )
    result = simulate_price_path(settings, prices)

    print("Simulated events")
    print("----------------")
    for event in result.events:
        print(f"{event.index:02d} price={event.price:8.4f} stage={event.stage:24s} {event.message}")

    cycle = result.cycle
    print("\nFinal cycle")
    print("-----------")
    print(f"stage:       {cycle.stage.value}")
    print(f"quantity:    {cycle.buy_filled_qty}")
    print(f"buy avg:     {cycle.avg_buy_price:.4f}" if cycle.avg_buy_price else "buy avg:     -")
    print(f"sell avg:    {cycle.avg_sell_price:.4f}" if cycle.avg_sell_price else "sell avg:    -")
    print(f"net pnl:     {cycle.net_pnl:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
