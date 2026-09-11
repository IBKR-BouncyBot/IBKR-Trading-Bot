from __future__ import annotations

"""Run the pure strategy engine against a CSV price series.

Usage from the project root:
    python scripts/run_simulated_strategy.py tests/simulated_data/full_profitable_cycle.csv

This script does not connect to IBKR. It is intended for documentation,
regression testing, and manual review of strategy behavior with synthetic data.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import StrategySettings  # noqa: E402
from tests.simulated_strategy_runner import load_prices_csv, run_one_cycle  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one simulated bot cycle from CSV price data.")
    parser.add_argument("csv", type=Path, help="CSV file with a price column.")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--investment", type=float, default=1000.0)
    parser.add_argument("--initial-drop", type=float, default=5.0)
    parser.add_argument("--buy-rebound", type=float, default=2.0)
    parser.add_argument("--min-profit", type=float, default=10.0)
    parser.add_argument("--sell-trail", type=float, default=1.0)
    parser.add_argument("--protective-sell", action="store_true")
    parser.add_argument("--protective-trail", type=float, default=3.0)
    parser.add_argument("--slippage-buffer", type=float, default=0.0, help="Enable BUY/SELL planning slippage buffer when > 0.")
    parser.add_argument("--partial-buy-ratio", type=float, default=1.0)
    parser.add_argument("--rth-closed", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = StrategySettings(
        ticker=args.ticker,
        investment_amount=args.investment,
        initial_drop_pct=args.initial_drop,
        buy_rebound_trail_pct=args.buy_rebound,
        rise_trigger_pct=args.min_profit,
        sell_trailing_stop_pct=args.sell_trail,
        protective_sell_enabled=bool(args.protective_sell),
        protective_sell_trailing_stop_pct=args.protective_trail,
        slippage_buffer_enabled=args.slippage_buffer > 0,
        slippage_buffer_pct=max(0.0, args.slippage_buffer),
    )
    result = run_one_cycle(
        settings,
        load_prices_csv(args.csv),
        rth_open=not args.rth_closed,
        partial_buy_ratio=args.partial_buy_ratio,
    )
    payload = {
        "final_stage": result.cycle.stage.value,
        "ticker": result.cycle.ticker,
        "quantity_planned": result.cycle.quantity,
        "buy_filled_qty": result.cycle.buy_filled_qty,
        "avg_buy_price": result.cycle.avg_buy_price,
        "sell_filled_qty": result.cycle.sell_filled_qty,
        "avg_sell_price": result.cycle.avg_sell_price,
        "gross_pnl": result.cycle.gross_pnl,
        "net_pnl": result.cycle.net_pnl,
        "events": [asdict(event) for event in result.events],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
