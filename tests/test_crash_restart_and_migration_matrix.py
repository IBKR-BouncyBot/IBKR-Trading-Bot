"""Subprocess crash-consistency and reconstructed schema-migration tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing
from pathlib import Path

import pytest

from app.models import Stage
from app.storage import BotStorage

_OLD_CYCLES_SCHEMA = """
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


def _run_child(script: str, db_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd())
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), str(db_path)],
        cwd=Path.cwd(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _create_legacy_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        con.execute(_OLD_CYCLES_SCHEMA)
        info = con.execute("PRAGMA table_info(cycles)").fetchall()
        values: dict[str, object] = {
            "id": "legacy-cycle",
            "cycle_number": 7,
            "ticker": "AAPL",
            "stage": Stage.WAIT_INITIAL_DROP.value,
            "created_at": "2025-01-01T14:30:00+00:00",
            "updated_at": "2025-01-01T14:30:00+00:00",
            "account": "",
            "exchange": "SMART",
            "currency": "USD",
            "investment_amount": 1_000.0,
            "budget": 1_000.0,
            "initial_drop_pct": 2.0,
            "buy_rebound_trail_pct": 1.0,
            "rise_trigger_pct": 3.0,
            "sell_trailing_stop_pct": 1.0,
            "quantity": 0,
        }
        for row in info:
            name = str(row[1])
            if name in values:
                continue
            column_type = str(row[2]).upper()
            values[name] = 0.0 if "REAL" in column_type else 0 if "INT" in column_type else None
        columns = [str(row[1]) for row in info]
        placeholders = ",".join("?" for _ in columns)
        con.execute(
            f"INSERT INTO cycles ({','.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )


def test_committed_order_intent_survives_abrupt_process_exit(tmp_path: Path) -> None:
    db = tmp_path / "intent.sqlite"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path
        from app.models import StrategySettings
        from app.storage import BotStorage
        from app.strategy import StrategyEngine

        storage = BotStorage(Path(sys.argv[1]))
        settings = StrategySettings(
            ticker="AAPL",
            atr_adaptive_enabled=False,
            atr_block_new_buy_until_ready=False,
        )
        cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
        storage.upsert_cycle(cycle)
        storage.create_order_intent(
            cycle=cycle,
            action="BUY",
            order_type="TRAIL",
            order_ref="IBKRBOT|AAPL|CRASH|BUY_TRAIL",
            quantity=10,
            trailing_percent=1.0,
            initial_stop_price=99.0,
            raw={"crash_point": "after_intent"},
        )
        os._exit(0)
        """,
        db,
    )
    assert result.returncode == 0, result.stderr

    storage = BotStorage(db)
    cycle = storage.get_latest_active_cycle()
    assert cycle is not None
    audit = storage.get_cycle_audit_bundle(cycle.id)
    assert len(audit["orders"]) == 1
    assert audit["orders"][0]["status"] == "INTENT_CREATED"
    assert audit["orders"][0]["raw"]["crash_point"] == "after_intent"


def test_committed_execution_survives_abrupt_exit_and_is_detectable_for_deduplication(tmp_path: Path) -> None:
    db = tmp_path / "execution.sqlite"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path
        from app.models import StrategySettings
        from app.storage import BotStorage
        from app.strategy import StrategyEngine

        storage = BotStorage(Path(sys.argv[1]))
        settings = StrategySettings(ticker="AAPL")
        cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
        storage.upsert_cycle(cycle)
        storage.add_execution(
            cycle=cycle,
            ticker="AAPL",
            side="BUY",
            shares=10,
            price=98.0,
            execution_id="CRASH-EXEC-1",
            order_ref="IBKRBOT|AAPL|CRASH|BUY",
        )
        os._exit(0)
        """,
        db,
    )
    assert result.returncode == 0, result.stderr

    storage = BotStorage(db)
    assert storage.execution_exists("CRASH-EXEC-1") is True
    cycle = storage.get_latest_active_cycle()
    assert cycle is not None
    audit = storage.get_cycle_audit_bundle(cycle.id)
    assert [row["execution_id"] for row in audit["executions"]] == ["CRASH-EXEC-1"]


def test_uncommitted_sqlite_transaction_is_rolled_back_after_abrupt_exit(tmp_path: Path) -> None:
    db = tmp_path / "rollback.sqlite"
    storage = BotStorage(db)
    result = _run_child(
        """
        import os
        import sqlite3
        import sys

        con = sqlite3.connect(sys.argv[1])
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO events(created_at, level, message, raw_json) VALUES(?,?,?,?)",
            ("2026-07-10T14:30:00+00:00", "INFO", "must roll back", "{}"),
        )
        os._exit(0)
        """,
        db,
    )
    assert result.returncode == 0, result.stderr
    assert storage.get_recent_events(10) == []


def test_online_backup_after_abrupt_writer_exit_contains_committed_rows(tmp_path: Path) -> None:
    db = tmp_path / "backup_after_crash.sqlite"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path
        from app.storage import BotStorage

        storage = BotStorage(Path(sys.argv[1]))
        storage.add_event("WARN", "committed before abrupt exit", ticker="AAPL")
        os._exit(0)
        """,
        db,
    )
    assert result.returncode == 0, result.stderr

    storage = BotStorage(db)
    backup = storage.backup_database("post_crash", keep=3)
    assert backup is not None
    validation = storage.validate_restore_candidate(backup)
    assert validation["ok"] is True
    with closing(sqlite3.connect(backup)) as con:
        message = con.execute("SELECT message FROM events").fetchone()[0]
    assert message == "committed before abrupt exit"


def test_reconstructed_legacy_schema_is_migrated_in_place_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    _create_legacy_database(db)

    storage = BotStorage(db)
    migrated = storage.get_cycle("legacy-cycle")
    assert migrated is not None
    assert migrated.ticker == "AAPL"
    assert migrated.cycle_number == 7
    assert migrated.stage == Stage.WAIT_INITIAL_DROP

    expected_columns = {
        "primary_exchange",
        "rth_only",
        "atr_adaptive_enabled",
        "atr_adapt_minimum_profit_enabled",
        "atr_block_new_buy_until_ready",
        "protective_sell_enabled",
        "slippage_buffer_enabled",
        "hard_risk_limits_enabled",
        "recovery_required",
        "protective_sell_order_ref",
        "protective_sell_filled_qty",
    }
    with storage.connect() as con:
        first_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(cycles)")}
        required_tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert expected_columns <= first_columns
    assert {"orders", "executions", "events", "decision_events", "broker_events"} <= required_tables
    assert list((tmp_path / "backups").glob("*_before_schema_check.sqlite"))

    reopened = BotStorage(db)
    with reopened.connect() as con:
        second_columns = {str(row[1]) for row in con.execute("PRAGMA table_info(cycles)")}
    assert second_columns == first_columns
    assert reopened.get_cycle("legacy-cycle") is not None


@pytest.mark.parametrize("precreate", [False, True])
def test_empty_or_zero_byte_database_bootstraps_complete_schema(tmp_path: Path, precreate: bool) -> None:
    db = tmp_path / f"empty_{precreate}.sqlite"
    if precreate:
        db.touch()
    storage = BotStorage(db)

    validation = storage.validate_backup(db)
    assert validation["ok"] is True
    assert storage.get_latest_active_cycle() is None
    assert storage.get_recent_events() == []


def test_corrupt_database_is_rejected_without_overwriting_original_bytes(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.sqlite"
    original = b"this is not a sqlite database\x00\xff"
    db.write_bytes(original)

    with pytest.raises(sqlite3.DatabaseError):
        BotStorage(db)

    assert db.read_bytes() == original
    backups = list((tmp_path / "backups").glob("*_before_schema_check.sqlite"))
    assert backups
    assert BotStorage._validate_sqlite_database_file(backups[0])["ok"] is False
