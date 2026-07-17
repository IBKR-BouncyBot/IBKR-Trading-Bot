from pathlib import Path

from app.models import ConnectionSettings, Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine


def test_storage_settings_and_cycle_roundtrip(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    conn = ConnectionSettings(host="127.0.0.1", port=7497, client_id=123, account="DU123")
    strat = StrategySettings(ticker="MSFT", investment_amount=2500, reinvest_profits=True)
    storage.save_connection_settings(conn)
    storage.save_strategy_settings(strat)

    assert storage.load_connection_settings().client_id == 123
    assert storage.load_strategy_settings().ticker == "MSFT"

    cycle = StrategyEngine.start_cycle(strat, 1, conn.account, 100.0, 25.0)
    storage.upsert_cycle(cycle)
    loaded = storage.get_latest_active_cycle("MSFT")

    assert loaded is not None
    assert loaded.ticker == "MSFT"
    assert loaded.stage == Stage.WAIT_INITIAL_DROP
    assert loaded.budget == 2525.0


def test_history_export(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(ticker="AAPL", investment_amount=1000)
    cycle = StrategyEngine.start_cycle(strat, 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 97.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=98.0, status="Filled")
    cycle = StrategyEngine.on_sell_fill(cycle, filled_qty=10, avg_fill_price=101.0, status="Filled")
    storage.upsert_cycle(cycle)

    rows = storage.history_cycles("AAPL")
    assert len(rows) == 1
    assert rows[0]["net_pnl"] == 30.0

    target = storage.export_history_csv(tmp_path / "history.csv")
    assert target.exists()
    assert "AAPL" in target.read_text(encoding="utf-8")


def test_add_execution_roundtrip_does_not_misbind_currency_and_time(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(ticker="AAPL", investment_amount=1000)
    cycle = StrategyEngine.start_cycle(strat, 1, "", 100.0, 0.0)
    storage.upsert_cycle(cycle)
    storage.add_execution(
        cycle=cycle,
        ticker="AAPL",
        side="BUY",
        shares=5,
        price=100.0,
        avg_price=100.0,
        commission=1.25,
        currency="USD",
        order_ref="IBKRBOT|AAPL|TEST|BUY",
        execution_id="EXEC1",
        executed_at="2026-01-01T00:00:00+00:00",
    )
    with storage.connect() as con:
        row = con.execute("SELECT currency, executed_at FROM executions WHERE execution_id='EXEC1'").fetchone()
    assert row["currency"] == "USD"
    assert row["executed_at"] == "2026-01-01T00:00:00+00:00"


def test_execution_exists(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(ticker="AAPL", investment_amount=1000)
    cycle = StrategyEngine.start_cycle(strat, 1, "", 100.0, 0.0)
    storage.upsert_cycle(cycle)
    assert not storage.execution_exists("EXEC-RECOVERED")
    storage.add_execution(
        cycle=cycle,
        ticker="AAPL",
        side="BUY",
        shares=1,
        price=100.0,
        execution_id="EXEC-RECOVERED",
    )
    assert storage.execution_exists("EXEC-RECOVERED")


def test_history_cycles_include_percentage_metrics(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(ticker="AAPL", investment_amount=1000, initial_drop_pct=5, buy_rebound_trail_pct=2, rise_trigger_pct=10, sell_trailing_stop_pct=1)
    cycle = StrategyEngine.start_cycle(strat, 1, "", 100.0, 0.0)
    cycle, _ = StrategyEngine.on_price_update(cycle, 94.0)
    cycle, _ = StrategyEngine.on_buy_fill(cycle, filled_qty=10, avg_fill_price=95.0, status="Filled")
    cycle.sell_initial_trail_stop_price = 104.50
    cycle = StrategyEngine.on_sell_fill(cycle, filled_qty=10, avg_fill_price=106.0, status="Filled")
    storage.upsert_cycle(cycle)

    row = storage.history_cycles("AAPL")[0]

    assert round(row["sell_vs_buy_pct"], 2) == 11.58
    assert round(row["gross_pnl_pct"], 2) == 11.58
    assert row["configured_min_profit_pct"] == 10
    assert row["configured_initial_drop_pct"] == 5
    assert row["configured_buy_rebound_pct"] == 2
    assert row["configured_sell_trail_pct"] == 1


def test_history_cycles_include_protection_and_slippage_columns(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=3.0,
        slippage_buffer_enabled=True,
        slippage_buffer_pct=0.5,
    )
    cycle = StrategyEngine.start_cycle(strat, 1, "", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.sell_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.avg_sell_price = 105.0
    storage.upsert_cycle(cycle)

    row = storage.history_cycles("AAPL")[0]

    assert row["protective_sell_enabled_display"] == "yes"
    assert row["configured_protective_sell_trail_pct"] == 3.0
    assert row["slippage_buffer_enabled_display"] == "yes"
    assert row["configured_slippage_buffer_pct"] == 0.5

    target = storage.export_history_csv(tmp_path / "history_with_risk.csv")
    csv_text = target.read_text(encoding="utf-8")
    assert "protective_sell_enabled_display" in csv_text
    assert "configured_slippage_buffer_pct" in csv_text


def test_storage_connection_context_manager_closes_handle(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")

    with storage.connect() as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1

    import sqlite3
    try:
        con.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("storage.connect() context manager left the SQLite connection open")


def test_completed_cycle_count_total_does_not_reset_by_day(tmp_path: Path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    strat = StrategySettings(ticker="AAPL", investment_amount=1000)
    for number, timestamp in [(1, "2026-01-01T12:00:00+00:00"), (2, "2026-01-02T12:00:00+00:00")]:
        cycle = StrategyEngine.start_cycle(strat, number, "", 100.0, 0.0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.updated_at = timestamp
        storage.upsert_cycle(cycle)

    assert storage.get_completed_cycle_count("AAPL") == 2
    assert storage.get_completed_cycle_count_today("AAPL", day_utc="2026-01-02") == 1
