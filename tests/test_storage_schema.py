import sqlite3
from contextlib import closing

from app.storage import BotStorage


def test_schema_upgrade_adds_missing_recent_columns(tmp_path):
    db = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(db)) as con:
        with con:
            con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
            con.execute(
                """
                CREATE TABLE cycles (
                id TEXT PRIMARY KEY,
                cycle_number INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                account TEXT,
                con_id INTEGER,
                exchange TEXT,
                currency TEXT,
                investment_amount REAL NOT NULL,
                budget REAL NOT NULL,
                reinvest_profits INTEGER NOT NULL,
                reinvested_profit REAL NOT NULL,
                initial_drop_pct REAL NOT NULL,
                buy_rebound_trail_pct REAL NOT NULL,
                rise_trigger_pct REAL NOT NULL,
                sell_trailing_stop_pct REAL NOT NULL,
                anchor_price REAL,
                last_price REAL,
                drop_trigger_price REAL,
                buy_initial_trail_stop_price REAL,
                rise_trigger_price REAL,
                sell_initial_trail_stop_price REAL,
                quantity INTEGER NOT NULL,
                buy_order_id INTEGER,
                buy_perm_id INTEGER,
                buy_order_ref TEXT,
                buy_status TEXT,
                buy_filled_qty INTEGER NOT NULL,
                avg_buy_price REAL,
                buy_commission REAL NOT NULL,
                buy_filled_at TEXT,
                sell_order_id INTEGER,
                sell_perm_id INTEGER,
                sell_order_ref TEXT,
                sell_status TEXT,
                sell_filled_qty INTEGER NOT NULL,
                avg_sell_price REAL,
                sell_commission REAL NOT NULL,
                sell_filled_at TEXT,
                gross_pnl REAL NOT NULL,
                net_pnl REAL NOT NULL,
                stop_after_current_cycle INTEGER NOT NULL,
                error_message TEXT
            )
            """
        )
    storage = BotStorage(db)
    with storage.connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(cycles)").fetchall()}

    assert "primary_exchange" in columns
    assert "rth_only" in columns
    assert "atr_adaptive_enabled" in columns
    assert "atr_adapt_minimum_profit_enabled" in columns
    assert "atr_block_new_buy_until_ready" in columns
    assert "atr_adapt_protective_sell_enabled" in columns
    assert "atr_protective_sell_multiplier" in columns
    assert "cancel_sell_and_liquidate_before_close_enabled" in columns
    assert "liquidate_before_close_minutes" in columns
    assert "close_before_rth_liquidation_requested" in columns
    assert "close_before_rth_cancel_requested" in columns
    assert "buy_remainder_cancel_requested" in columns


def test_execution_exists_treats_empty_id_as_missing(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    assert storage.execution_exists("") is False
    assert storage.execution_exists(None) is False  # type: ignore[arg-type]
