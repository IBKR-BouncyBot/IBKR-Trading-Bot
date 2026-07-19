"""Portable SQLite repository, migration, backup, and audit-export layer.

The database lives beside the source tree or packaged executable. It persists
draft settings, active/completed cycles, app orders, deduplicated executions,
audit/decision/broker events, and locally derived history metrics. Schema changes
are additive, connections are short-lived, and backups use SQLite's online API
with restore-candidate validation.

SQLite records application intent and observed facts; the controller still
compares it with live app-owned broker state during recovery.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import ConnectionSettings, CycleState, Stage, StrategySettings, utc_now_iso


class _ClosingSqliteConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the handle.

    sqlite3.Connection.__exit__ commits or rolls back but intentionally leaves
    the connection open. BotStorage methods rely on short-lived connections, so
    closing here makes `with storage.connect()` match that contract.
    """

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _backup_stamp() -> str:
    """Return a filename-safe UTC timestamp with microseconds for backups."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse the ISO UTC strings stored by the app for summary metrics."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class BotStorage:
    """Small repository object around the portable SQLite database.

    The class does not keep a persistent SQLite connection open. That is
    intentional: it reduces the chance that an unexpected Windows/app restart
    leaves a long-lived writer connection. Each method opens, commits, and
    closes its own connection.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._history_summary_cache: dict[str, tuple[int, str, dict[str, Any]]] = {}
        self._ensure_schema()

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection.

        Foreign keys are enabled for every connection because SQLite keeps this
        setting per connection. row_factory allows callers to read rows by name.
        """
        con = sqlite3.connect(self.db_path, timeout=10.0, factory=_ClosingSqliteConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        return con

    def _ensure_schema(self) -> None:
        """Create or migrate the SQLite schema in place.

        The app ships as a portable folder, so an existing bot_state.sqlite
        can be opened by a later build. Schema updates must be additive
        and idempotent. A best-effort backup is made before migrations touch an
        existing database file.
        """
        if self.db_path.exists():
            try:
                backup_dir = self.db_path.parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = backup_dir / f"bot_state_{_backup_stamp()}_before_schema_check.sqlite"
                with sqlite3.connect(self.db_path, factory=_ClosingSqliteConnection) as source, sqlite3.connect(target, factory=_ClosingSqliteConnection) as dest:
                    source.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    source.backup(dest)
            except Exception:
                pass
        with sqlite3.connect(self.db_path, factory=_ClosingSqliteConnection) as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cycles (
                    id TEXT PRIMARY KEY,
                    cycle_number INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    account TEXT,
                    con_id INTEGER,
                    exchange TEXT,
                    primary_exchange TEXT DEFAULT '',
                    currency TEXT,
                    rth_only INTEGER NOT NULL DEFAULT 1,
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
                );

                CREATE INDEX IF NOT EXISTS idx_cycles_ticker_stage ON cycles(ticker, stage);
                CREATE INDEX IF NOT EXISTS idx_cycles_updated_at ON cycles(updated_at);
                CREATE INDEX IF NOT EXISTS idx_cycles_stage_ticker_updated ON cycles(stage, ticker, updated_at);
                CREATE INDEX IF NOT EXISTS idx_cycles_stage_sell_updated ON cycles(stage, sell_filled_at, updated_at);
                CREATE INDEX IF NOT EXISTS idx_cycles_stage_ticker_sell_updated ON cycles(stage, ticker, sell_filled_at, updated_at);

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    order_id INTEGER,
                    perm_id INTEGER,
                    order_ref TEXT,
                    quantity INTEGER NOT NULL,
                    trailing_percent REAL,
                    initial_stop_price REAL,
                    status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_orders_ref ON orders(order_ref);
                CREATE INDEX IF NOT EXISTS idx_orders_cycle ON orders(cycle_id);
                CREATE INDEX IF NOT EXISTS idx_orders_cycle_status_ref ON orders(cycle_id, status, order_ref);

                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT,
                    ticker TEXT NOT NULL,
                    order_ref TEXT,
                    order_id INTEGER,
                    perm_id INTEGER,
                    execution_id TEXT,
                    side TEXT NOT NULL,
                    shares REAL NOT NULL,
                    price REAL NOT NULL,
                    avg_price REAL,
                    commission REAL NOT NULL DEFAULT 0,
                    currency TEXT DEFAULT 'USD',
                    executed_at TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_exec_cycle ON executions(cycle_id);
                CREATE INDEX IF NOT EXISTS idx_exec_order_ref ON executions(order_ref);
                CREATE INDEX IF NOT EXISTS idx_exec_execution_id ON executions(execution_id);
                CREATE INDEX IF NOT EXISTS idx_exec_cycle_time ON executions(cycle_id, executed_at);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    message TEXT NOT NULL,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_cycle_created ON events(cycle_id, created_at, id);

                CREATE TABLE IF NOT EXISTS decision_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    stage_before TEXT,
                    stage_after TEXT,
                    decision_result TEXT,
                    message TEXT NOT NULL,
                    broker_order_id INTEGER,
                    perm_id INTEGER,
                    raw_json TEXT,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_decision_events_cycle ON decision_events(cycle_id);
                CREATE INDEX IF NOT EXISTS idx_decision_events_created_at ON decision_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_decision_events_cycle_created ON decision_events(cycle_id, created_at, id);

                CREATE TABLE IF NOT EXISTS broker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    cycle_id TEXT,
                    order_ref TEXT,
                    order_id INTEGER,
                    perm_id INTEGER,
                    execution_id TEXT,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_broker_events_created_at ON broker_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_broker_events_order_ref ON broker_events(order_ref);
                CREATE INDEX IF NOT EXISTS idx_broker_events_execution_id ON broker_events(execution_id);
                """
            )
            self._add_column_if_missing(con, "cycles", "primary_exchange", "TEXT DEFAULT ''")
            self._add_column_if_missing(con, "cycles", "rth_only", "INTEGER NOT NULL DEFAULT 1")
            for column, definition in {
                "atr_adaptive_enabled": "INTEGER NOT NULL DEFAULT 1",
                "atr_adapt_minimum_profit_enabled": "INTEGER NOT NULL DEFAULT 1",
                "atr_block_new_buy_until_ready": "INTEGER NOT NULL DEFAULT 1",
                "atr_adapt_protective_sell_enabled": "INTEGER NOT NULL DEFAULT 0",
                "atr_protective_sell_multiplier": "REAL NOT NULL DEFAULT 3.0",
                "atr_period": "INTEGER NOT NULL DEFAULT 14",
                "atr_bar_seconds": "INTEGER NOT NULL DEFAULT 60",
                "atr_initial_drop_multiplier": "REAL NOT NULL DEFAULT 1.5",
                "atr_buy_rebound_multiplier": "REAL NOT NULL DEFAULT 0.75",
                "atr_minimum_profit_multiplier": "REAL NOT NULL DEFAULT 1.0",
                "atr_sell_trail_multiplier": "REAL NOT NULL DEFAULT 1.0",
                "atr_min_pct": "REAL NOT NULL DEFAULT 0.10",
                "atr_max_pct": "REAL NOT NULL DEFAULT 20.0",
                "protective_sell_enabled": "INTEGER NOT NULL DEFAULT 0",
                "protective_sell_trailing_stop_pct": "REAL NOT NULL DEFAULT 0",
                "slippage_buffer_enabled": "INTEGER NOT NULL DEFAULT 0",
                "slippage_buffer_pct": "REAL NOT NULL DEFAULT 0",
                "hard_risk_limits_enabled": "INTEGER NOT NULL DEFAULT 0",
                "max_daily_loss_ticker": "REAL NOT NULL DEFAULT 0",
                "max_daily_loss_total": "REAL NOT NULL DEFAULT 0",
                "max_cycles_per_ticker_day": "INTEGER NOT NULL DEFAULT 0",
                "max_consecutive_losses": "INTEGER NOT NULL DEFAULT 0",
                "max_spread_pct": "REAL NOT NULL DEFAULT 0",
                "min_trade_price": "REAL NOT NULL DEFAULT 0",
                "max_gap_from_prev_close_pct": "REAL NOT NULL DEFAULT 0",
                "block_delayed_data_in_live": "INTEGER NOT NULL DEFAULT 1",
                "what_if_check_enabled": "INTEGER NOT NULL DEFAULT 1",
                "stale_data_guard_enabled": "INTEGER NOT NULL DEFAULT 1",
                "max_selected_price_age_seconds": "REAL NOT NULL DEFAULT 3",
                "max_bid_ask_age_seconds": "REAL NOT NULL DEFAULT 3",
                "max_rth_status_age_seconds": "REAL NOT NULL DEFAULT 60",
                "volatility_filter_enabled": "INTEGER NOT NULL DEFAULT 0",
                "volatility_window_seconds": "INTEGER NOT NULL DEFAULT 300",
                "max_recent_price_move_pct": "REAL NOT NULL DEFAULT 5",
                "session_timing_guard_enabled": "INTEGER NOT NULL DEFAULT 1",
                "no_new_buy_first_minutes": "INTEGER NOT NULL DEFAULT 5",
                "no_new_buy_last_minutes": "INTEGER NOT NULL DEFAULT 15",
                "cancel_buy_before_close_minutes": "INTEGER NOT NULL DEFAULT 5",
                "recovery_required": "INTEGER NOT NULL DEFAULT 0",
                "close_position_market_requested": "INTEGER NOT NULL DEFAULT 0",
                "protective_sell_order_id": "INTEGER",
                "protective_sell_perm_id": "INTEGER",
                "protective_sell_order_ref": "TEXT",
                "protective_sell_status": "TEXT",
                "protective_sell_initial_stop_price": "REAL",
                "protective_sell_cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "protective_sell_filled_qty": "INTEGER NOT NULL DEFAULT 0",
                "protective_avg_sell_price": "REAL",
                "protective_sell_commission": "REAL NOT NULL DEFAULT 0",
                "protective_sell_filled_at": "TEXT",
            }.items():
                self._add_column_if_missing(con, "cycles", column, definition)

    @staticmethod
    def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _json_default(value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        missing = object()
        enum_value = getattr(value, "value", missing)
        if enum_value is not missing:
            return enum_value
        return str(value)

    def debug_reports_dir(self) -> Path:
        """Directory for human-readable diagnostics kept beside SQLite."""
        target = self.db_path.parent / "debug_reports"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _append_human_event_log(
        self,
        *,
        created_at: str,
        level: str,
        message: str,
        ticker: Optional[str],
        cycle_id: Optional[str],
        raw: Optional[dict[str, Any]],
    ) -> None:
        """Append a readable audit line without affecting SQLite writes."""
        try:
            parts = [created_at, f"[{str(level or '').upper()}]"]
            if ticker:
                parts.append(f"ticker={str(ticker).upper()}")
            if cycle_id:
                parts.append(f"cycle={cycle_id}")
            parts.append(str(message or ""))
            if raw:
                parts.append("raw=" + json.dumps(raw, sort_keys=True, default=self._json_default))
            with (self.debug_reports_dir() / "audit_events_readable.log").open("a", encoding="utf-8") as handle:
                handle.write(" ".join(parts).rstrip() + "\n")
        except Exception:
            pass

    def write_human_debug_report(self, snapshot: dict[str, Any]) -> Path:
        """Write a readable latest-state report for support/debugging.

        SQLite remains the source of truth. This file is a best-effort operator
        report containing the same important state in a format that can be sent
        directly with screenshots and audit logs.
        """
        target = self.debug_reports_dir() / "latest_state_report.txt"
        snapshot = dict(snapshot or {})
        connection = dict(snapshot.get("connection") or {})
        strategy = dict(snapshot.get("strategy") or {})
        cycle = dict(snapshot.get("active_cycle") or {})
        price = dict(snapshot.get("price_snapshot") or {})
        broker = dict(snapshot.get("broker_recovery") or {})
        history = dict(snapshot.get("history_summary") or {})
        events = list(snapshot.get("events") or [])
        lines: list[str] = []

        def section(title: str) -> None:
            if lines:
                lines.append("")
            lines.append(title)
            lines.append("-" * len(title))

        def add_mapping(data: dict[str, Any], keys: list[str] | None = None) -> None:
            if not data:
                lines.append("  none")
                return
            selected = keys or sorted(data.keys())
            for key in selected:
                if key not in data:
                    continue
                value = data.get(key)
                if isinstance(value, (dict, list, tuple)):
                    formatted = json.dumps(value, indent=2, sort_keys=True, default=self._json_default)
                    lines.append(f"  {key}: {formatted}")
                else:
                    lines.append(f"  {key}: {value}")

        lines.append("IBKR Trading Bot human-readable debug report")
        lines.append(f"Generated at: {utc_now_iso()}")
        lines.append(f"SQLite DB: {self.db_path}")
        lines.append(f"Connected: {snapshot.get('connected')}")
        lines.append(f"Status: {snapshot.get('status')}")
        lines.append(f"Recovery required: {snapshot.get('recovery_required')}")
        lines.append(f"Startup resume required: {snapshot.get('startup_resume_required')}")

        section("Connection")
        add_mapping(connection, ["platform", "host", "port", "client_id", "trading_mode", "market_data_type", "account"])
        lines.append(f"  display_account: {snapshot.get('display_account') or ''}")
        lines.append(f"  broker_accounts: {json.dumps(snapshot.get('broker_accounts') or [])}")

        section("Strategy")
        add_mapping(strategy, [
            "ticker", "exchange", "primary_exchange", "currency", "investment_amount",
            "initial_drop_pct", "buy_rebound_trail_pct", "rise_trigger_pct", "sell_trailing_stop_pct",
            "protective_sell_enabled", "protective_sell_trailing_stop_pct",
            "atr_adaptive_enabled", "atr_block_new_buy_until_ready",
            "atr_adapt_minimum_profit_enabled", "atr_adapt_protective_sell_enabled",
            "atr_protective_sell_multiplier", "atr_period", "atr_bar_seconds",
            "rth_only", "max_cycles_per_ticker_day", "max_spread_pct", "max_gap_from_prev_close_pct",
            "session_timing_guard_enabled", "volatility_filter_enabled", "stale_data_guard_enabled",
        ])

        section("Active cycle")
        add_mapping(cycle, [
            "id", "ticker", "stage", "cycle_number", "account", "updated_at", "error_message",
            "quantity", "buy_filled_qty", "avg_buy_price", "sell_filled_qty", "avg_sell_price",
            "last_price", "anchor_price", "drop_trigger_price", "buy_order_ref", "buy_order_id",
            "buy_status", "protective_sell_order_ref", "protective_sell_status", "sell_order_ref",
            "sell_order_id", "sell_status", "stop_after_current_cycle", "recovery_required",
        ])

        section("Price snapshot")
        add_mapping(price, [
            "price", "source", "status", "error", "timestamp", "age_seconds", "api_data_age_seconds",
            "api_data_seen_count", "api_data_change_count", "api_data_last_received_at",
            "api_data_last_change_at", "requested_market_data_type", "subscription_market_data_type",
            "selected_market_data_type", "ticker_update_time", "rth_open", "rth_message", "rth_checked_at",
        ])
        if price.get("fields"):
            lines.append("  fields:")
            for key, value in sorted(dict(price.get("fields") or {}).items()):
                lines.append(f"    {key}: {value}")

        section("Broker recovery probe")
        add_mapping(broker, ["checked_at", "connected", "error", "position", "position_qty", "account"])
        open_orders = broker.get("open_app_orders") or []
        lines.append(f"  open_app_orders: {len(open_orders)}")
        for idx, order in enumerate(open_orders[:20], start=1):
            lines.append(f"    {idx}. {json.dumps(order, sort_keys=True, default=self._json_default)}")
        executions = broker.get("recent_executions") or []
        lines.append(f"  recent_executions: {len(executions)}")
        for idx, execution in enumerate(executions[:20], start=1):
            lines.append(f"    {idx}. {json.dumps(execution, sort_keys=True, default=self._json_default)}")

        section("History summary")
        add_mapping(history)

        section("Recent audit events")
        if not events:
            lines.append("  none")
        for event in events[-120:]:
            if not isinstance(event, dict):
                lines.append(f"  {event}")
                continue
            when = event.get("created_at") or ""
            level = event.get("level") or ""
            ticker = event.get("ticker") or ""
            cycle_id = event.get("cycle_id") or ""
            message = event.get("message") or ""
            suffix = ""
            if ticker:
                suffix += f" ticker={ticker}"
            if cycle_id:
                suffix += f" cycle={cycle_id}"
            lines.append(f"  {when} [{level}]{suffix} {message}".rstrip())

        section("Raw snapshot JSON")
        lines.append(json.dumps(snapshot, indent=2, sort_keys=True, default=self._json_default))
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return target

    def set_json(self, key: str, value: Any) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, default=self._json_default), utc_now_iso()),
            )

    def get_json(self, key: str, default: Any = None) -> Any:
        with self.connect() as con:
            row = con.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def save_connection_settings(self, settings: ConnectionSettings) -> None:
        self.set_json("connection", asdict(settings))

    def load_connection_settings(self) -> ConnectionSettings:
        data = self.get_json("connection", None)
        if not isinstance(data, dict):
            return ConnectionSettings()
        allowed = set(ConnectionSettings.__dataclass_fields__)
        return ConnectionSettings(**{k: v for k, v in data.items() if k in allowed})

    def save_strategy_settings(self, settings: StrategySettings) -> None:
        self.set_json("strategy", asdict(settings))

    def load_strategy_settings(self) -> StrategySettings:
        data = self.get_json("strategy", None)
        if not isinstance(data, dict):
            return StrategySettings()
        allowed = set(StrategySettings.__dataclass_fields__)
        return StrategySettings(**{k: v for k, v in data.items() if k in allowed})

    def save_resume_checkpoint(
        self,
        connection: ConnectionSettings,
        strategy: StrategySettings,
        cycle: Optional[CycleState],
        *,
        reason: str,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        """Atomically persist the state needed for a later operator resume.

        Windows can request session termination without routing through the
        main-window close button. This transaction stores the latest editable
        settings, the current in-memory cycle, a durable checkpoint marker, and
        its audit event together. Reusing ``checkpoint_id`` is idempotent so a
        timeout fallback and the worker command cannot record the same shutdown
        twice if they race.
        """
        normalized_reason = str(reason or "application_shutdown").strip() or "application_shutdown"
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        if not normalized_checkpoint_id:
            raise ValueError("Resume checkpoint ID is required.")

        stage = cycle.stage.value if cycle is not None else ""
        resume_required = bool(cycle is not None and cycle.stage not in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED})
        checkpoint = {
            "checkpoint_id": normalized_checkpoint_id,
            "created_at": utc_now_iso(),
            "reason": normalized_reason,
            "active_cycle_id": cycle.id if cycle is not None else None,
            "active_cycle_stage": stage or None,
            "ticker": cycle.ticker if cycle is not None else None,
            "resume_required": resume_required,
        }
        event_message = (
            f"Resume checkpoint saved before {normalized_reason.replace('_', ' ')}; "
            "the active cycle stage and app-owned broker orders were preserved for recovery on next start."
            if resume_required
            else f"Application state checkpoint saved before {normalized_reason.replace('_', ' ')}."
        )
        created = False
        with self.connect() as con:
            # Serialize the idempotence check with the writes. This prevents a
            # delayed worker command and its timeout fallback from both logging
            # the same logical checkpoint when they race.
            con.execute("BEGIN IMMEDIATE")
            existing_row = con.execute(
                "SELECT value_json FROM app_settings WHERE key='last_resume_checkpoint'"
            ).fetchone()
            if existing_row is not None:
                try:
                    existing = json.loads(existing_row["value_json"])
                except (TypeError, json.JSONDecodeError):
                    existing = None
                if isinstance(existing, dict) and existing.get("checkpoint_id") == normalized_checkpoint_id:
                    return existing

            for key, value in (
                ("connection", asdict(connection)),
                ("strategy", asdict(strategy)),
                ("last_resume_checkpoint", checkpoint),
            ):
                con.execute(
                    """
                    INSERT INTO app_settings(key, value_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(value, default=self._json_default), checkpoint["created_at"]),
                )
            if cycle is not None:
                self._upsert_cycle_in_connection(con, cycle)
            con.execute(
                """
                INSERT INTO events(created_at, level, ticker, cycle_id, message, raw_json)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    checkpoint["created_at"],
                    "INFO",
                    cycle.ticker if cycle is not None else None,
                    cycle.id if cycle is not None else None,
                    event_message,
                    json.dumps(checkpoint, default=self._json_default),
                ),
            )
            created = True

        if created:
            try:
                self._append_human_event_log(
                    created_at=str(checkpoint["created_at"]),
                    level="INFO",
                    message=event_message,
                    ticker=cycle.ticker if cycle is not None else None,
                    cycle_id=cycle.id if cycle is not None else None,
                    raw=checkpoint,
                )
            except Exception:
                pass
        return checkpoint

    def _cycle_upsert_statement(self, cycle: CycleState) -> tuple[str, list[str], dict[str, Any]]:
        data = cycle.to_dict()
        data["reinvest_profits"] = int(bool(cycle.reinvest_profits))
        data["rth_only"] = int(bool(getattr(cycle, "rth_only", True)))
        data["protective_sell_enabled"] = int(bool(getattr(cycle, "protective_sell_enabled", False)))
        data["slippage_buffer_enabled"] = int(bool(getattr(cycle, "slippage_buffer_enabled", False)))
        data["hard_risk_limits_enabled"] = int(bool(getattr(cycle, "hard_risk_limits_enabled", False)))
        data["atr_adaptive_enabled"] = int(bool(getattr(cycle, "atr_adaptive_enabled", True)))
        data["atr_adapt_minimum_profit_enabled"] = int(bool(getattr(cycle, "atr_adapt_minimum_profit_enabled", True)))
        data["atr_block_new_buy_until_ready"] = int(bool(getattr(cycle, "atr_block_new_buy_until_ready", True)))
        data["atr_adapt_protective_sell_enabled"] = int(bool(getattr(cycle, "atr_adapt_protective_sell_enabled", False)))
        data["block_delayed_data_in_live"] = int(bool(getattr(cycle, "block_delayed_data_in_live", False)))
        for _bool_name in [
            "what_if_check_enabled",
            "stale_data_guard_enabled",
            "volatility_filter_enabled",
            "session_timing_guard_enabled",
            "recovery_required",
            "close_position_market_requested",
        ]:
            data[_bool_name] = int(bool(getattr(cycle, _bool_name, False)))
        data["protective_sell_cancel_requested"] = int(bool(getattr(cycle, "protective_sell_cancel_requested", False)))
        data["stop_after_current_cycle"] = int(bool(cycle.stop_after_current_cycle))
        columns = list(data.keys())
        placeholders = ",".join("?" for _ in columns)
        update_set = ",".join(f"{col}=excluded.{col}" for col in columns if col != "id")
        sql = f"""
            INSERT INTO cycles({','.join(columns)}) VALUES({placeholders})
            ON CONFLICT(id) DO UPDATE SET {update_set}
        """
        return sql, columns, data

    def _upsert_cycle_in_connection(self, con: sqlite3.Connection, cycle: CycleState) -> None:
        sql, columns, data = self._cycle_upsert_statement(cycle)
        con.execute(sql, tuple(data[col] for col in columns))

    def upsert_cycle(self, cycle: CycleState) -> None:
        with self.connect() as con:
            self._upsert_cycle_in_connection(con, cycle)

    def _row_to_cycle(self, row: sqlite3.Row) -> CycleState:
        data = dict(row)
        data.setdefault("primary_exchange", "")
        data.setdefault("rth_only", 1)
        data["reinvest_profits"] = bool(data["reinvest_profits"])
        data["rth_only"] = bool(data["rth_only"])
        for key in [
            "protective_sell_enabled",
            "slippage_buffer_enabled",
            "hard_risk_limits_enabled",
            "atr_adaptive_enabled",
            "atr_adapt_minimum_profit_enabled",
            "atr_block_new_buy_until_ready",
            "atr_adapt_protective_sell_enabled",
            "block_delayed_data_in_live",
            "protective_sell_cancel_requested",
            "what_if_check_enabled",
            "stale_data_guard_enabled",
            "volatility_filter_enabled",
            "session_timing_guard_enabled",
            "recovery_required",
            "close_position_market_requested",
        ]:
            if key in data:
                data[key] = bool(data[key])
        data["stop_after_current_cycle"] = bool(data["stop_after_current_cycle"])
        return CycleState.from_dict(data)

    def get_cycle(self, cycle_id: str) -> Optional[CycleState]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        return self._row_to_cycle(row) if row else None

    def get_latest_active_cycle(self, ticker: Optional[str] = None) -> Optional[CycleState]:
        """Return the newest cycle that still needs attention.

        ERROR and MANUAL_REVIEW are included deliberately. A restart should not
        hide a problematic cycle just because it is no longer actively trading.
        """
        active_stages = (
            Stage.WAIT_INITIAL_DROP.value,
            Stage.BUY_TRAIL_ACTIVE.value,
            Stage.WAIT_RISE_TRIGGER.value,
            Stage.SELL_TRAIL_ACTIVE.value,
            Stage.MANUAL_REVIEW.value,
            Stage.ERROR.value,
        )
        params: list[Any] = list(active_stages)
        query = f"SELECT * FROM cycles WHERE stage IN ({','.join('?' for _ in active_stages)})"
        if ticker:
            query += " AND ticker=?"
            params.append(ticker.upper())
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self.connect() as con:
            row = con.execute(query, tuple(params)).fetchone()
        return self._row_to_cycle(row) if row else None

    def get_app_owned_unsold_position(self, ticker: str) -> dict[str, Any]:
        """Return locally tracked unsold shares created by this app.

        IBKR account positions can combine app trades with manual or external
        holdings.  BUY gating must therefore use the app's own persisted fill
        ledger rather than the account-wide broker position.  Cycles explicitly
        marked manually handled are excluded because the operator has confirmed
        that their recovery state was resolved outside the app.
        """
        normalized = str(ticker or "").strip().upper()
        if not normalized:
            return {"quantity": 0, "cycles": []}
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT
                    c.id,
                    c.cycle_number,
                    c.stage,
                    c.buy_filled_qty,
                    c.sell_filled_qty,
                    c.protective_sell_filled_qty,
                    c.error_message,
                    EXISTS(
                        SELECT 1
                        FROM decision_events d
                        WHERE d.cycle_id=c.id AND d.event_type='MANUALLY_HANDLED'
                    ) AS manually_handled
                FROM cycles c
                WHERE c.ticker=? AND COALESCE(c.buy_filled_qty, 0) > 0
                ORDER BY c.cycle_number ASC
                """,
                (normalized,),
            ).fetchall()

        total = 0
        cycles: list[dict[str, Any]] = []
        for row in rows:
            message = str(row["error_message"] or "").lower()
            if bool(row["manually_handled"]) or "marked manually handled" in message:
                continue
            bought = max(0, int(row["buy_filled_qty"] or 0))
            final_sold = max(0, int(row["sell_filled_qty"] or 0))
            protective_sold = max(0, int(row["protective_sell_filled_qty"] or 0))
            remaining = max(0, bought - max(final_sold, protective_sold))
            if remaining <= 0:
                continue
            total += remaining
            cycles.append(
                {
                    "cycle_id": str(row["id"]),
                    "cycle_number": int(row["cycle_number"] or 0),
                    "stage": str(row["stage"] or ""),
                    "quantity": remaining,
                }
            )
        return {"quantity": total, "cycles": cycles}

    def get_next_cycle_number(self, ticker: str) -> int:
        with self.connect() as con:
            row = con.execute("SELECT COALESCE(MAX(cycle_number), 0) AS n FROM cycles WHERE ticker=?", (ticker.upper(),)).fetchone()
        return int(row["n"] or 0) + 1

    def get_realized_net_profit_for_ticker(self, ticker: str) -> float:
        """Return completed-cycle net P/L for one ticker.

        This value feeds the reinvest-profits option. The strategy engine later
        clamps negative totals so losses do not reduce the user's base budget.
        """
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COALESCE(SUM(net_pnl), 0) AS pnl
                FROM cycles
                WHERE ticker=? AND stage=?
                """,
                (ticker.upper(), Stage.CYCLE_COMPLETE.value),
            ).fetchone()
        return float(row["pnl"] or 0.0)

    def get_daily_net_pnl_for_ticker(self, ticker: str, day_utc: Optional[str] = None) -> float:
        """Return completed-cycle net P/L for one ticker on a UTC date.

        ``day_utc`` is YYYY-MM-DD. When omitted, today's UTC date is used.
        This feeds optional hard risk limits and is intentionally based on
        completed cycles recorded by this app, not the whole IBKR account.
        """
        day = day_utc or utc_now_iso()[:10]
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COALESCE(SUM(net_pnl), 0) AS pnl
                FROM cycles
                WHERE ticker=? AND stage=? AND substr(updated_at, 1, 10)=?
                """,
                (ticker.upper(), Stage.CYCLE_COMPLETE.value, day),
            ).fetchone()
        return float(row["pnl"] or 0.0)

    def get_daily_net_pnl_total(self, day_utc: Optional[str] = None) -> float:
        day = day_utc or utc_now_iso()[:10]
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COALESCE(SUM(net_pnl), 0) AS pnl
                FROM cycles
                WHERE stage=? AND substr(updated_at, 1, 10)=?
                """,
                (Stage.CYCLE_COMPLETE.value, day),
            ).fetchone()
        return float(row["pnl"] or 0.0)

    def get_completed_cycle_count_today(self, ticker: str, day_utc: Optional[str] = None) -> int:
        day = day_utc or utc_now_iso()[:10]
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS n
                FROM cycles
                WHERE ticker=? AND stage=? AND substr(updated_at, 1, 10)=?
                """,
                (ticker.upper(), Stage.CYCLE_COMPLETE.value, day),
            ).fetchone()
        return int(row["n"] or 0)

    def get_completed_cycle_count(self, ticker: str) -> int:
        """Count all completed cycles stored for one ticker."""
        with self.connect() as con:
            row = con.execute(
                """
                SELECT COUNT(*) AS n
                FROM cycles
                WHERE ticker=? AND stage=?
                """,
                (ticker.upper(), Stage.CYCLE_COMPLETE.value),
            ).fetchone()
        return int(row["n"] or 0)

    def get_consecutive_loss_count(self, ticker: str) -> int:
        """Count most recent completed losing cycles for one ticker."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT net_pnl
                FROM cycles
                WHERE ticker=? AND stage=?
                ORDER BY updated_at DESC
                LIMIT 100
                """,
                (ticker.upper(), Stage.CYCLE_COMPLETE.value),
            ).fetchall()
        count = 0
        for row in rows:
            if float(row["net_pnl"] or 0.0) < 0:
                count += 1
            else:
                break
        return count


    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            number = float(value)
            if number != number or number in (float("inf"), float("-inf")):
                return None
            return number
        except Exception:
            return None

    @classmethod
    def _add_history_metrics(cls, row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        qty = cls._safe_float(enriched.get("buy_filled_qty")) or 0.0
        buy = cls._safe_float(enriched.get("avg_buy_price"))
        sell = cls._safe_float(enriched.get("avg_sell_price"))
        gross = cls._safe_float(enriched.get("gross_pnl"))
        net = cls._safe_float(enriched.get("net_pnl"))
        anchor = cls._safe_float(enriched.get("anchor_price"))
        sell_stop = cls._safe_float(enriched.get("sell_initial_trail_stop_price"))
        buy_cost = (buy or 0.0) * qty
        if buy and sell:
            enriched["sell_vs_buy_pct"] = ((sell / buy) - 1.0) * 100.0
        else:
            enriched["sell_vs_buy_pct"] = None
        if buy_cost > 0:
            enriched["gross_pnl_pct"] = ((gross or 0.0) / buy_cost) * 100.0
            enriched["net_pnl_pct"] = ((net or 0.0) / buy_cost) * 100.0
        else:
            enriched["gross_pnl_pct"] = None
            enriched["net_pnl_pct"] = None
        if anchor and buy:
            enriched["buy_vs_anchor_pct"] = ((buy / anchor) - 1.0) * 100.0
        else:
            enriched["buy_vs_anchor_pct"] = None
        if buy and sell_stop:
            enriched["initial_sell_stop_vs_buy_pct"] = ((sell_stop / buy) - 1.0) * 100.0
        else:
            enriched["initial_sell_stop_vs_buy_pct"] = None
        enriched["configured_min_profit_pct"] = enriched.get("rise_trigger_pct")
        enriched["configured_initial_drop_pct"] = enriched.get("initial_drop_pct")
        enriched["configured_buy_rebound_pct"] = enriched.get("buy_rebound_trail_pct")
        enriched["configured_sell_trail_pct"] = enriched.get("sell_trailing_stop_pct")
        enriched["protective_sell_enabled_display"] = "yes" if bool(enriched.get("protective_sell_enabled")) else "no"
        enriched["configured_protective_sell_trail_pct"] = enriched.get("protective_sell_trailing_stop_pct")
        enriched["slippage_buffer_enabled_display"] = "yes" if bool(enriched.get("slippage_buffer_enabled")) else "no"
        enriched["configured_slippage_buffer_pct"] = enriched.get("slippage_buffer_pct")
        return enriched

    def create_order_intent(
        self,
        *,
        cycle: CycleState,
        action: str,
        order_type: str,
        order_ref: str,
        quantity: int,
        trailing_percent: Optional[float],
        initial_stop_price: Optional[float],
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        """Durably record an app order intent before submitting to IBKR."""
        intent_raw = dict(raw or {})
        intent_raw.setdefault("intent_created", True)
        self.add_order(
            cycle=cycle,
            action=action,
            order_type=order_type,
            order_id=None,
            perm_id=None,
            order_ref=order_ref,
            quantity=quantity,
            trailing_percent=trailing_percent,
            initial_stop_price=initial_stop_price,
            status="INTENT_CREATED",
            raw=intent_raw,
        )

    def record_order_submission(
        self,
        *,
        cycle: CycleState,
        order_ref: str,
        order_id: Optional[int],
        perm_id: Optional[int],
        status: str,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist submitted cycle state and accepted broker identity atomically."""
        with self.connect() as con:
            self._upsert_cycle_in_connection(con, cycle)
            con.execute(
                """
                UPDATE orders
                SET status=?, order_id=COALESCE(?, order_id), perm_id=COALESCE(?, perm_id), updated_at=?, raw_json=?
                WHERE id = (SELECT id FROM orders WHERE order_ref=? ORDER BY id DESC LIMIT 1)
                """,
                (
                    status,
                    order_id,
                    perm_id,
                    utc_now_iso(),
                    json.dumps(raw or {}, default=self._json_default),
                    order_ref,
                ),
            )

    def mark_order_intent_failed(self, order_ref: str, error: str, raw: Optional[dict[str, Any]] = None) -> None:
        details = dict(raw or {})
        details["error"] = str(error)
        with self.connect() as con:
            con.execute(
                """
                UPDATE orders
                SET status=?, updated_at=?, raw_json=?
                WHERE id = (SELECT id FROM orders WHERE order_ref=? ORDER BY id DESC LIMIT 1)
                """,
                ("SUBMIT_FAILED", utc_now_iso(), json.dumps(details, default=self._json_default), order_ref),
            )

    def add_order(
        self,
        *,
        cycle: CycleState,
        action: str,
        order_type: str,
        order_id: Optional[int],
        perm_id: Optional[int],
        order_ref: str,
        quantity: int,
        trailing_percent: Optional[float],
        initial_stop_price: Optional[float],
        status: str,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO orders(
                    cycle_id, ticker, action, order_type, order_id, perm_id, order_ref,
                    quantity, trailing_percent, initial_stop_price, status,
                    created_at, updated_at, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cycle.id,
                    cycle.ticker,
                    action,
                    order_type,
                    order_id,
                    perm_id,
                    order_ref,
                    quantity,
                    trailing_percent,
                    initial_stop_price,
                    status,
                    now,
                    now,
                    json.dumps(raw or {}, default=self._json_default),
                ),
            )

    def update_order_status(self, order_ref: str, status: str, order_id: Optional[int] = None, perm_id: Optional[int] = None) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE orders
                SET status=?, order_id=COALESCE(?, order_id), perm_id=COALESCE(?, perm_id), updated_at=?
                WHERE id = (SELECT id FROM orders WHERE order_ref=? ORDER BY id DESC LIMIT 1)
                """,
                (status, order_id, perm_id, utc_now_iso(), order_ref),
            )

    def add_execution(
        self,
        *,
        cycle: Optional[CycleState],
        ticker: str,
        side: str,
        shares: float,
        price: float,
        avg_price: Optional[float] = None,
        commission: float = 0.0,
        currency: str = "USD",
        order_ref: Optional[str] = None,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        execution_id: Optional[str] = None,
        executed_at: Optional[str] = None,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO executions(
                    cycle_id, ticker, order_ref, order_id, perm_id, execution_id,
                    side, shares, price, avg_price, commission, currency, executed_at, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cycle.id if cycle else None,
                    ticker.upper(),
                    order_ref,
                    order_id,
                    perm_id,
                    execution_id,
                    side,
                    float(shares),
                    float(price),
                    avg_price,
                    float(commission or 0.0),
                    currency,
                    executed_at or utc_now_iso(),
                    json.dumps(raw or {}, default=self._json_default),
                ),
            )


    def execution_exists(self, execution_id: str) -> bool:
        if not execution_id:
            return False
        with self.connect() as con:
            row = con.execute(
                "SELECT 1 FROM executions WHERE execution_id=? LIMIT 1",
                (str(execution_id),),
            ).fetchone()
        return row is not None

    def add_event(self, level: str, message: str, ticker: Optional[str] = None, cycle_id: Optional[str] = None, raw: Optional[dict[str, Any]] = None) -> None:
        """Append an operator/audit event without letting logging crash trading.

        Most events reference an active cycle. During failure-injection tests,
        recovery, or unexpected shutdown/restart windows, a warning may be
        produced before the corresponding cycle row has been durably written.
        Rather than raising a foreign-key error from the logging path, drop the
        dangling cycle reference and retain the event at ticker/global scope.
        """
        created_at = utc_now_iso()
        persisted_cycle_id = cycle_id
        with self.connect() as con:
            if persisted_cycle_id:
                row = con.execute("SELECT 1 FROM cycles WHERE id=? LIMIT 1", (persisted_cycle_id,)).fetchone()
                if row is None:
                    persisted_cycle_id = None
            con.execute(
                """
                INSERT INTO events(created_at, level, ticker, cycle_id, message, raw_json)
                VALUES(?,?,?,?,?,?)
                """,
                (created_at, level.upper(), ticker.upper() if ticker else None, persisted_cycle_id, message, json.dumps(raw or {}, default=self._json_default)),
            )
        self._append_human_event_log(
            created_at=created_at,
            level=level,
            message=message,
            ticker=ticker,
            cycle_id=persisted_cycle_id,
            raw=raw,
        )

    def get_recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT created_at, level, ticker, cycle_id, message FROM events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows][::-1]

    def get_cycle_audit_bundle(self, cycle_id: str) -> dict[str, Any]:
        """Return all persisted records used to inspect one completed/active cycle.

        This is used by the history table detail dialog. It deliberately returns
        raw JSON payloads as parsed dictionaries when possible so a support or
        audit review can see broker callback details, order refs, executions,
        and decision events in one place.
        """
        def parse_json(value: Any) -> Any:
            if not value:
                return {}
            try:
                return json.loads(value)
            except Exception:
                return value

        with self.connect() as con:
            cycle_row = con.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            orders = con.execute(
                "SELECT * FROM orders WHERE cycle_id=? ORDER BY id ASC",
                (cycle_id,),
            ).fetchall()
            executions = con.execute(
                "SELECT * FROM executions WHERE cycle_id=? ORDER BY executed_at ASC, id ASC",
                (cycle_id,),
            ).fetchall()
            events = con.execute(
                "SELECT * FROM events WHERE cycle_id=? ORDER BY created_at ASC, id ASC",
                (cycle_id,),
            ).fetchall()
            decisions = con.execute(
                "SELECT * FROM decision_events WHERE cycle_id=? ORDER BY created_at ASC, id ASC",
                (cycle_id,),
            ).fetchall()
        def normalize(row: sqlite3.Row) -> dict[str, Any]:
            data = dict(row)
            if "raw_json" in data:
                data["raw"] = parse_json(data.pop("raw_json"))
            return data
        return {
            "cycle": normalize(cycle_row) if cycle_row else {},
            "orders": [normalize(row) for row in orders],
            "executions": [normalize(row) for row in executions],
            "events": [normalize(row) for row in events],
            "decision_events": [normalize(row) for row in decisions],
        }

    def add_broker_event(
        self,
        *,
        event_type: str,
        raw: dict[str, Any],
        ticker: Optional[str] = None,
        cycle_id: Optional[str] = None,
        order_ref: Optional[str] = None,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        execution_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        with self.connect() as con:
            persisted_cycle_id = cycle_id
            if persisted_cycle_id:
                row = con.execute("SELECT 1 FROM cycles WHERE id=? LIMIT 1", (persisted_cycle_id,)).fetchone()
                if row is None:
                    persisted_cycle_id = None
            con.execute(
                """
                INSERT INTO broker_events(created_at, event_type, ticker, cycle_id, order_ref, order_id, perm_id, execution_id, raw_json)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    created_at or utc_now_iso(),
                    str(event_type or "").upper(),
                    ticker.upper() if ticker else None,
                    persisted_cycle_id,
                    order_ref,
                    order_id,
                    perm_id,
                    execution_id,
                    json.dumps(raw or {}, default=self._json_default),
                ),
            )

    def recent_broker_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM broker_events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        result = []
        for row in rows[::-1]:
            item = dict(row)
            try:
                item["raw"] = json.loads(item.pop("raw_json") or "{}")
            except Exception:
                item["raw"] = item.pop("raw_json", "")
            result.append(item)
        return result

    def add_decision_event(
        self,
        *,
        event_type: str,
        message: str,
        cycle: Optional[CycleState] = None,
        stage_before: Optional[str] = None,
        stage_after: Optional[str] = None,
        decision_result: str = "",
        broker_order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append a structured audit event used to reconstruct strategy decisions."""
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO decision_events(
                    created_at, event_type, ticker, cycle_id, stage_before, stage_after,
                    decision_result, message, broker_order_id, perm_id, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    utc_now_iso(),
                    str(event_type).upper(),
                    cycle.ticker if cycle else None,
                    cycle.id if cycle else None,
                    stage_before,
                    stage_after,
                    decision_result,
                    message,
                    broker_order_id,
                    perm_id,
                    json.dumps(raw or {}, default=self._json_default),
                ),
            )

    def backup_database(self, reason: str = "manual", keep: int = 50) -> Optional[Path]:
        """Create a consistent SQLite backup and prune older backups.

        The application runs SQLite in WAL mode. Copying only the main .sqlite
        file can miss recently committed pages that still live in the -wal file.
        The sqlite3 online backup API produces a single, consistent backup file
        without needing to reason about WAL/shm sidecar files.
        """
        if not self.db_path.exists():
            return None
        backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(reason))[:40] or "backup"
        stamp = _backup_stamp()
        target = backup_dir / f"bot_state_{stamp}_{safe_reason}.sqlite"
        try:
            with sqlite3.connect(self.db_path, factory=_ClosingSqliteConnection) as source, sqlite3.connect(target, factory=_ClosingSqliteConnection) as dest:
                source.execute("PRAGMA wal_checkpoint(PASSIVE)")
                source.backup(dest)
        except Exception:
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            return None
        validation = self.validate_restore_candidate(target)
        if not validation.get("ok"):
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        try:
            (backup_dir / "latest_restore_validation.json").write_text(
                json.dumps(validation, indent=2, sort_keys=True, default=self._json_default) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        backups = sorted(backup_dir.glob("bot_state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[int(keep):]:
            try:
                old.unlink()
            except Exception:
                pass
        return target

    def list_database_backups(self) -> list[Path]:
        """Return known SQLite backup files newest-first."""
        backup_dir = self.db_path.parent / "backups"
        if not backup_dir.exists():
            return []
        return sorted(backup_dir.glob("bot_state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)

    @staticmethod
    def _validate_sqlite_database_file(path: Path) -> dict[str, Any]:
        """Validate that a SQLite file is internally consistent and app-shaped.

        This is intentionally a restore-readiness check, not a restore action. It
        opens the file read-only, runs SQLite integrity_check, and verifies the
        core tables that a restored bot database must contain.
        """
        target = Path(path)
        result: dict[str, Any] = {
            "path": str(target),
            "exists": target.exists(),
            "ok": False,
            "integrity_check": None,
            "missing_tables": [],
            "error": "",
        }
        if not target.exists():
            result["error"] = "file does not exist"
            return result
        required_tables = {"app_settings", "cycles", "orders", "executions", "events", "decision_events"}
        try:
            uri = f"file:{target.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, factory=_ClosingSqliteConnection) as con:
                integrity = con.execute("PRAGMA integrity_check").fetchone()
                integrity_text = str(integrity[0] if integrity else "")
                result["integrity_check"] = integrity_text
                rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                existing = {str(row[0]) for row in rows}
                missing = sorted(required_tables - existing)
                result["missing_tables"] = missing
                result["ok"] = integrity_text.lower() == "ok" and not missing
        except Exception as exc:
            result["error"] = str(exc)
        return result

    def validate_backup(self, backup_path: Path) -> dict[str, Any]:
        """Return restore-readiness details for one backup file."""
        return self._validate_sqlite_database_file(Path(backup_path))

    def validate_latest_backup(self) -> dict[str, Any]:
        """Validate the newest rotated backup without modifying the active DB."""
        backups = self.list_database_backups()
        if not backups:
            return {"path": "", "exists": False, "ok": False, "integrity_check": None, "missing_tables": [], "error": "no backups found"}
        return self.validate_backup(backups[0])

    def validate_restore_candidate(self, backup_path: Path) -> dict[str, Any]:
        """Copy a backup to a temporary file and validate the copy as a restore candidate.

        A restore should not be attempted from a backup that cannot survive this
        copy-and-open cycle. The active database is never overwritten here.
        """
        backup = Path(backup_path)
        result = self.validate_backup(backup)
        if not result.get("ok"):
            return result
        try:
            with tempfile.TemporaryDirectory(prefix="ibkr_bot_restore_validate_") as tmp:
                candidate = Path(tmp) / "restore_candidate.sqlite"
                shutil.copy2(backup, candidate)
                copied = self._validate_sqlite_database_file(candidate)
                result["restore_copy_validated"] = bool(copied.get("ok"))
                if not copied.get("ok"):
                    result["ok"] = False
                    result["error"] = copied.get("error") or "restore candidate copy failed validation"
        except Exception as exc:
            result["ok"] = False
            result["restore_copy_validated"] = False
            result["error"] = str(exc)
        return result

    def create_audit_export_bundle(self, target_dir: Optional[Path] = None, snapshot: Optional[dict[str, Any]] = None) -> Path:
        """Create a support/audit ZIP with DB, logs, backups, and recovery facts.

        The bundle is read-only diagnostic material. It does not alter strategy
        state and it does not contact IBKR/TWS.
        """
        root = Path(target_dir) if target_dir is not None else self.db_path.parent / "audit_exports"
        root.mkdir(parents=True, exist_ok=True)
        stamp = _backup_stamp()
        target = root / f"ibkr_bot_audit_bundle_{stamp}.zip"
        snapshot_data = dict(snapshot or {})
        backup = self.backup_database("audit_export")
        latest_backup_validation = self.validate_backup(backup) if backup else {"ok": False, "error": "backup could not be created"}
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "created_at": utc_now_iso(),
                "db_path": str(self.db_path),
                "backup": str(backup) if backup else "",
                "backup_validation": latest_backup_validation,
                "snapshot_included": bool(snapshot_data),
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True, default=self._json_default))
            if snapshot_data:
                zf.writestr("snapshot.json", json.dumps(snapshot_data, indent=2, sort_keys=True, default=self._json_default))
            if backup and backup.exists():
                zf.write(backup, "database/bot_state_backup.sqlite")
            elif self.db_path.exists():
                # Fallback for unusual cases where online backup failed but the
                # active DB file exists. The manifest records the failed backup.
                zf.write(self.db_path, "database/bot_state_unvalidated.sqlite")
            debug_dir = self.debug_reports_dir()
            for name in ["latest_state_report.txt", "audit_events_readable.log"]:
                path = debug_dir / name
                if path.exists():
                    zf.write(path, f"debug_reports/{name}")
            with self.connect() as con:
                for table in ["cycles", "orders", "executions", "events", "decision_events", "broker_events"]:
                    rows = con.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 2000").fetchall()
                    payload = [dict(row) for row in rows]
                    zf.writestr(f"sqlite_exports/{table}.json", json.dumps(payload, indent=2, sort_keys=True, default=self._json_default))
            recent_events = self.get_recent_events(500)
            zf.writestr("recent_events.json", json.dumps(recent_events, indent=2, sort_keys=True, default=self._json_default))
        return target

    def history_summary(self, ticker: str = "") -> dict[str, Any]:
        ticker_key = str(ticker or "").strip().upper()
        params: list[Any] = [Stage.CYCLE_COMPLETE.value]
        where = "stage=?"
        if ticker_key:
            where += " AND ticker=?"
            params.append(ticker_key)
        with self.connect() as con:
            meta = con.execute(
                f"SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS max_updated FROM cycles WHERE {where}",
                tuple(params),
            ).fetchone()
            count = int(meta["n"] or 0) if meta else 0
            max_updated = str(meta["max_updated"] or "") if meta else ""
            cached = self._history_summary_cache.get(ticker_key)
            if cached and cached[0] == count and cached[1] == max_updated:
                return dict(cached[2])
            if count <= 0:
                result = {
                    "cycles": 0,
                    "win_rate_pct": None,
                    "avg_net_pct": None,
                    "median_net_pct": None,
                    "best_net_pnl": None,
                    "worst_net_pnl": None,
                    "total_net_pnl": 0.0,
                    "total_commissions": 0.0,
                    "max_consecutive_losses": 0,
                    "avg_holding_minutes": None,
                    "max_completed_drawdown": 0.0,
                }
                self._history_summary_cache[ticker_key] = (count, max_updated, dict(result))
                return result
            rows = con.execute(
                f"""
                SELECT net_pnl, buy_commission, sell_commission, buy_filled_qty, avg_buy_price,
                       buy_filled_at, sell_filled_at
                FROM cycles
                WHERE {where}
                ORDER BY sell_filled_at DESC, updated_at DESC
                """,
                tuple(params),
            ).fetchall()
        net_values = [float(row["net_pnl"] or 0.0) for row in rows]
        net_pcts: list[float] = []
        commissions = 0.0
        hold_minutes: list[float] = []
        for row in rows:
            qty = self._safe_float(row["buy_filled_qty"]) or 0.0
            buy = self._safe_float(row["avg_buy_price"])
            net = self._safe_float(row["net_pnl"]) or 0.0
            buy_cost = (buy or 0.0) * qty
            if buy_cost > 0:
                net_pcts.append((net / buy_cost) * 100.0)
            commissions += float(row["buy_commission"] or 0.0) + float(row["sell_commission"] or 0.0)
            buy_at = _parse_dt(row["buy_filled_at"])
            sell_at = _parse_dt(row["sell_filled_at"])
            if buy_at and sell_at and sell_at >= buy_at:
                hold_minutes.append((sell_at - buy_at).total_seconds() / 60.0)
        wins = sum(1 for x in net_values if x > 0)
        sorted_pcts = sorted(net_pcts)
        if sorted_pcts:
            mid = len(sorted_pcts) // 2
            median = sorted_pcts[mid] if len(sorted_pcts) % 2 else (sorted_pcts[mid - 1] + sorted_pcts[mid]) / 2.0
            avg_pct = sum(sorted_pcts) / len(sorted_pcts)
        else:
            median = None
            avg_pct = None
        max_loss_streak = 0
        streak = 0
        for pnl in net_values:
            if pnl < 0:
                streak += 1
                max_loss_streak = max(max_loss_streak, streak)
            else:
                streak = 0
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in reversed(net_values):
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        result = {
            "cycles": len(rows),
            "win_rate_pct": wins / len(rows) * 100.0,
            "avg_net_pct": avg_pct,
            "median_net_pct": median,
            "best_net_pnl": max(net_values),
            "worst_net_pnl": min(net_values),
            "total_net_pnl": sum(net_values),
            "total_commissions": commissions,
            "max_consecutive_losses": max_loss_streak,
            "avg_holding_minutes": sum(hold_minutes) / len(hold_minutes) if hold_minutes else None,
            "max_completed_drawdown": max_drawdown,
        }
        self._history_summary_cache[ticker_key] = (count, max_updated, dict(result))
        return result

    def history_cycles(self, ticker: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Return completed cycles with derived percentage metrics for the UI.

        The raw cycle table stores prices/P&L. This method adds display-only
        percentages so the history table and CSV exports can show both absolute
        and normalized performance.
        """
        params: list[Any] = []
        query = "SELECT * FROM cycles WHERE stage=?"
        params.append(Stage.CYCLE_COMPLETE.value)
        if ticker.strip():
            query += " AND ticker=?"
            params.append(ticker.strip().upper())
        query += " ORDER BY sell_filled_at DESC, updated_at DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as con:
            rows = con.execute(query, tuple(params)).fetchall()
        return [self._add_history_metrics(dict(row)) for row in rows]

    def cycle_audit_details(self, cycle_id: str) -> dict[str, Any]:
        """Return one cycle plus all app-owned audit records tied to it.

        This powers the Trade History click-through dialog. It deliberately
        reads from local SQLite only; no broker/API calls are made from the GUI
        when inspecting completed-cycle history.
        """
        cycle_id = str(cycle_id or "").strip()
        if not cycle_id:
            return {"cycle": None, "orders": [], "executions": [], "events": [], "decision_events": []}
        with self.connect() as con:
            cycle = con.execute("SELECT * FROM cycles WHERE id=?", (cycle_id,)).fetchone()
            orders = con.execute("SELECT * FROM orders WHERE cycle_id=? ORDER BY id ASC", (cycle_id,)).fetchall()
            executions = con.execute("SELECT * FROM executions WHERE cycle_id=? ORDER BY executed_at ASC, id ASC", (cycle_id,)).fetchall()
            events = con.execute("SELECT * FROM events WHERE cycle_id=? ORDER BY created_at ASC, id ASC", (cycle_id,)).fetchall()
            decisions = con.execute("SELECT * FROM decision_events WHERE cycle_id=? ORDER BY created_at ASC, id ASC", (cycle_id,)).fetchall()
        return {
            "cycle": dict(cycle) if cycle else None,
            "orders": [dict(row) for row in orders],
            "executions": [dict(row) for row in executions],
            "events": [dict(row) for row in events],
            "decision_events": [dict(row) for row in decisions],
        }

    def export_history_csv(self, target: Path, ticker: str = "") -> Path:
        rows = self.history_cycles(ticker=ticker, limit=100000)
        target.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "ticker",
            "cycle_number",
            "buy_filled_at",
            "sell_filled_at",
            "buy_filled_qty",
            "avg_buy_price",
            "avg_sell_price",
            "buy_commission",
            "sell_commission",
            "gross_pnl",
            "gross_pnl_pct",
            "net_pnl",
            "net_pnl_pct",
            "sell_vs_buy_pct",
            "buy_vs_anchor_pct",
            "initial_sell_stop_vs_buy_pct",
            "configured_min_profit_pct",
            "configured_initial_drop_pct",
            "configured_buy_rebound_pct",
            "configured_sell_trail_pct",
            "protective_sell_enabled_display",
            "configured_protective_sell_trail_pct",
            "slippage_buffer_enabled_display",
            "configured_slippage_buffer_pct",
            "reinvested_profit",
            "budget",
            "buy_order_ref",
            "sell_order_ref",
            "buy_perm_id",
            "sell_perm_id",
        ]
        with target.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name) for name in fieldnames})
        return target
