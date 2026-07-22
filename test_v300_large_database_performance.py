from __future__ import annotations

import time
from pathlib import Path

from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine


def _completed_cycle(settings: StrategySettings, number: int):
    cycle = StrategyEngine.start_cycle(settings, number, "SIM", 100.0 + (number % 25), 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 95.0 + (number % 10) * 0.1
    cycle.avg_sell_price = cycle.avg_buy_price + ((number % 7) - 2) * 0.25
    cycle.sell_filled_qty = 10
    cycle.buy_commission = 0.35
    cycle.sell_commission = 0.35
    cycle.gross_pnl = (cycle.avg_sell_price - cycle.avg_buy_price) * 10
    cycle.net_pnl = cycle.gross_pnl - cycle.buy_commission - cycle.sell_commission
    minute = number % 60
    cycle.buy_filled_at = f"2026-01-{1 + (number % 20):02d}T14:{minute:02d}:00+00:00"
    cycle.sell_filled_at = f"2026-01-{1 + (number % 20):02d}T15:{minute:02d}:00+00:00"
    cycle.updated_at = cycle.sell_filled_at
    return cycle


def _seed_completed_cycles(storage: BotStorage, count: int = 8000) -> None:
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    with storage.connect() as con:
        for idx in range(1, count + 1):
            storage._upsert_cycle_in_connection(con, _completed_cycle(settings, idx))


def test_history_summary_large_database_has_bounded_cached_runtime(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    _seed_completed_cycles(storage, 8000)

    start = time.perf_counter()
    first = storage.history_summary("AAPL")
    first_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    second = storage.history_summary("AAPL")
    cached_elapsed = time.perf_counter() - start

    assert first["cycles"] == 8000
    assert second == first
    assert first_elapsed < 2.0
    assert cached_elapsed < 0.05


def test_history_cycles_large_database_limit_keeps_ui_query_bounded(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    _seed_completed_cycles(storage, 8000)

    start = time.perf_counter()
    rows = storage.history_cycles("AAPL", limit=250)
    elapsed = time.perf_counter() - start

    assert len(rows) == 250
    assert elapsed < 1.0
    assert {row["ticker"] for row in rows} == {"AAPL"}
