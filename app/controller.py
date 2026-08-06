"""Worker-thread coordinator for strategy, broker, storage, and GUI state.

The controller persists editable settings, maintains price/ATR diagnostics,
evaluates entry and exit guards, advances the pure strategy engine, executes
app-owned broker actions, records audit data, and reconciles persisted state with
TWS or IB Gateway after startup and reconnect.

Design invariants used throughout this file:

* broker side effects occur only in the worker path;
* cancellation and recovery require exact OrderRefs persisted by this installation;
* a stored active cycle requires explicit operator Start/resume after launch;
* external account positions do not replace the app-owned fill ledger;
* live-cycle edits are applied only when safe for the current stage;
* uncertain connection, RTH, price, order, or recovery facts fail closed.
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from collections import deque
from copy import copy
from dataclasses import asdict
from datetime import datetime, timezone
from math import ceil, floor, isfinite
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

if os.environ.get("IBKR_BOT_HEADLESS_SIGNALS") == "1":
    class QObject:  # type: ignore[no-redef]
        pass

    # Signal.__get__ instantiates the module-level _HeadlessSignalInstance
    # defined after the imports below; a duplicate inline definition here was
    # dead code shadowed by that later definition.
    class Signal:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.name = ""

        def __set_name__(self, owner: type, name: str) -> None:
            self.name = name

        def __get__(self, instance: Any, owner: type | None = None) -> Any:
            if instance is None:
                return self
            signal = instance.__dict__.get(self.name)
            if signal is None:
                signal = _HeadlessSignalInstance()
                instance.__dict__[self.name] = signal
            return signal
else:
    from PySide6.QtCore import QObject, Signal

from .ib_adapter import (
    BrokerAdapterError,
    IbAsyncTwsAdapter,
    MarketPriceSnapshot,
    PolledOrderState,
    QualifiedContract,
    RthStatus,
)
from .ib_platform import connection_helper_text, launch_platform, platform_label
from .market_data_capture import MarketDataCaptureManager
from .models import (
    SUPPORTED_CONTRACT_CURRENCIES,
    ConnectionSettings,
    CycleState,
    Stage,
    StopAction,
    StrategySettings,
    normalize_contract_currency,
    recovery_cycle_signature,
    strategy_with_atr_adaptive_percentages,
    utc_now_iso,
)
from .order_diagnostics import native_trailing_order_diagnostics
from .paths import database_path, debug_captures_dir, exports_dir
from .storage import BotStorage
from .strategy import StrategyAction, StrategyEngine, make_order_ref
from .watchdog import append_emergency_log


class _HeadlessSignalInstance:
    """Tiny Signal-compatible object used only for non-GUI build/test runs."""

    def __init__(self) -> None:
        self.emissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emissions.append((args, kwargs))
        for slot in list(self._slots):
            slot(*args, **kwargs)


if os.environ.get("IBKR_BOT_HEADLESS_SIGNALS") == "1":
    class ControllerSignals:
        """Headless signal namespace for Windows build-test gating.

        The packaged GUI is still built with real PySide6 signals. The build
        script enables this mode only while running tests so rich Python
        snapshot dictionaries never pass through PySide's meta-type conversion.
        """

        def __init__(self) -> None:
            self.snapshot_updated = _HeadlessSignalInstance()
            self.history_updated = _HeadlessSignalInstance()
            self.event_logged = _HeadlessSignalInstance()
            self.connection_changed = _HeadlessSignalInstance()
            self.contract_search_updated = _HeadlessSignalInstance()
            self.ticker_search_updated = _HeadlessSignalInstance()
else:
    class ControllerSignals(QObject):
        # Use object payloads for Python containers. Signal(dict/list) asks PySide to
        # convert nested payloads to Qt meta-types, which is brittle in tests and can
        # produce platform-specific conversion warnings for rich snapshots.
        snapshot_updated = Signal(object)
        history_updated = Signal(object)
        event_logged = Signal(str)
        connection_changed = Signal(bool, str)
        contract_search_updated = Signal(object)
        ticker_search_updated = Signal(object)


class TradingController:
    """Owns the live worker loop and coordinates state transitions.

    Public methods enqueue commands from the GUI thread. Private methods run in
    the worker thread and are the only places that touch the broker adapter.
    """

    # Worker responsibilities run on independent monotonic cadences. GUI commands
    # remain event-driven because Queue.get() wakes as soon as a command arrives;
    # the bounded idle wait exists only so broker callbacks are pumped regularly
    # even when the GUI is idle.
    BROKER_CADENCE_SECONDS = 0.05
    STRATEGY_CADENCE_SECONDS = 0.10
    GUI_CADENCE_SECONDS = 0.50
    DATABASE_CADENCE_SECONDS = 1.00
    MAINTENANCE_CADENCE_SECONDS = 1.00
    MAX_IDLE_WAIT_SECONDS = 0.25
    RECONNECT_INTERVAL_SECONDS = 10.0
    BUY_PREFLIGHT_AUDIT_THROTTLE_SECONDS = 60.0
    STORAGE_FAULT_PROBE_INTERVAL_SECONDS = 2.0
    STAGE3_SELL_CONFIRMATIONS_REQUIRED = 2
    DIAGNOSTIC_FIRST_SUMMARY_SECONDS = 60.0
    DIAGNOSTIC_REPEAT_SUMMARY_SECONDS = 5.0 * 60.0
    NATIVE_ORDER_WAIT_SUMMARY_SECONDS = 15.0 * 60.0
    STAGE3_NEAR_TRIGGER_PCT = 0.5
    # A positive partial fill proves that a native TRAIL BUY has triggered, or
    # that a zero-trail MKT BUY is executing. Allow normal multi-print marketable
    # execution a brief chance to finish before cancelling the working remainder.
    BUY_PARTIAL_FILL_GRACE_SECONDS = 3.0

    # Zero means do not add a second rate limit inside the strategy cadence when
    # reading the cached TWS subscription handle. The adapter stamps actual
    # pending-ticker events; repeated cached reads may update diagnostics but
    # cannot refresh data age or advance strategy logic.
    PRICE_POLL_INTERVAL_SECONDS = 0.0
    STALE_ACTIVE_CYCLE_SECONDS = 12 * 60 * 60
    ATR_WARMUP_BLOCK_PREFIX = "ATR warmup guard blocked BUY:"
    ATR_CALCULATION_INTERVAL_SECONDS = 0.25

    def __init__(self, storage: Optional[BotStorage] = None):
        self.storage = storage or BotStorage(database_path())
        self.signals = ControllerSignals()
        self._commands: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, name="IBKRBotWorker", daemon=True)
        self._thread_started = False
        self._shutdown_complete = threading.Event()
        self._worker_started_at = utc_now_iso()
        self._worker_last_heartbeat_monotonic = time.monotonic()
        self._worker_current_operation = "initializing"
        self._worker_operation_started_monotonic = self._worker_last_heartbeat_monotonic
        self._worker_snapshot_sequence = 0
        self._worker_failure_message = ""
        self._worker_failure_at = ""

        self.connection = self.storage.load_connection_settings()
        self.strategy = self.storage.load_strategy_settings()
        self.adapter = IbAsyncTwsAdapter()
        self.connected = False
        self.status = "Disconnected"
        self._storage_fault_active = False
        self._storage_fault_message = ""
        self._storage_fault_operation = ""
        self._storage_fault_started_at = ""
        self._storage_fault_started_monotonic = 0.0
        self._storage_fault_last_probe_at = ""
        self._storage_fault_last_probe_monotonic = 0.0
        self._storage_fault_probe_succeeded = False
        self._storage_fault_restart_recommended = False
        self._storage_fault_repeat_log_monotonic = 0.0
        self._watchdog_auto_recovery_request: Optional[dict[str, Any]] = None
        self._watchdog_auto_recovery_pending = False
        self.active_cycle: Optional[CycleState] = self.storage.get_latest_active_cycle()
        self.contract: Optional[QualifiedContract] = None
        self._last_snapshot_emit = 0.0
        self._last_snapshot_payload: dict[str, Any] = {}
        self._last_database_refresh_monotonic = 0.0
        self._snapshot_database_cache: dict[str, Any] = {
            "ticker": "",
            "recent_events": [],
            "history_summary": {},
            "guard_facts": {},
            "database_currency": {},
            "errors": {},
        }
        self._executions_recorded: set[str] = set()
        self._pending_commissions_by_execution_id: dict[str, dict[str, Any]] = {}
        self._commission_currency_mismatch_keys: set[str] = set()
        self._last_price_warning_at: dict[str, float] = {}
        self._audit_conditions: dict[str, dict[str, Any]] = {}
        self._stage3_quote_guard_status: dict[str, Any] = {}
        # Routine Stage-3 quote invalidity can alternate with fresh quotes many
        # times in one cycle.  Keep the one informational "condition started"
        # row to at most once per cycle; persistent summaries and all safety-
        # relevant warnings remain independently eligible.
        self._stage3_routine_guard_entry_cycle_id = ""
        self.price_snapshot: Optional[dict[str, Any]] = None
        self._last_price_poll_monotonic = 0.0
        self._api_data_seen_count = 0
        self._api_data_change_count = 0
        self._api_last_data_monotonic = 0.0
        self._api_last_data_wall_time = ""
        self._api_last_change_monotonic = 0.0
        self._api_last_change_wall_time = ""
        self._api_last_field_signature: tuple[tuple[str, Any], ...] | None = None
        self._last_market_data_event_token: tuple[str, int] | None = None
        self._api_data_invalidated = True
        self._api_data_invalidated_reason = "Waiting for the first fresh market-data update."
        self._api_data_invalidated_at = utc_now_iso()
        self._stage3_sell_confirmation: dict[str, Any] = {}

        self._broker_connectivity: dict[str, Any] = {
            "local_connected": False,
            "upstream_connected": False,
            "state": "local_disconnected",
            "message": "The local API connection to IB Gateway/TWS is disconnected.",
            "error_code": None,
            "changed_at": utc_now_iso(),
            "market_data_resubscribe_required": False,
            "awaiting_fresh_market_data": False,
            "market_data_event_tracking": False,
            "trading_ready": False,
        }
        self._broker_connectivity_initialized = False
        self._last_broker_refresh_monotonic = 0.0
        self._upstream_recovery_pending = False
        self._last_upstream_recovery_attempt_monotonic = 0.0
        self._auto_reconnect_enabled = False
        self._last_reconnect_attempt_monotonic = 0.0
        self._reconnect_failures = 0
        self._latest_rth_status: Optional[dict[str, Any]] = None
        self._price_history: "deque[tuple[float, float]]" = deque(maxlen=21600)
        # The price-history deque retains every bounded-session observation;
        # fixed-time RTH OHLC bars keep ATR work bounded per quote.
        self._atr_bars: "deque[dict[str, float]]" = deque(maxlen=512)
        self._atr_bar_seconds_cache = 0
        self._atr_history_signature: tuple[Any, ...] = (0, None, None)
        self._market_capture = MarketDataCaptureManager(debug_captures_dir(), pre_window_seconds=15*60, post_window_seconds=15*60, async_writes=True)
        self._last_atr_snapshot: dict[str, Any] = {}
        self._last_atr_snapshot_monotonic = 0.0
        self._last_atr_snapshot_config: tuple[int, int, bool, bool] | None = None
        self._last_atr_adaptive_values: dict[str, float] = {}
        self._last_human_report_monotonic = 0.0
        self._recovery_required = False
        self._last_recovery_probe: dict[str, Any] = {}
        self._last_successful_recovery_refresh_at = ""
        self._broker_display_accounts: list[str] = []
        self._startup_resume_required = self._cycle_needs_operator_start(self.active_cycle)
        self._stale_active_cycle_detected = self._active_cycle_is_stale(self.active_cycle)
        if self._stale_active_cycle_detected and self.active_cycle is not None:
            self._recovery_required = True
            self.storage.add_event(
                "WARN",
                "Startup detected a stale active cycle. Broker/local reconciliation is required before monitoring resumes.",
                ticker=self.active_cycle.ticker,
                cycle_id=self.active_cycle.id,
                raw={
                    "cycle_updated_at": self.active_cycle.updated_at,
                    "stale_after_seconds": self.STALE_ACTIVE_CYCLE_SECONDS,
                },
            )

    @staticmethod
    def _cycle_needs_operator_start(cycle: Optional[CycleState]) -> bool:
        if cycle is None:
            return False
        return cycle.stage not in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED}

    @classmethod
    def _active_cycle_stale_age_seconds(cls, cycle: Optional[CycleState]) -> Optional[float]:
        if cycle is None or cycle.stage in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED}:
            return None
        try:
            updated = datetime.fromisoformat(str(cycle.updated_at).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except Exception:
            return float("inf")
        return max(0.0, (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds())

    @classmethod
    def _active_cycle_is_stale(cls, cycle: Optional[CycleState]) -> bool:
        age = cls._active_cycle_stale_age_seconds(cycle)
        return bool(age is not None and age > cls.STALE_ACTIVE_CYCLE_SECONDS)

    def _refresh_stale_active_cycle_flag(self) -> None:
        self._stale_active_cycle_detected = self._active_cycle_is_stale(self.active_cycle)
        if self._stale_active_cycle_detected:
            self._recovery_required = True

    def _refresh_display_accounts(self) -> None:
        """Refresh broker account IDs for GUI display only.

        ConnectionSettings.account is an optional routing override. When it is
        blank, broker orders leave Order.account unset so TWS/IB Gateway can
        select the account. This display cache lets the top status bar show the
        account reported by the connected platform without forcing that value
        into subsequent orders.
        """
        accounts: list[str] = []
        method = getattr(self.adapter, "managed_accounts", None)
        if callable(method):
            try:
                accounts = [str(item).strip() for item in (method() or []) if str(item).strip()]
            except Exception as exc:
                self._log("WARN", f"Could not read managed account list for status display: {exc}")
                accounts = []
        seen: set[str] = set()
        self._broker_display_accounts = []
        for account in accounts:
            if account in seen:
                continue
            seen.add(account)
            self._broker_display_accounts.append(account)

    def _remember_recovery_account_values(self, values: Any) -> None:
        """Add account IDs seen in broker recovery facts to the display cache."""
        if str(getattr(self.connection, "account", "") or "").strip():
            return
        if isinstance(values, (str, bytes)) or values is None:
            iterable = [values]
        else:
            try:
                iterable = list(values)
            except TypeError:
                iterable = [values]
        seen = set(self._broker_display_accounts)
        for value in iterable:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                self._broker_display_accounts.append(text)

    def _display_account_label(self) -> str:
        configured = str(getattr(self.connection, "account", "") or "").strip()
        if configured:
            return configured
        if self.active_cycle and str(self.active_cycle.account or "").strip():
            return str(self.active_cycle.account).strip()
        if len(self._broker_display_accounts) == 1:
            return self._broker_display_accounts[0]
        if len(self._broker_display_accounts) > 1:
            first = self._broker_display_accounts[0]
            return f"{first} +{len(self._broker_display_accounts) - 1} accounts"
        return ""

    @property
    def db_path(self) -> Path:
        return self.storage.db_path

    def worker_is_alive(self) -> bool:
        """Return whether the single broker/strategy worker is currently alive."""
        return bool(self._thread_started and self._thread.is_alive())

    def resume_after_watchdog_restart(self, request: dict[str, Any]) -> None:
        """Queue fail-closed recovery for one exact persisted cycle.

        The replacement process receives a one-time watchdog token in ``main``.
        This controller entry point never creates a new cycle: it only permits
        the normal startup/reconciliation path to resume the exact cycle named
        in the authenticated handoff request.
        """
        if self._stop_event.is_set():
            return
        self._commands.put(("WATCHDOG_AUTO_RECOVER", {"request": dict(request or {})}))

    def _write_emergency_log(
        self,
        message: str,
        *,
        exc: Optional[BaseException] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        append_emergency_log(
            message,
            exc=exc,
            context=context,
            base_dir=self.storage.db_path.parent,
        )

    def _begin_worker_operation(self, name: str) -> None:
        now = time.monotonic()
        self._worker_current_operation = str(name or "unknown")
        self._worker_operation_started_monotonic = now
        self._worker_last_heartbeat_monotonic = now

    def _complete_worker_operation(self) -> None:
        now = time.monotonic()
        self._worker_last_heartbeat_monotonic = now
        self._worker_current_operation = "idle"
        self._worker_operation_started_monotonic = now

    def _worker_health_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        operation_age = max(0.0, now - self._worker_operation_started_monotonic)
        heartbeat_age = max(0.0, now - self._worker_last_heartbeat_monotonic)
        return {
            "thread_started": bool(self._thread_started),
            "thread_alive": self.worker_is_alive(),
            "started_at": self._worker_started_at,
            "snapshot_sequence": int(self._worker_snapshot_sequence),
            "heartbeat_age_seconds": heartbeat_age,
            "current_operation": self._worker_current_operation,
            "current_operation_age_seconds": operation_age,
            "failure_message": self._worker_failure_message,
            "failure_at": self._worker_failure_at,
        }

    def _storage_fault_snapshot(self) -> dict[str, Any]:
        elapsed = (
            max(0.0, time.monotonic() - self._storage_fault_started_monotonic)
            if self._storage_fault_started_monotonic > 0
            else 0.0
        )
        return {
            "active": bool(self._storage_fault_active),
            "message": self._storage_fault_message,
            "operation": self._storage_fault_operation,
            "started_at": self._storage_fault_started_at,
            "elapsed_seconds": elapsed,
            "last_probe_at": self._storage_fault_last_probe_at,
            "probe_succeeded": bool(self._storage_fault_probe_succeeded),
            "restart_recommended": bool(self._storage_fault_restart_recommended),
        }

    def _enter_storage_fault(self, operation: str, exc: BaseException) -> None:
        """Fail closed without calling SQLite again from the error path."""
        now = time.monotonic()
        first_fault = not self._storage_fault_active
        if first_fault:
            self._storage_fault_started_at = utc_now_iso()
            self._storage_fault_started_monotonic = now
        self._storage_fault_active = True
        self._storage_fault_operation = str(operation or "SQLite operation")
        self._storage_fault_message = str(exc or "Unknown SQLite failure")
        self._storage_fault_probe_succeeded = False
        self._storage_fault_restart_recommended = False
        self.status = (
            "Storage unavailable - trading is fail-closed. Broker callbacks and "
            "the GUI remain active while BouncyBot prepares a full-process restart."
        )
        if first_fault or now - self._storage_fault_repeat_log_monotonic >= 30.0:
            self._storage_fault_repeat_log_monotonic = now
            self._write_emergency_log(
                "SQLite storage fault entered; all strategy and broker actions are blocked.",
                exc=exc,
                context={
                    "operation": self._storage_fault_operation,
                    "active_cycle_id": getattr(self.active_cycle, "id", None),
                    "active_cycle_stage": getattr(getattr(self.active_cycle, "stage", None), "value", None),
                },
            )
        try:
            self.signals.connection_changed.emit(False, self.status)
        except Exception as signal_exc:
            self._write_emergency_log(
                "Could not publish the storage-fault status through Qt.",
                exc=signal_exc,
            )

    def _probe_storage_fault_if_due(self) -> None:
        """Use a short independent transaction to decide when restart is safe."""
        if not self._storage_fault_active:
            return
        now = time.monotonic()
        if (
            self._storage_fault_last_probe_monotonic > 0
            and now - self._storage_fault_last_probe_monotonic
            < self.STORAGE_FAULT_PROBE_INTERVAL_SECONDS
        ):
            return
        self._storage_fault_last_probe_monotonic = now
        self._storage_fault_last_probe_at = utc_now_iso()
        try:
            writable = bool(self.storage.probe_writable(timeout_seconds=0.25))
        except Exception as exc:
            self._storage_fault_probe_succeeded = False
            self._storage_fault_message = str(exc or self._storage_fault_message)
            if now - self._storage_fault_repeat_log_monotonic >= 30.0:
                self._storage_fault_repeat_log_monotonic = now
                self._write_emergency_log(
                    "SQLite storage-fault probe still cannot acquire a write transaction.",
                    exc=exc,
                    context={"operation": self._storage_fault_operation},
                )
            return
        if not writable:
            return
        self._storage_fault_probe_succeeded = True
        self._storage_fault_restart_recommended = True
        self.status = (
            "Storage write access is available again. Trading remains fail-closed "
            "until the watchdog restarts BouncyBot and reconciles the exact cycle."
        )

    def _commit_waiting_transition(
        self,
        next_cycle: CycleState,
        actions: list[StrategyAction],
    ) -> bool:
        """Persist a market-driven transition before publishing or executing it."""
        try:
            self.storage.upsert_cycle(next_cycle)
        except sqlite3.Error as exc:
            self._enter_storage_fault("persist waiting-stage transition", exc)
            return False
        self.active_cycle = next_cycle
        self._execute_actions(actions, next_cycle)
        return True

    def _broker_mutation_blocked_by_storage_fault(
        self,
        operation: str,
        cycle: Optional[CycleState] = None,
    ) -> bool:
        """Return ``True`` when a broker-changing call must remain fail-closed."""
        if not self._storage_fault_active:
            return False
        self.status = (
            f"{operation} blocked: SQLite storage is unavailable. No broker order "
            "will be submitted, cancelled or replaced before full-process restart "
            "and reconciliation."
        )
        # Do not call _log here: it intentionally uses the unavailable store.
        # The first storage fault already wrote the emergency-file diagnostic.
        return True

    def start_thread(self) -> None:
        if not self._thread_started:
            self._thread_started = True
            try:
                self._thread.start()
            except Exception:
                self._thread_started = False
                raise

    def shutdown(self) -> None:
        self._auto_reconnect_enabled = False
        self._commands.put(("SHUTDOWN", {}))
        self._stop_event.set()
        if self._thread_started and self._thread.is_alive():
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                self._write_emergency_log(
                    "Worker shutdown did not complete within the deterministic shutdown timeout.",
                    context={"current_operation": self._worker_current_operation},
                )

    def shutdown_and_wait(self, timeout: float = 8.0) -> bool:
        self.shutdown()
        if not self._thread_started:
            return True
        return self._shutdown_complete.wait(max(0.1, float(timeout or 0.0)))

    def connect_tws(self, settings: ConnectionSettings) -> None:
        self._commands.put(("CONNECT", {"settings": settings}))

    def disconnect_tws(self) -> None:
        self._commands.put(("DISCONNECT", {}))

    def start_ibkr_platform(self, settings: ConnectionSettings) -> None:
        self._commands.put(("START_PLATFORM", {"settings": settings}))

    def start_strategy(self, connection: ConnectionSettings, strategy: StrategySettings) -> None:
        self._commands.put(("START_STRATEGY", {"connection": connection, "strategy": strategy}))

    def request_stop(self, action: StopAction) -> None:
        self._commands.put(("STOP_ACTION", {"action": action}))

    def request_stop_and_wait(self, action: StopAction, timeout: float = 4.0) -> bool:
        """Queue a stop action and wait briefly for the worker to apply it.

        The normal GUI path is asynchronous. For the explicit
        "Stop strategy and exit app" choice, the local STOPPED state must be
        written to SQLite before the process is shut down; otherwise the next
        launch can still see the previous active cycle and lock ticker selection.
        This helper does not change the stop action itself, it only waits for
        the existing worker command to finish.
        """
        return self._put_command_and_wait("STOP_ACTION", {"action": action}, timeout=timeout)

    def _put_command_and_wait(self, name: str, payload: dict[str, Any], timeout: float = 4.0) -> bool:
        if self._stop_event.is_set():
            return False
        ack = threading.Event()
        queued_payload = dict(payload)
        queued_payload["_ack_event"] = ack
        self._commands.put((name, queued_payload))
        return ack.wait(max(0.1, float(timeout or 0.0)))

    def refresh_history(self, ticker: str = "") -> None:
        self._commands.put(("REFRESH_HISTORY", {"ticker": ticker}))

    def refresh_broker_state(self) -> None:
        """Refresh the visible recovery comparison without submitting or cancelling orders."""
        self._commands.put(("REFRESH_BROKER_STATE", {}))

    def resume_recovery_monitoring(self) -> None:
        """Resume monitoring by rerunning the existing startup/reconnect recovery path."""
        self._commands.put(("RESUME_RECOVERY_MONITORING", {}))

    def mark_recovery_manually_handled(self, note: str = "") -> None:
        """Record that the operator handled recovery outside the app; no broker order is sent."""
        self._commands.put(("MARK_RECOVERY_MANUALLY_HANDLED", {"note": note}))

    def cancel_recovery_app_order(self) -> None:
        """Cancel app-owned open order(s) visible to the recovery screen."""
        self._commands.put(("CANCEL_RECOVERY_APP_ORDER", {}))

    def save_draft_settings(self, connection: ConnectionSettings, strategy: StrategySettings) -> None:
        """Persist editable settings without starting or reconnecting the strategy.

        The GUI calls this after the user changes any input. It keeps the
        portable SQLite file current and prevents worker snapshots from being
        treated as the editable source of truth.
        """
        self._commands.put(("SAVE_DRAFT_SETTINGS", {"connection": connection, "strategy": strategy}))

    def checkpoint_for_resume_later(
        self,
        connection: ConnectionSettings,
        strategy: StrategySettings,
        *,
        reason: str,
        timeout: float = 4.0,
    ) -> bool:
        """Durably save settings and active-cycle state before app termination.

        The normal path runs in the worker so live-safe setting edits are
        applied before the checkpoint. If the worker is blocked long enough to
        miss the bounded acknowledgement, a direct SQLite fallback stores the
        latest already-known cycle and GUI settings. The shared checkpoint ID
        makes that fallback idempotent if the delayed worker command later runs.
        """
        checkpoint_id = uuid4().hex
        result: dict[str, Any] = {"ok": False}
        if self._thread_started and self._thread.is_alive() and not self._stop_event.is_set():
            ack = threading.Event()
            self._commands.put(
                (
                    "CHECKPOINT_RESUME_STATE",
                    {
                        "connection": connection,
                        "strategy": strategy,
                        "reason": reason,
                        "checkpoint_id": checkpoint_id,
                        "_checkpoint_result": result,
                        "_ack_event": ack,
                    },
                )
            )
            if ack.wait(max(0.1, float(timeout or 0.0))) and bool(result.get("ok")):
                return True

        try:
            # The worker owns self.active_cycle and can mutate it in place.
            # This GUI-thread fallback therefore re-reads until two consecutive
            # serializations match, so a torn mid-mutation snapshot is not
            # persisted as the resume checkpoint.
            cycle_ref = self.active_cycle
            cycle: Optional[CycleState] = None
            if cycle_ref is not None:
                data = cycle_ref.to_dict()
                stable_data: Optional[dict[str, Any]] = None
                for _ in range(8):
                    confirmation = cycle_ref.to_dict()
                    if confirmation == data:
                        stable_data = confirmation
                        break
                    data = confirmation
                if stable_data is None:
                    raise RuntimeError(
                        "The active cycle changed continuously while the GUI fallback checkpoint was being created."
                    )
                cycle = CycleState.from_dict(stable_data)
            self.storage.save_resume_checkpoint(
                connection,
                strategy,
                cycle,
                reason=reason,
                checkpoint_id=checkpoint_id,
            )
            # The SQLite transaction above is the critical shutdown boundary.
            # A restore-validated backup is requested separately so a slow or
            # unavailable backup destination cannot turn a successful durable
            # checkpoint into an apparent failure while Windows is waiting.
            if self._thread_started and self._thread.is_alive() and not self._stop_event.is_set():
                self._commands.put(("CREATE_DATABASE_BACKUP", {"reason": f"resume_{reason}"}))
            return True
        except Exception as exc:
            try:
                self.storage.add_event("ERROR", f"Could not save resume checkpoint before {reason}: {exc}")
            except Exception:
                pass
            return False

    def search_tickers(self, connection: ConnectionSettings, pattern: str) -> None:
        self._commands.put(("SEARCH_CONTRACTS", {"connection": connection, "query": pattern}))

    def search_contracts(self, connection: ConnectionSettings, query: str) -> None:
        self._commands.put(("SEARCH_CONTRACTS", {"connection": connection, "query": query}))

    def confirm_ticker_price(self, connection: ConnectionSettings, strategy: StrategySettings) -> None:
        self._commands.put(("CONFIRM_TICKER_PRICE", {"connection": connection, "strategy": strategy}))

    def get_cycle_audit_details(self, cycle_id: str) -> dict[str, Any]:
        """Synchronous local-SQLite read used by the trade-history detail dialog."""
        return self.storage.cycle_audit_details(cycle_id)

    def app_owned_unsold_position(
        self,
        ticker: str,
        *,
        con_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return the persisted unsold quantity created by this app only.

        This is a read-only local query used immediately before GUI stop and
        recovery actions. It deliberately excludes the account-wide IBKR
        position, which can contain shares acquired manually or by other apps.
        When the active cycle matches the requested ticker, its exact conId is
        used automatically so another listing with the same symbol is excluded.
        """
        active = self.active_cycle
        exact_con_id = con_id
        if exact_con_id is None and active is not None and active.ticker == str(ticker or "").strip().upper():
            exact_con_id = active.con_id
        return self.storage.get_app_owned_unsold_position(ticker, con_id=exact_con_id)

    def export_history(self, ticker: str = "") -> Path:
        stamp = utc_now_iso().replace(":", "-")
        return self.storage.export_history_csv(exports_dir() / f"trade_history_{stamp}.csv", ticker=ticker)

    def export_audit_bundle(self, target_dir: Optional[Path] = None) -> Path:
        """Synchronous diagnostic export used by the Reconciliation screen."""
        try:
            database_currency = self.storage.database_contract_currency_info()
        except Exception as exc:
            database_currency = {"currency": "", "locked": False, "cycle_count": 0, "error": str(exc)}
        snapshot = {
            "created_at": utc_now_iso(),
            "connected": self.connected,
            "status": self.status,
            "db_path": str(self.storage.db_path),
            "database_contract_currency": database_currency.get("currency", ""),
            "database_contract_currency_locked": bool(database_currency.get("locked", False)),
            "database_cycle_count": int(database_currency.get("cycle_count", 0) or 0),
            "connection": asdict(self.connection),
            "display_account": self._display_account_label(),
            "strategy": asdict(self.strategy),
            "active_cycle": self.active_cycle.snapshot() if self.active_cycle else None,
            "broker_recovery": dict(self._last_recovery_probe or {}),
            "recovery_confidence": self._recovery_confidence(),
            "recovery_required": self._recovery_required,
            "startup_resume_required": self._startup_resume_required,
            "stale_active_cycle": bool(getattr(self, "_stale_active_cycle_detected", False)),
            "recent_events": self.storage.get_recent_events(200),
        }
        path = self.storage.create_audit_export_bundle(target_dir=target_dir or exports_dir(), snapshot=snapshot)
        self.storage.add_event("INFO", f"Audit export bundle created: {path}")
        return path

    def _recovery_confidence(self) -> str:
        cycle = self.active_cycle
        probe = dict(self._last_recovery_probe or {})
        if bool(getattr(self, "_stale_active_cycle_detected", False)):
            return "manual_review_required"
        if bool(self._recovery_required) or (cycle is not None and (cycle.stage == Stage.MANUAL_REVIEW or bool(getattr(cycle, "recovery_required", False)))):
            return "manual_review_required"
        if not self.connected:
            return "local_state_only"
        if any(probe.get(key) for key in ("error", "position_error", "recent_executions_error")):
            return "broker_partially_checked"
        if probe.get("checked_at"):
            return "fully_reconciled"
        return "broker_partially_checked"

    def _app_owned_position_blocker_for_buy(
        self,
        cycle: CycleState,
        database_facts: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, str]]:
        """Return a BUY blocker for unsold shares created by this app only."""
        if database_facts is None:
            try:
                summary = self.storage.get_app_owned_unsold_position(cycle.ticker, con_id=cycle.con_id)
            except Exception as exc:
                message = f"BUY pre-flight blocked order: app-owned position ledger could not be confirmed: {exc}"
                return self._trading_blocker("BUY", "app_position_unverified", message, "App ledger unavailable")
        else:
            errors = database_facts.get("errors") or {}
            error = errors.get("app_owned_position")
            if error:
                message = f"BUY pre-flight blocked order: app-owned position ledger could not be confirmed: {error}"
                return self._trading_blocker("BUY", "app_position_unverified", message, "App ledger unavailable")
            if (
                str(database_facts.get("ticker") or "") != cycle.ticker
                or int(database_facts.get("con_id") or 0) != int(cycle.con_id or 0)
                or "app_owned_position" not in database_facts
            ):
                message = "BUY pre-flight blocked order: the cached app-owned position ledger has not been refreshed for this exact contract yet."
                return self._trading_blocker("BUY", "app_position_unverified", message, "App ledger refreshing")
            summary = database_facts.get("app_owned_position") or {}
        try:
            quantity = int(summary.get("quantity") or 0)
        except Exception:
            quantity = 0
        if quantity <= 0:
            return None
        cycle_numbers = [
            str(item.get("cycle_number"))
            for item in (summary.get("cycles") or [])
            if item.get("cycle_number") not in (None, "", 0)
        ]
        source = f" from app cycle(s) {', '.join(cycle_numbers)}" if cycle_numbers else ""
        message = (
            f"BUY pre-flight blocked order: the app records {quantity} unsold app-owned "
            f"{cycle.ticker} share{'s' if quantity != 1 else ''}{source}. "
            "Manual or externally acquired broker holdings are not counted."
        )
        return self._trading_blocker("BUY", "app_owned_position", message, f"App position {quantity}")

    def _current_trading_blockers(self, database_facts: Optional[dict[str, Any]] = None) -> list[dict[str, str]]:
        """Describe live conditions that currently prevent a new BUY or SELL."""
        cycle = self.active_cycle
        if cycle is None:
            return []

        blockers: list[dict[str, str]] = []
        stage = cycle.stage
        active_stages = {
            Stage.WAIT_INITIAL_DROP,
            Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER,
            Stage.SELL_TRAIL_ACTIVE,
        }
        if stage in active_stages:
            if database_facts is not None and self._broker_connectivity_initialized:
                connectivity = dict(self._broker_connectivity)
            else:
                connectivity = self._adapter_connectivity_snapshot()
                self._broker_connectivity = connectivity
            if stage in {Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE}:
                connectivity_side = "BUY"
            elif stage in {Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE}:
                connectivity_side = "SELL"
            else:
                connectivity_side = "BUY/SELL"
            if not self.connected or not bool(connectivity.get("local_connected")):
                blockers.append(
                    self._trading_blocker(
                        connectivity_side,
                        "disconnected",
                        f"Connection guard blocked {connectivity_side}: the local IBKR API connection is disconnected.",
                        "Disconnected",
                    )
                )
            elif connectivity.get("upstream_connected") is not True:
                detail = str(connectivity.get("message") or "Gateway-to-IBKR server connectivity is unavailable.")
                code = connectivity.get("error_code")
                code_text = f" IBKR code {code}." if code not in (None, "") else ""
                blockers.append(
                    self._trading_blocker(
                        connectivity_side,
                        "upstream_disconnected",
                        f"Connectivity guard blocked {connectivity_side}: IBKR server connectivity is not confirmed.{code_text} {detail}".strip(),
                        "IBKR link lost",
                    )
                )
            elif self._upstream_recovery_pending:
                blockers.append(
                    self._trading_blocker(
                        connectivity_side,
                        "upstream_recovery",
                        f"Connectivity guard blocked {connectivity_side}: post-reconnect broker reconciliation is still in progress.",
                        "Reconnecting",
                    )
                )

        if stage == Stage.WAIT_INITIAL_DROP:
            app_position = self._app_owned_position_blocker_for_buy(cycle, database_facts)
            if app_position is not None:
                blockers.append(app_position)
            try:
                blockers.extend(self._risk_guard_blockers_for_buy(cycle, database_facts=database_facts))
            except Exception as exc:
                blockers.append(
                    self._trading_blocker(
                        "BUY",
                        "guard_evaluation_unavailable",
                        f"BUY guard evaluation is unavailable: {exc}",
                        "Guard check unavailable",
                    )
                )

        if stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}:
            snapshot = self.price_snapshot or {}
            snapshot_price = self._positive_float(snapshot.get("price"))
            side = "BUY" if stage == Stage.WAIT_INITIAL_DROP else "SELL"
            if snapshot_price is None and not any(item.get("code") == "stale_data" for item in blockers):
                blockers.append(
                    self._trading_blocker(
                        side,
                        "no_price",
                        f"Market-data guard blocked {side}: no usable current strategy price is available yet.",
                        "No usable price",
                    )
                )
            elif (
                (self._api_data_invalidated or bool(snapshot.get("api_data_invalidated")))
                and (
                    self._broker_connectivity_initialized
                    or "api_data_invalidated" in snapshot
                    or bool(snapshot.get("market_data_event_tracking"))
                )
                and not any(item.get("code") in {"upstream_disconnected", "disconnected"} for item in blockers)
            ):
                reason = str(
                    snapshot.get("api_data_invalidated_reason")
                    or self._api_data_invalidated_reason
                    or "waiting for a fresh market-data update"
                )
                blockers.append(
                    self._trading_blocker(
                        side,
                        "fresh_market_data_pending",
                        f"Market-data guard blocked {side}: {reason}",
                        "Waiting for data",
                    )
                )
            else:
                data_age = snapshot.get("api_data_age_seconds")
                max_age = max(0.1, float(getattr(cycle, "max_selected_price_age_seconds", 3.0) or 3.0))
                data_is_stale = str(snapshot.get("api_data_state") or "") == "stale" or (
                    isinstance(data_age, (int, float)) and float(data_age) > max_age
                )
                if data_is_stale and not any(item.get("code") == "stale_data" for item in blockers):
                    age_text = f" Last actual update age: {float(data_age):.1f}s." if isinstance(data_age, (int, float)) else ""
                    blockers.append(
                        self._trading_blocker(
                            side,
                            "stale_data",
                            f"Market-data guard blocked {side}: no sufficiently recent streaming update is available.{age_text}",
                            "Stale data",
                        )
                    )

        if stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER} and bool(getattr(cycle, "rth_only", True)):
            rth = self._latest_rth_status
            if rth is not None and not bool(rth.get("is_open")):
                side = "BUY" if stage == Stage.WAIT_INITIAL_DROP else "SELL"
                detail = str(rth.get("message") or rth.get("source") or "regular trading hours are closed")
                blockers.append(
                    self._trading_blocker(
                        side,
                        "rth_closed",
                        f"RTH guard blocked {side}: {detail}",
                        "RTH closed",
                    )
                )

        message = str(getattr(cycle, "error_message", "") or "").strip()
        if message and stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}:
            lowered = message.lower()
            retired_external_position_block = (
                "current broker position is already" in lowered
                and "expected no open long position" in lowered
            )
            last_attempt_guard = any(
                token in lowered
                for token in (
                    "what-if",
                    "pre-flight",
                    "waiting for protective sell cancellation",
                    "order submission failed",
                    "not placed because",
                )
            )
            duplicate = any(item.get("message") == message for item in blockers)
            if not retired_external_position_block and last_attempt_guard and not duplicate:
                side = "BUY" if stage == Stage.WAIT_INITIAL_DROP else "SELL"
                if "what-if" in lowered:
                    short = "What-if check"
                elif "pre-flight" in lowered:
                    short = "Pre-flight"
                elif "protective" in lowered:
                    short = "Protective cancel"
                elif "minimum profit" in lowered or "minimum-profit" in lowered:
                    short = "Profit guard"
                else:
                    short = "Last submission"
                blockers.append(self._trading_blocker(side, "last_guard", message, short))

        deduplicated: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for blocker in blockers:
            key = (blocker.get("side", ""), blocker.get("code", ""), blocker.get("message", ""))
            if key not in seen:
                seen.add(key)
                deduplicated.append(blocker)
        return deduplicated

    def _trading_status_snapshot(self, database_facts: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Build the compact top-bar Trading state and full blocker details."""
        cycle = self.active_cycle
        if self._storage_fault_active:
            message = (
                "SQLite storage is unavailable. All strategy evaluation and broker "
                "actions are blocked until a full-process restart and reconciliation complete."
            )
            return {
                "summary": "Storage fault",
                "state": "risk",
                "tooltip": f"{message} {self._storage_fault_message}".strip(),
                "blockers": [self._trading_blocker("BUY/SELL", "storage", message, "Storage")],
            }
        if self._watchdog_auto_recovery_pending:
            return {
                "summary": "Recovering",
                "state": "waiting",
                "tooltip": "Watchdog restart recovery is validating the exact persisted cycle and broker state.",
                "blockers": [],
            }
        if self._startup_resume_required:
            return {
                "summary": "Start required",
                "state": "waiting",
                "tooltip": "A stored cycle requires an explicit Start action before monitoring resumes.",
                "blockers": [],
            }

        stage = cycle.stage if cycle is not None else None
        if stage in {Stage.ERROR, Stage.MANUAL_REVIEW}:
            message = str(getattr(cycle, "error_message", "") or "Recovery/manual review is required.")
            return {
                "summary": "Blocked",
                "state": "risk",
                "tooltip": message,
                "blockers": [self._trading_blocker("BUY/SELL", "recovery", message, "Recovery")],
            }

        blockers = self._current_trading_blockers(database_facts)
        if blockers:
            sides = {str(item.get("side") or "").upper() for item in blockers}
            if sides == {"BUY"}:
                prefix = "BUY blocked"
            elif sides == {"SELL"}:
                prefix = "SELL blocked"
            else:
                prefix = "BUY/SELL blocked"
            first = blockers[0]
            summary = f"{prefix}: {first.get('short') or 'Guard'}"
            if len(blockers) > 1:
                summary += f" +{len(blockers) - 1}"
            tooltip = "\n".join(
                f"{item.get('side')}: {item.get('message')}" for item in blockers
            )
            return {
                "summary": summary,
                "state": "waiting",
                "tooltip": tooltip,
                "blockers": blockers,
            }

        if stage in {
            Stage.WAIT_INITIAL_DROP,
            Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER,
            Stage.SELL_TRAIL_ACTIVE,
        }:
            return {
                "summary": "Running",
                "state": "active",
                "tooltip": "No configured trading guard is currently blocking the next eligible broker action.",
                "blockers": [],
            }
        return {
            "summary": "Stopped",
            "state": "waiting",
            "tooltip": "No strategy cycle is currently running.",
            "blockers": [],
        }

    def _snapshot_guard_database_facts(self) -> dict[str, Any]:
        """Read GUI-only guard facts on the database cadence.

        Broker order pre-flight paths continue to query SQLite synchronously so
        an order is never authorized from a cadence cache. These facts are only
        used to render the top-bar blocker summary without opening several SQLite
        connections on every GUI refresh.
        """
        cycle = self.active_cycle
        facts: dict[str, Any] = {
            "ticker": cycle.ticker if cycle is not None else "",
            "con_id": cycle.con_id if cycle is not None else None,
            "errors": {},
        }
        if cycle is None or cycle.stage != Stage.WAIT_INITIAL_DROP:
            return facts

        errors: dict[str, str] = facts["errors"]

        def capture(name: str, loader: Any) -> None:
            try:
                facts[name] = loader()
            except Exception as exc:
                errors[name] = str(exc)

        capture("app_owned_position", lambda: self.storage.get_app_owned_unsold_position(cycle.ticker, con_id=cycle.con_id))
        if bool(getattr(cycle, "hard_risk_limits_enabled", False)):
            if float(getattr(cycle, "max_daily_loss_ticker", 0.0) or 0.0) > 0:
                capture("daily_net_pnl_ticker", lambda: self.storage.get_daily_net_pnl_for_ticker(cycle.ticker, con_id=cycle.con_id))
            if float(getattr(cycle, "max_daily_loss_total", 0.0) or 0.0) > 0:
                capture("daily_net_pnl_total", self.storage.get_daily_net_pnl_total)
            if int(getattr(cycle, "max_cycles_per_ticker_day", 0) or 0) > 0:
                capture("completed_cycle_count", lambda: self.storage.get_completed_cycle_count(cycle.ticker, con_id=cycle.con_id))
            if int(getattr(cycle, "max_consecutive_losses", 0) or 0) > 0:
                capture("consecutive_loss_count", lambda: self.storage.get_consecutive_loss_count(cycle.ticker, con_id=cycle.con_id))
        return facts

    def _refresh_snapshot_database_cache(self, *, force: bool = False) -> dict[str, Any]:
        """Refresh read-heavy snapshot data independently from GUI rendering."""
        now = time.monotonic()
        if (
            not force
            and self._last_database_refresh_monotonic > 0
            and now - self._last_database_refresh_monotonic < self.DATABASE_CADENCE_SECONDS
        ):
            return self._snapshot_database_cache

        previous = self._snapshot_database_cache
        errors: dict[str, str] = {}
        ticker = self.strategy.normalized_ticker() if self.strategy else ""
        try:
            recent_events = self.storage.get_recent_events(60)
        except Exception as exc:
            recent_events = list(previous.get("recent_events") or [])
            errors["recent_events"] = str(exc)
        try:
            history_summary = self.storage.history_summary(ticker)
        except Exception as exc:
            history_summary = dict(previous.get("history_summary") or {})
            errors["history_summary"] = str(exc)
        try:
            database_currency = self.storage.database_contract_currency_info()
        except Exception as exc:
            database_currency = dict(previous.get("database_currency") or {})
            errors["database_currency"] = str(exc)

        guard_facts = self._snapshot_guard_database_facts()
        self._snapshot_database_cache = {
            "ticker": ticker,
            "recent_events": list(recent_events or []),
            "history_summary": dict(history_summary or {}),
            "guard_facts": guard_facts,
            "database_currency": dict(database_currency or {}),
            "errors": errors,
            "refreshed_at": utc_now_iso(),
        }
        self._last_database_refresh_monotonic = now
        return self._snapshot_database_cache

    def _run_database_cycle(self, *, force: bool = False) -> None:
        if self._storage_fault_active:
            self._probe_storage_fault_if_due()
            return
        self._refresh_snapshot_database_cache(force=force)

    def emit_snapshot(self, force: bool = False, *, refresh_database: bool = True) -> None:
        now = time.monotonic()
        if not force and now - self._last_snapshot_emit < self.GUI_CADENCE_SECONDS:
            return
        self._last_snapshot_emit = now
        # GUI rendering never advances the database cadence. The first direct
        # snapshot call may initialize an empty cache for compatibility with
        # headless callers, but every subsequent refresh is owned exclusively
        # by _run_database_cycle().
        if refresh_database and self._last_database_refresh_monotonic <= 0:
            self._refresh_snapshot_database_cache(force=True)
        database_cache = self._snapshot_database_cache
        recent_events = list(database_cache.get("recent_events") or [])
        history_summary = dict(database_cache.get("history_summary") or {})
        database_facts = database_cache.get("guard_facts") or {}
        database_currency = dict(database_cache.get("database_currency") or {})

        if not self.connected:
            broker_connectivity = dict(self._broker_connectivity)
        elif self._broker_connectivity_initialized and self._last_broker_refresh_monotonic > 0:
            broker_connectivity = dict(self._broker_connectivity)
        else:
            broker_connectivity = self._adapter_connectivity_snapshot()
            self._broker_connectivity_initialized = True
            self._last_broker_refresh_monotonic = time.monotonic()
        self._broker_connectivity = broker_connectivity
        price_snapshot = dict(self.price_snapshot or {})
        monotonic_now = time.monotonic()
        price_snapshot = self._refresh_price_snapshot_freshness_for_emit(
            price_snapshot,
            broker_connectivity,
            monotonic_now,
        )
        if self.price_snapshot is not None and price_snapshot:
            # Keep the controller-side state synchronized with the emitted
            # freshness classification so trading-status blockers and the GUI
            # cannot keep reporting a previously green snapshot after updates
            # have stopped arriving.
            self.price_snapshot.update(price_snapshot)
        stage3_status: dict[str, Any] = {}
        if (
            self.active_cycle is not None
            and self.active_cycle.stage == Stage.WAIT_RISE_TRIGGER
            and str(self._stage3_quote_guard_status.get("cycle_id") or "") == self.active_cycle.id
        ):
            stage3_status = dict(self._stage3_quote_guard_status)
            condition = self._audit_conditions.get(
                f"stage3_sell_quote_guard|{self.active_cycle.id}"
            )
            if condition is not None:
                stage3_status["duration_seconds"] = max(
                    0.0,
                    monotonic_now
                    - float(condition.get("first_seen_monotonic", monotonic_now)),
                )
                stage3_status["occurrence_count"] = int(
                    condition.get("occurrence_count", 0) or 0
                )
                stage3_status["audit_emissions"] = int(
                    condition.get("audit_emissions", 0) or 0
                )
                stage3_status["suppressed_count"] = max(
                    0,
                    int(condition.get("occurrence_count", 0) or 0)
                    - int(condition.get("audit_emissions", 0) or 0),
                )
        price_snapshot["stage3_sell_quote_status"] = stage3_status
        self._worker_snapshot_sequence += 1
        snapshot = {
            "connected": self.connected,
            "status": self.status,
            "db_path": str(self.storage.db_path),
            "database_contract_currency": database_currency.get("currency", ""),
            "database_contract_currency_locked": bool(database_currency.get("locked", False)),
            "database_cycle_count": int(database_currency.get("cycle_count", 0) or 0),
            "connection": asdict(self.connection),
            "display_account": self._display_account_label(),
            "broker_accounts": list(self._broker_display_accounts),
            "broker_connectivity": broker_connectivity,
            "upstream_recovery_pending": bool(self._upstream_recovery_pending),
            "strategy": asdict(self.strategy),
            "active_cycle": self.active_cycle.snapshot() if self.active_cycle else None,
            "trading_status": self._trading_status_snapshot(database_facts),
            "price_snapshot": price_snapshot,
            "price_poll_interval_seconds": self.PRICE_POLL_INTERVAL_SECONDS,
            "worker_health": self._worker_health_snapshot(),
            "storage_fault": self._storage_fault_snapshot(),
            "watchdog_auto_recovery": {
                "pending": bool(self._watchdog_auto_recovery_pending),
                "expected_cycle_id": (self._watchdog_auto_recovery_request or {}).get("expected_cycle_id"),
                "reason": (self._watchdog_auto_recovery_request or {}).get("reason", ""),
            },
            "worker_cadences": {
                "broker_seconds": self.BROKER_CADENCE_SECONDS,
                "strategy_seconds": self.STRATEGY_CADENCE_SECONDS,
                "gui_seconds": self.GUI_CADENCE_SECONDS,
                "database_seconds": self.DATABASE_CADENCE_SECONDS,
                "maintenance_seconds": self.MAINTENANCE_CADENCE_SECONDS,
                "reconnect_seconds": self.RECONNECT_INTERVAL_SECONDS,
            },
            "auto_reconnect_enabled": bool(self._auto_reconnect_enabled),
            "reconnect_failures": int(self._reconnect_failures),
            "market_capture": {"enabled": True, "buffer_rows": self._market_capture.buffer_size, "pending": self._market_capture.pending_count, "completed_files": [str(p) for p in self._market_capture.completed_files[-5:]]},
            "broker_recovery": dict(self._last_recovery_probe or {}),
            "recovery_confidence": self._recovery_confidence(),
            "recovery_required": self._recovery_required,
            "startup_resume_required": self._startup_resume_required,
            "stale_active_cycle": bool(getattr(self, "_stale_active_cycle_detected", False)),
            "stale_active_cycle_age_seconds": self._active_cycle_stale_age_seconds(self.active_cycle),
            "stale_active_cycle_threshold_seconds": self.STALE_ACTIVE_CYCLE_SECONDS,
            "history_summary": history_summary,
            "events": recent_events,
            "database_snapshot": {
                "refreshed_at": database_cache.get("refreshed_at", ""),
                "errors": dict(database_cache.get("errors") or {}),
            },
        }
        self._last_snapshot_payload = snapshot
        self.signals.snapshot_updated.emit(snapshot)

    def _refresh_price_snapshot_freshness_for_emit(
        self,
        price_snapshot: dict[str, Any],
        broker_connectivity: dict[str, Any],
        monotonic_now: float,
    ) -> dict[str, Any]:
        """Re-evaluate streaming freshness even when no new quote read occurs.

        A Ticker object retains its last values when IBKR stops streaming. The
        worker can also temporarily skip quote polling during connectivity
        transitions. Reclassifying on every emitted snapshot prevents a stale
        cached price from remaining green merely because the last read said
        ``receiving``.
        """
        if not price_snapshot:
            return price_snapshot

        if self._last_price_poll_monotonic > 0:
            price_snapshot["age_seconds"] = max(0.0, monotonic_now - self._last_price_poll_monotonic)
            price_snapshot["next_refresh_seconds"] = self._seconds_until_next_price_poll()
        else:
            price_snapshot["next_refresh_seconds"] = 0.0

        data_age: Optional[float] = None
        if self._api_last_data_monotonic > 0:
            data_age = max(0.0, monotonic_now - self._api_last_data_monotonic)
            price_snapshot["api_data_age_seconds"] = data_age
            price_snapshot["api_data_last_received_age_seconds"] = data_age
            price_snapshot["api_last_data_received_at"] = self._api_last_data_wall_time
            price_snapshot["api_data_last_received_at"] = self._api_last_data_wall_time
        if self._api_last_change_monotonic > 0:
            change_age = max(0.0, monotonic_now - self._api_last_change_monotonic)
            price_snapshot["api_data_change_age_seconds"] = change_age
            price_snapshot["api_data_last_change_age_seconds"] = change_age
            price_snapshot["api_last_value_change_at"] = self._api_last_change_wall_time
            price_snapshot["api_data_last_change_at"] = self._api_last_change_wall_time

        price_snapshot["api_data_seen_count"] = self._api_data_seen_count
        price_snapshot["api_data_update_count"] = self._api_data_seen_count
        price_snapshot["api_data_reads_with_values"] = self._api_data_seen_count
        price_snapshot["api_data_change_count"] = self._api_data_change_count
        upstream_connected = broker_connectivity.get("upstream_connected")
        awaiting_fresh_data = bool(broker_connectivity.get("awaiting_fresh_market_data"))
        freshness_limit = max(
            0.1,
            float(getattr(self.strategy, "max_selected_price_age_seconds", 3.0) or 3.0),
        )
        fields = price_snapshot.get("fields") or {}
        has_cached_values = bool(
            price_snapshot.get("price") is not None
            or any(value is not None for value in fields.values())
        )
        invalidated = bool(self._api_data_invalidated or awaiting_fresh_data)
        invalidation_reason = (
            self._api_data_invalidated_reason
            or str(broker_connectivity.get("message") or "")
            or "WAITING FOR A FRESH MARKET-DATA UPDATE"
        )
        price_snapshot["api_data_invalidated"] = invalidated
        price_snapshot["api_data_invalidated_reason"] = invalidation_reason if invalidated else ""

        if upstream_connected is False:
            data_state = "upstream_disconnected"
            indicator_text = "IBKR SERVER LINK LOST - cached quote fields are invalid"
        elif invalidated:
            data_state = "invalidated"
            indicator_text = invalidation_reason
        elif data_age is not None and data_age > freshness_limit:
            data_state = "stale"
            indicator_text = f"API DATA STALE - last actual update {data_age:.1f}s ago"
        elif self._api_last_data_monotonic > 0:
            latest_read_consumed_update = bool(price_snapshot.get("api_data_received_in_latest_read"))
            data_state = "receiving" if latest_read_consumed_update else "recent"
            indicator_text = (
                "FRESH API UPDATE - streaming event received"
                if latest_read_consumed_update
                else "NO NEW UPDATE IN THIS READ - last actual update remains recent"
            )
        elif has_cached_values:
            data_state = "cached_only"
            indicator_text = "CACHED API FIELDS ONLY - waiting for an actual update event"
        else:
            data_state = "none"
            indicator_text = "NO API PRICE DATA RECEIVED"

        price_snapshot["api_data_state"] = data_state
        price_snapshot["api_data_indicator_text"] = indicator_text
        if data_state not in {"receiving", "recent"}:
            price_snapshot["strategy_price_usable"] = False
        elif data_age is not None and data_age > freshness_limit:
            price_snapshot["strategy_price_usable"] = False
        return price_snapshot

    def _write_human_debug_report(self, snapshot: dict[str, Any], *, force: bool = False) -> None:
        """Persist a best-effort human-readable debug report beside SQLite."""
        now = time.monotonic()
        if not force and now - self._last_human_report_monotonic < 60.0:
            return
        self._last_human_report_monotonic = now
        try:
            self.storage.write_human_debug_report(snapshot)
        except Exception:
            # Diagnostics must never affect trading or GUI updates.
            pass

    def _prune_inactive_audit_conditions(self) -> None:
        """Resolve coalesced diagnostics when their owning workflow is gone."""
        cycle = self.active_cycle
        if not (
            cycle is not None
            and cycle.stage == Stage.WAIT_RISE_TRIGGER
            and cycle.id == self._stage3_routine_guard_entry_cycle_id
        ):
            self._stage3_routine_guard_entry_cycle_id = ""
        for key, state in list(self._audit_conditions.items()):
            kind = str(state.get("kind") or "")
            state_cycle_id = str(state.get("cycle_id") or "")
            if kind == "stage3_sell_quote_guard":
                active = bool(
                    cycle is not None
                    and cycle.stage == Stage.WAIT_RISE_TRIGGER
                    and cycle.id == state_cycle_id
                )
                if not active:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="Stage 3 is no longer active.",
                    )
                    if str(self._stage3_quote_guard_status.get("cycle_id") or "") == state_cycle_id:
                        self._stage3_quote_guard_status = {}
                    if self._stage3_routine_guard_entry_cycle_id == state_cycle_id:
                        self._stage3_routine_guard_entry_cycle_id = ""
            elif kind in {"native_order_wait", "native_order_anomaly"}:
                context = dict(state.get("latest_context") or {})
                side = str(context.get("side") or "").upper()
                order_ref = str(context.get("order_ref") or "")
                if side == "BUY":
                    active_ref = str(cycle.buy_order_ref or "") if cycle and cycle.stage == Stage.BUY_TRAIL_ACTIVE else ""
                elif side == "PROTECTIVE_SELL":
                    active_ref = (
                        str(cycle.protective_sell_order_ref or "")
                        if cycle and cycle.stage == Stage.WAIT_RISE_TRIGGER
                        else ""
                    )
                else:
                    active_ref = str(cycle.sell_order_ref or "") if cycle and cycle.stage == Stage.SELL_TRAIL_ACTIVE else ""
                if not order_ref or active_ref != order_ref:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="The broker order is no longer in the waiting state.",
                    )
            elif kind == "buy_preflight_block":
                if cycle is None or cycle.id != state_cycle_id or cycle.stage != Stage.WAIT_INITIAL_DROP:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="The BUY preflight block is no longer active.",
                    )
            elif kind == "session_timing_guard":
                active = bool(
                    cycle is not None
                    and cycle.id == state_cycle_id
                    and cycle.stage == Stage.WAIT_RISE_TRIGGER
                    and bool(
                        getattr(
                            cycle,
                            "cancel_sell_and_liquidate_before_close_enabled",
                            False,
                        )
                    )
                )
                if not active:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="The Stage-3 session-timing guard is no longer active.",
                    )
            elif kind == "stage4_session_timing_guard":
                active = bool(
                    cycle is not None
                    and cycle.id == state_cycle_id
                    and cycle.stage == Stage.SELL_TRAIL_ACTIVE
                    and (
                        bool(
                            getattr(
                                cycle,
                                "cancel_sell_and_liquidate_before_close_enabled",
                                False,
                            )
                        )
                        or bool(
                            getattr(
                                cycle,
                                "close_before_rth_liquidation_requested",
                                False,
                            )
                        )
                    )
                )
                if not active:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="The Stage-4 session-timing guard is no longer active.",
                    )
            elif kind == "close_before_rth_wait":
                active = bool(
                    cycle is not None
                    and cycle.id == state_cycle_id
                    and (
                        bool(getattr(cycle, "close_before_rth_liquidation_requested", False))
                        or bool(getattr(cycle, "close_before_rth_cancel_requested", False))
                    )
                )
                if not active:
                    self._clear_audit_condition(
                        key,
                        cycle=cycle if cycle and cycle.id == state_cycle_id else None,
                        recovery_message="The close-before-RTH workflow is no longer waiting.",
                    )

        connectivity = dict(self._broker_connectivity or {})
        if self.connected and connectivity.get("upstream_connected") is True:
            self._clear_audit_condition(
                "broker_reconnect",
                cycle=self.active_cycle,
                recovery_message="IBKR connectivity is restored.",
            )

    def _run_maintenance_cycle(self, *, force: bool = False) -> None:
        """Run low-frequency housekeeping independently from GUI rendering."""
        self._refresh_stale_active_cycle_flag()
        self._prune_inactive_audit_conditions()
        if self._last_snapshot_payload:
            self._write_human_debug_report(self._last_snapshot_payload, force=force)

    @staticmethod
    def _worker_interval(value: float) -> float:
        """Clamp a configured cadence so a bad value cannot create a busy loop."""
        return max(0.001, float(value))

    def _thread_main(self) -> None:
        unexpected_failure: Optional[BaseException] = None
        try:
            self._begin_worker_operation("startup")
            self._log("INFO", "Application worker started.")
            self._run_database_cycle(force=True)
            self.emit_snapshot(force=True, refresh_database=False)
            self._run_maintenance_cycle(force=True)
            self._complete_worker_operation()

            now = time.monotonic()
            next_broker = now
            next_strategy = now
            next_database = now + self._worker_interval(self.DATABASE_CADENCE_SECONDS)
            next_gui = now + self._worker_interval(self.GUI_CADENCE_SECONDS)
            next_maintenance = now + self._worker_interval(self.MAINTENANCE_CADENCE_SECONDS)
            broker_ready_for_strategy = False

            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_broker:
                    self._begin_worker_operation("broker cadence")
                    try:
                        broker_ready_for_strategy = self._run_broker_cycle(process_timeout=0.0)
                    except sqlite3.Error as exc:
                        broker_ready_for_strategy = False
                        self._enter_storage_fault("broker cadence", exc)
                    except BrokerAdapterError as exc:
                        broker_ready_for_strategy = False
                        self._handle_broker_connection_problem(exc)
                    except Exception as exc:
                        broker_ready_for_strategy = False
                        self._log("ERROR", f"Broker cadence error: {exc}")
                    finally:
                        self._complete_worker_operation()
                    next_broker = time.monotonic() + self._worker_interval(self.BROKER_CADENCE_SECONDS)

                if self._stop_event.is_set():
                    break

                now = time.monotonic()
                if now >= next_strategy:
                    if broker_ready_for_strategy and not self._storage_fault_active:
                        self._begin_worker_operation("strategy cadence")
                        try:
                            self._run_strategy_cycle(price_timeout=0.0)
                        except sqlite3.Error as exc:
                            self._enter_storage_fault("strategy cadence", exc)
                        except BrokerAdapterError as exc:
                            broker_ready_for_strategy = False
                            self._handle_broker_connection_problem(exc)
                        except Exception as exc:
                            self._log("ERROR", f"Strategy cadence error: {exc}")
                            if self.active_cycle:
                                failed_cycle = StrategyEngine.mark_error(self.active_cycle, str(exc))
                                try:
                                    self.storage.upsert_cycle(failed_cycle)
                                except sqlite3.Error as storage_exc:
                                    self._enter_storage_fault(
                                        "persist strategy error state",
                                        storage_exc,
                                    )
                                else:
                                    self.active_cycle = failed_cycle
                        finally:
                            self._complete_worker_operation()
                    next_strategy = time.monotonic() + self._worker_interval(self.STRATEGY_CADENCE_SECONDS)

                now = time.monotonic()
                if now >= next_database:
                    self._begin_worker_operation("database cadence")
                    try:
                        self._run_database_cycle(force=True)
                    except sqlite3.Error as exc:
                        self._enter_storage_fault("database cadence", exc)
                    except Exception as exc:
                        self._log("WARN", f"Database snapshot cadence error: {exc}")
                    finally:
                        self._complete_worker_operation()
                    next_database = time.monotonic() + self._worker_interval(self.DATABASE_CADENCE_SECONDS)

                now = time.monotonic()
                if now >= next_gui:
                    self._begin_worker_operation("GUI snapshot cadence")
                    try:
                        self.emit_snapshot(refresh_database=False)
                    except Exception as exc:
                        self._log("WARN", f"GUI snapshot cadence error: {exc}")
                    finally:
                        self._complete_worker_operation()
                    next_gui = time.monotonic() + self._worker_interval(self.GUI_CADENCE_SECONDS)

                now = time.monotonic()
                if now >= next_maintenance:
                    self._begin_worker_operation("maintenance cadence")
                    try:
                        self._run_maintenance_cycle()
                    except Exception as exc:
                        self._log("WARN", f"Maintenance cadence error: {exc}")
                    finally:
                        self._complete_worker_operation()
                    next_maintenance = time.monotonic() + self._worker_interval(self.MAINTENANCE_CADENCE_SECONDS)

                if self._stop_event.is_set():
                    break

                now = time.monotonic()
                next_deadline = min(
                    next_broker,
                    next_strategy,
                    next_database,
                    next_gui,
                    next_maintenance,
                )
                wait_timeout = min(
                    self._worker_interval(self.MAX_IDLE_WAIT_SECONDS),
                    max(0.0, next_deadline - now),
                )
                self._begin_worker_operation("command wait")
                try:
                    command = self._commands.get(timeout=wait_timeout)
                except queue.Empty:
                    self._complete_worker_operation()
                    continue
                self._complete_worker_operation()
                # shutdown() sets the stop event as well as waking this queue. If
                # an older command was already queued, do not execute it after
                # shutdown has begun (it could otherwise submit a broker action).
                if self._stop_event.is_set():
                    break

                # Dispatch any already-reported connectivity callback before the
                # command. This preserves the fail-closed order-submission race
                # guarantee while Queue.get() provides immediate command wakeup.
                if self.connected and self.adapter.is_connected():
                    self._begin_worker_operation("pre-command broker callbacks")
                    try:
                        if self._storage_fault_active:
                            self._pump_broker_transport_only(process_timeout=0.0)
                        else:
                            self._pump_broker_callbacks(process_timeout=0.0)
                    except sqlite3.Error as exc:
                        self._enter_storage_fault("pre-command broker callbacks", exc)
                    except BrokerAdapterError as exc:
                        self._handle_broker_connection_problem(exc)
                    except Exception as exc:
                        self._log("WARN", f"Pre-command broker callback error: {exc}")
                    finally:
                        self._complete_worker_operation()
                self._begin_worker_operation(f"command {command[0]}")
                try:
                    self._process_queued_command(*command)
                    self._drain_commands(max_commands=63)
                finally:
                    self._complete_worker_operation()
                broker_ready_for_strategy = False

                # Commands may connect, disconnect, stop, or alter strategy state.
                # Re-evaluate broker/strategy/GUI work immediately on the next
                # loop without coupling it to the database/maintenance clocks.
                now = time.monotonic()
                next_broker = min(next_broker, now)
                next_strategy = min(next_strategy, now)
                next_gui = min(next_gui, now)
        except BaseException as exc:
            unexpected_failure = exc
            self._worker_failure_message = f"{type(exc).__name__}: {exc}"
            self._worker_failure_at = utc_now_iso()
            self.status = (
                "Worker terminated unexpectedly - trading monitoring is not running. "
                "The GUI watchdog will restart BouncyBot."
            )
            self._write_emergency_log(
                "Unhandled exception terminated the BouncyBot worker.",
                exc=exc,
                context={
                    "current_operation": self._worker_current_operation,
                    "active_cycle_id": getattr(self.active_cycle, "id", None),
                    "active_cycle_stage": getattr(getattr(self.active_cycle, "stage", None), "value", None),
                },
            )
            try:
                self.emit_snapshot(force=True, refresh_database=False)
            except BaseException as snapshot_exc:
                self._write_emergency_log(
                    "Could not emit the final worker-failure snapshot.",
                    exc=snapshot_exc,
                )
        finally:
            self._worker_current_operation = "shutdown cleanup"
            self._worker_operation_started_monotonic = time.monotonic()
            try:
                if self.adapter.is_connected():
                    self.adapter.disconnect()
            except Exception:
                pass
            try:
                self._market_capture.shutdown(wait=True, timeout=5.0)
            except Exception:
                pass
            if unexpected_failure is None and not self._storage_fault_active:
                self._log("INFO", "Application worker stopped cleanly during app shutdown.")
                try:
                    self.storage.backup_database("app_shutdown")
                except Exception:
                    pass
            self.connected = False
            if unexpected_failure is None:
                self.status = "Stopped"
            try:
                if not self._storage_fault_active:
                    self._run_database_cycle(force=True)
            except Exception:
                pass
            try:
                self.emit_snapshot(force=True, refresh_database=False)
            except Exception:
                pass
            try:
                self._run_maintenance_cycle(force=True)
            except Exception:
                pass
            self._worker_last_heartbeat_monotonic = time.monotonic()
            self._worker_current_operation = "stopped"
            self._worker_operation_started_monotonic = self._worker_last_heartbeat_monotonic
            self._shutdown_complete.set()

    def _process_queued_command(self, name: str, payload: dict[str, Any]) -> None:
        ack = payload.pop("_ack_event", None) if isinstance(payload, dict) else None
        try:
            self._handle_command(name, payload)
        except sqlite3.Error as exc:
            self._enter_storage_fault(f"command {name}", exc)
        except Exception as exc:
            self._log("ERROR", f"Command {name} failed: {exc}")
            self.status = f"Error: {exc}"
            try:
                self.signals.connection_changed.emit(self.connected, self.status)
            except Exception as signal_exc:
                self._write_emergency_log(
                    f"Could not publish command failure for {name}.",
                    exc=signal_exc,
                )
        finally:
            if ack is not None:
                try:
                    ack.set()
                except Exception:
                    pass

    def _drain_commands(self, max_commands: Optional[int] = None) -> None:
        processed = 0
        while max_commands is None or processed < max(0, int(max_commands)):
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            self._process_queued_command(name, payload)
            processed += 1

    def _handle_command(self, name: str, payload: dict[str, Any]) -> None:
        if self._storage_fault_active and name != "SHUTDOWN":
            self.status = (
                f"Command {name} blocked: SQLite storage is unavailable and trading is fail-closed "
                "until the watchdog completes a full-process restart."
            )
            self.emit_snapshot(force=True, refresh_database=False)
            return
        if name == "WATCHDOG_AUTO_RECOVER":
            self._prepare_watchdog_auto_recovery(dict(payload.get("request") or {}))
        elif name == "CONNECT":
            self._connect(payload["settings"])
        elif name == "START_PLATFORM":
            self._start_ibkr_platform(payload["settings"])
        elif name == "DISCONNECT":
            self._disconnect()
        elif name == "START_STRATEGY":
            self.connection = payload["connection"]
            self.strategy = payload["strategy"]
            self.storage.save_connection_settings(self.connection)
            self.storage.save_strategy_settings(self.strategy)
            if not self.connected:
                if not self._connect(self.connection):
                    return
            if not self._require_broker_operation_connectivity("Start strategy"):
                return
            if self._upstream_recovery_pending and not self._recover_upstream_session_if_needed():
                self.status = "Start strategy blocked: post-reconnect broker reconciliation has not completed."
                self._log("WARN", self.status, self.active_cycle)
                self.emit_snapshot(force=True)
                return
            self._start_strategy(self.strategy)
        elif name == "SAVE_DRAFT_SETTINGS":
            previous_market_data_type = self.connection.market_data_type
            self.connection = payload["connection"]
            self.strategy = payload["strategy"]
            self.storage.save_connection_settings(self.connection)
            self.storage.save_strategy_settings(self.strategy)
            self._apply_active_strategy_edits(self.strategy)
            if self.connected and previous_market_data_type != self.connection.market_data_type:
                self.adapter.set_market_data_type(self.connection.market_data_type)
                self.price_snapshot = None
                self._last_price_poll_monotonic = 0.0
                self._log("INFO", f"Market data mode changed to {self.connection.market_data_type}; market data subscriptions refreshed.")
        elif name == "SEARCH_CONTRACTS":
            self._search_contracts(payload["connection"], str(payload.get("query", "")))
        elif name == "CONFIRM_TICKER_PRICE":
            self.connection = payload["connection"]
            self.strategy = payload["strategy"]
            self.storage.save_connection_settings(self.connection)
            self.storage.save_strategy_settings(self.strategy)
            if not self.connected:
                if not self._connect(self.connection):
                    return
            if not self._require_broker_operation_connectivity(
                "Confirm ticker/price",
                require_reconciliation_complete=True,
            ):
                return
            self._confirm_ticker_price(self.strategy)
        elif name == "STOP_ACTION":
            self._apply_stop_action(payload["action"])
        elif name == "REFRESH_HISTORY":
            self.signals.history_updated.emit(self.storage.history_cycles(payload.get("ticker", "")))
        elif name == "REFRESH_BROKER_STATE":
            self._refresh_broker_state_for_recovery()
        elif name == "RESUME_RECOVERY_MONITORING":
            self._resume_recovery_monitoring()
        elif name == "MARK_RECOVERY_MANUALLY_HANDLED":
            self._mark_recovery_manually_handled(str(payload.get("note") or ""))
        elif name == "CANCEL_RECOVERY_APP_ORDER":
            self._cancel_recovery_app_order()
        elif name == "CHECKPOINT_RESUME_STATE":
            result = payload.get("_checkpoint_result")
            self.connection = payload["connection"]
            self.strategy = payload["strategy"]
            self._apply_active_strategy_edits(self.strategy, reevaluate_market_state=False)
            checkpoint = self.storage.save_resume_checkpoint(
                self.connection,
                self.strategy,
                self.active_cycle,
                reason=str(payload.get("reason") or "application_shutdown"),
                checkpoint_id=str(payload["checkpoint_id"]),
            )
            if isinstance(result, dict):
                result["ok"] = True
                result["checkpoint"] = checkpoint
            # Queue the non-critical online backup behind the acknowledged
            # checkpoint. _drain_commands() sets the caller's acknowledgement
            # before processing this next command, keeping the Windows session
            # callback bounded by the atomic SQLite save rather than backup I/O.
            self._commands.put(
                (
                    "CREATE_DATABASE_BACKUP",
                    {"reason": f"resume_{payload.get('reason') or 'application_shutdown'}"},
                )
            )
        elif name == "CREATE_DATABASE_BACKUP":
            reason = str(payload.get("reason") or "manual")
            try:
                self.storage.backup_database(reason)
            except Exception as exc:
                self._log("WARN", f"Could not create database backup ({reason}): {exc}")
        elif name == "SHUTDOWN":
            self._auto_reconnect_enabled = False
            self._stop_event.set()

    @staticmethod
    def _watchdog_stage_text(cycle: Optional[CycleState]) -> str:
        stage = getattr(cycle, "stage", None)
        return str(getattr(stage, "value", stage) or "")

    def _watchdog_request_mismatch(
        self,
        request: dict[str, Any],
        cycle: Optional[CycleState],
    ) -> Optional[str]:
        if cycle is None:
            return "the expected SQLite cycle no longer exists"
        if not bool(request.get("was_monitoring_active")):
            return "the final worker snapshot did not prove that strategy monitoring was active"
        if bool(request.get("startup_resume_required")):
            return "the cycle already required an explicit operator Start before the watchdog incident"
        if bool(request.get("recovery_required")):
            return "broker/local reconciliation was already required before the watchdog incident"
        if bool(getattr(cycle, "recovery_required", False)):
            return "the persisted cycle requires manual reconciliation"
        expected_signature = request.get("expected_cycle_signature")
        if not isinstance(expected_signature, dict) or not expected_signature:
            return "the restart handoff does not contain an exact broker-relevant cycle signature"
        if recovery_cycle_signature(cycle) != expected_signature:
            return "broker-relevant persisted cycle facts changed since the final healthy worker snapshot"
        expected_id = str(request.get("expected_cycle_id") or "").strip()
        if not expected_id or str(cycle.id) != expected_id:
            return "the persisted cycle ID does not match the one-time restart handoff"
        expected_stage = str(request.get("expected_cycle_stage") or "").strip()
        actual_stage = self._watchdog_stage_text(cycle)
        allowed = {
            Stage.WAIT_INITIAL_DROP.value,
            Stage.BUY_TRAIL_ACTIVE.value,
            Stage.WAIT_RISE_TRIGGER.value,
            Stage.SELL_TRAIL_ACTIVE.value,
        }
        if expected_stage not in allowed or actual_stage != expected_stage:
            return f"the persisted stage changed from {expected_stage or '-'} to {actual_stage or '-'}"
        expected_ticker = str(request.get("ticker") or "").upper().strip()
        if expected_ticker and str(cycle.ticker or "").upper().strip() != expected_ticker:
            return "the persisted ticker changed"
        expected_con_id = request.get("con_id")
        try:
            expected_con_id_value = int(expected_con_id or 0)
        except Exception:
            expected_con_id_value = 0
        try:
            actual_con_id_value = int(cycle.con_id or 0)
        except Exception:
            actual_con_id_value = 0
        if expected_con_id_value > 0 and actual_con_id_value != expected_con_id_value:
            return "the exact IBKR conId changed"
        refs = request.get("order_refs") or {}
        if isinstance(refs, dict):
            for label, attribute in (
                ("buy", "buy_order_ref"),
                ("protective_sell", "protective_sell_order_ref"),
                ("sell", "sell_order_ref"),
            ):
                expected_ref = str(refs.get(label) or "").strip()
                if expected_ref and str(getattr(cycle, attribute, "") or "").strip() != expected_ref:
                    return f"the persisted {label.replace('_', ' ')} OrderRef changed"
        return None

    def _reject_watchdog_auto_recovery(self, reason: str) -> None:
        self._watchdog_auto_recovery_pending = False
        self.status = (
            "Watchdog restart completed, but automatic strategy recovery was blocked: "
            f"{reason}. Use the Reconciliation screen before resuming manually."
        )
        self._recovery_required = True
        self._log("ERROR", self.status, self.active_cycle)
        self.emit_snapshot(force=True, refresh_database=False)

    def _prepare_watchdog_auto_recovery(self, request: dict[str, Any]) -> None:
        """Validate the one-time handoff before connecting or resuming anything."""
        self._watchdog_auto_recovery_request = dict(request or {})
        eligible = bool(
            request.get("auto_resume")
            and request.get("was_monitoring_active")
            and not request.get("startup_resume_required")
            and not request.get("recovery_required")
        )
        if not eligible:
            self._watchdog_auto_recovery_pending = False
            self.status = (
                "Watchdog restart completed. No exact active cycle was eligible for "
                "automatic recovery; BouncyBot remains stopped."
            )
            self._log("WARN", self.status, self.active_cycle)
            self.emit_snapshot(force=True, refresh_database=False)
            return
        expected_id = str(request.get("expected_cycle_id") or "").strip()
        cycle = self.storage.get_cycle(expected_id) if expected_id else None
        latest = self.storage.get_latest_active_cycle()
        mismatch = self._watchdog_request_mismatch(request, cycle)
        if mismatch is None and (latest is None or str(latest.id) != expected_id):
            mismatch = "a different active SQLite cycle is now the newest recovery target"
        if mismatch is not None:
            self.active_cycle = latest or cycle or self.active_cycle
            self._reject_watchdog_auto_recovery(mismatch)
            return

        assert cycle is not None
        self.active_cycle = cycle
        self.strategy = self._settings_for_repeat_cycle(cycle)
        self._startup_resume_required = True
        self._watchdog_auto_recovery_pending = True
        self._auto_reconnect_enabled = True
        self.status = (
            f"Watchdog restart recovered exact cycle {cycle.cycle_number} for {cycle.ticker}. "
            "Connecting and reconciling broker state before monitoring resumes."
        )
        self._log("WARN", self.status, cycle)

        if not self.connected or not self.adapter.is_connected():
            try:
                self._connect(self.connection)
            except BrokerAdapterError:
                # _connect leaves fixed-interval reconnect enabled. The pending
                # exact-cycle request remains fail-closed until connectivity is
                # restored and the broker cadence can reconcile it.
                self.emit_snapshot(force=True, refresh_database=False)
                return
        self._attempt_watchdog_auto_recovery()
        self.emit_snapshot(force=True, refresh_database=False)

    def _attempt_watchdog_auto_recovery(self) -> bool:
        """Run the existing Start/reconciliation path for one exact cycle only."""
        if not self._watchdog_auto_recovery_pending or self._storage_fault_active:
            return False
        request = dict(self._watchdog_auto_recovery_request or {})
        expected_id = str(request.get("expected_cycle_id") or "").strip()
        try:
            cycle = self.storage.get_cycle(expected_id) if expected_id else None
            latest = self.storage.get_latest_active_cycle()
        except sqlite3.Error as exc:
            self._enter_storage_fault("validate watchdog recovery cycle", exc)
            return False
        mismatch = self._watchdog_request_mismatch(request, cycle)
        if mismatch is None and (latest is None or str(latest.id) != expected_id):
            mismatch = "another active SQLite cycle replaced the expected recovery target"
        if mismatch is not None:
            self.active_cycle = latest or cycle or self.active_cycle
            self._reject_watchdog_auto_recovery(mismatch)
            return False
        if not self.connected or not self.adapter.is_connected():
            return False
        connectivity = self._adapter_connectivity_snapshot()
        self._broker_connectivity = connectivity
        if connectivity.get("upstream_connected") is not True or self._upstream_recovery_pending:
            return False

        assert cycle is not None
        self.active_cycle = cycle
        settings = self._settings_for_repeat_cycle(cycle)
        self.strategy = settings
        try:
            self._start_strategy(settings)
        except sqlite3.Error as exc:
            self._enter_storage_fault("watchdog strategy recovery", exc)
            return False
        except BrokerAdapterError as exc:
            self._handle_broker_connection_problem(exc)
            return False
        except Exception as exc:
            self._watchdog_auto_recovery_pending = False
            self._recovery_required = True
            self.status = (
                "Watchdog restart could not safely resume the exact cycle: "
                f"{exc}. Manual reconciliation is required."
            )
            self._write_emergency_log(
                "Unexpected exception during watchdog exact-cycle recovery.",
                exc=exc,
                context={"expected_cycle_id": expected_id},
            )
            self._log("ERROR", self.status, self.active_cycle)
            self.emit_snapshot(force=True, refresh_database=False)
            return False

        current = self.active_cycle
        active_stages = {
            Stage.WAIT_INITIAL_DROP,
            Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER,
            Stage.SELL_TRAIL_ACTIVE,
            Stage.CYCLE_COMPLETE,
        }
        if (
            current is not None
            and current.stage in active_stages
            and not self._startup_resume_required
            and not self._recovery_required
        ):
            self._watchdog_auto_recovery_pending = False
            self.status = (
                f"Watchdog restart and broker reconciliation completed for {current.ticker}; "
                f"monitoring resumed in stage {current.stage.value}."
            )
            self._log("INFO", self.status, current)
            self.emit_snapshot(force=True, refresh_database=False)
            return True

        self._watchdog_auto_recovery_pending = False
        self.status = (
            "Watchdog restart completed, but broker reconciliation did not establish "
            "a safe resumable state. Manual reconciliation is required."
        )
        self._recovery_required = True
        self._log("ERROR", self.status, current)
        self.emit_snapshot(force=True, refresh_database=False)
        return False

    def _platform_name(self, settings: Optional[ConnectionSettings] = None) -> str:
        settings = settings or self.connection
        return platform_label(getattr(settings, "platform", "tws"))

    def _adapter_connectivity_snapshot(self) -> dict[str, Any]:
        """Normalize local API and Gateway-to-IBKR connectivity facts."""
        is_connected_method = getattr(self.adapter, "is_connected", None)
        if callable(is_connected_method):
            try:
                local_connected = bool(is_connected_method())
            except Exception:
                local_connected = bool(self.connected)
        else:
            local_connected = bool(self.connected)
        # Direct unit tests and legacy duck-typed adapters may set the controller
        # flag without running the real connect command.  Production connections
        # always initialize the dedicated connectivity snapshot.
        if self.connected and not self._broker_connectivity_initialized and getattr(self.adapter, "ib", object()) is None:
            local_connected = True
        method = getattr(self.adapter, "connectivity_status", None)
        use_status_method = callable(method) and not (
            self.connected
            and not self._broker_connectivity_initialized
            and getattr(self.adapter, "ib", object()) is None
        )
        if use_status_method:
            try:
                raw = method()
                if hasattr(raw, "to_dict"):
                    data = raw.to_dict()
                elif isinstance(raw, dict):
                    data = dict(raw)
                else:
                    data = {}
            except Exception as exc:
                data = {
                    "local_connected": local_connected,
                    "upstream_connected": None if local_connected else False,
                    "state": "connectivity_status_error",
                    "message": f"Could not read broker connectivity status: {exc}",
                    "error_code": None,
                }
        else:
            # Compatibility for deterministic test adapters written before the
            # production adapter exposed IBKR upstream-connectivity state.
            data = {
                "local_connected": local_connected,
                "upstream_connected": local_connected,
                "state": "connected" if local_connected else "local_disconnected",
                "message": "Broker API connection is ready." if local_connected else "Broker API connection is disconnected.",
                "error_code": None,
            }
        data["local_connected"] = bool(data.get("local_connected", local_connected))
        upstream = data.get("upstream_connected")
        if upstream not in {True, False, None}:
            upstream = bool(upstream)
        if not data["local_connected"]:
            upstream = False
        data["upstream_connected"] = upstream
        data.setdefault("state", "connected" if upstream is True else "upstream_unknown")
        data.setdefault("message", "")
        data.setdefault("error_code", None)
        data.setdefault("changed_at", "")
        data.setdefault("market_data_resubscribe_required", False)
        data.setdefault("awaiting_fresh_market_data", False)
        data.setdefault("market_data_event_tracking", False)
        data["trading_ready"] = bool(data["local_connected"] and upstream is True)
        return data

    def _refresh_broker_connectivity_snapshot(self, *, detect_transition: bool = True) -> dict[str, Any]:
        previous = dict(self._broker_connectivity or {})
        current = self._adapter_connectivity_snapshot()
        self._broker_connectivity = current
        self._last_broker_refresh_monotonic = time.monotonic()
        if not self._broker_connectivity_initialized:
            self._broker_connectivity_initialized = True
            return current
        if not detect_transition:
            return current

        previous_upstream = previous.get("upstream_connected")
        current_upstream = current.get("upstream_connected")
        previous_state = str(previous.get("state") or "")
        current_state = str(current.get("state") or "")
        if current_upstream is False and (previous_upstream is not False or previous_state != current_state):
            self._handle_upstream_connectivity_lost(current)
        elif current_upstream is True and previous_upstream is False:
            self._handle_upstream_connectivity_restored(current)
        return current

    def _invalidate_market_data_freshness(self, reason: str) -> None:
        """Make cached quote fields ineligible until a new update event arrives."""
        self._api_data_invalidated = True
        self._api_data_invalidated_reason = str(reason or "Market-data freshness was invalidated.")
        self._api_data_invalidated_at = utc_now_iso()
        self._stage3_sell_confirmation.clear()
        # Keep the last consumed event token. Clearing it here would let another
        # read of the same cached event sequence masquerade as the first fresh
        # update after invalidation. A genuine update changes the sequence, and a
        # recreated subscription changes the subscription id.
        if self.price_snapshot is not None:
            self.price_snapshot["strategy_price_usable"] = False
            self.price_snapshot["api_data_received_in_latest_read"] = False
            self.price_snapshot["api_data_state"] = "invalidated"
            self.price_snapshot["api_data_indicator_text"] = self._api_data_invalidated_reason

    def _handle_upstream_connectivity_lost(self, status: dict[str, Any]) -> None:
        message = str(status.get("message") or "IB Gateway/TWS lost connectivity to IBKR servers.")
        code = status.get("error_code")
        code_text = f" (IBKR {code})" if code not in (None, "") else ""
        self._invalidate_market_data_freshness(f"IBKR server connectivity lost{code_text}; cached quotes are invalid.")
        if self._last_recovery_probe:
            probe = dict(self._last_recovery_probe)
            probe["invalidated_at"] = utc_now_iso()
            probe["invalidation_reason"] = "IBKR upstream connectivity was lost after this probe."
            self._last_recovery_probe = probe
        self._upstream_recovery_pending = False
        self.status = (
            f"{self._platform_name()} is locally connected, but its IBKR server connection is unavailable{code_text}. "
            "Trading and broker-state polling are paused."
        )
        self.signals.connection_changed.emit(False, self.status)
        self._update_audit_condition(
            key="broker_reconnect",
            family="IBKR connection outage",
            reason_code="UPSTREAM_DISCONNECTED",
            message=f"{self.status} {message}",
            cycle=self.active_cycle,
            initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
            repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
            summary_level="WARN",
            first_summary_level="INFO",
            immediate_level="WARN",
            immediate_token="outage_started",
            context={"error_code": code, "connectivity": dict(status)},
            kind="broker_reconnect",
        )

    def _handle_upstream_connectivity_restored(self, status: dict[str, Any]) -> None:
        message = str(status.get("message") or "IBKR server connectivity was restored.")
        code = status.get("error_code")
        code_text = f" (IBKR {code})" if code not in (None, "") else ""
        self._invalidate_market_data_freshness(
            f"IBKR server connectivity restored{code_text}; waiting for a post-recovery market-data update."
        )
        self._upstream_recovery_pending = True
        self._last_upstream_recovery_attempt_monotonic = 0.0
        self.status = (
            f"IBKR server connectivity restored{code_text}. Revalidating app-owned orders/executions and market data before trading resumes."
        )
        self.signals.connection_changed.emit(True, self.status)
        cleared = self._clear_audit_condition(
            "broker_reconnect",
            cycle=self.active_cycle,
            recovery_message=f"{self.status} {message}",
            context={"error_code": code, "connectivity": dict(status)},
        )
        if cleared is None or int(cleared.get("audit_emissions", 0) or 0) <= 0:
            self._log("INFO", f"{self.status} {message}", self.active_cycle)

    def _upstream_trading_ready(self) -> bool:
        status = self._adapter_connectivity_snapshot()
        self._broker_connectivity = status
        return bool(self.connected and status.get("trading_ready"))

    def _broker_operation_connectivity_message(
        self,
        operation: str,
        *,
        require_reconciliation_complete: bool = False,
    ) -> Optional[str]:
        """Return a fail-closed reason before a broker-side operation.

        The local API socket can remain open while IB Gateway/TWS has lost its
        server connection.  Pump callbacks at the operation boundary so a queued
        1100/2110 notification is observed before the app sends a request.
        Risk-reducing recovery actions may run while post-reconnect reconciliation
        is pending, but new orders must wait until that reconciliation completes.
        """
        process_events = getattr(self.adapter, "process_events", None)
        if self.connected and callable(process_events):
            try:
                process_events(0.0)
            except Exception:
                pass
            self._drain_broker_events()
        status = self._adapter_connectivity_snapshot()
        self._broker_connectivity = status
        operation_label = str(operation or "Broker operation").strip()
        if not self.connected or not bool(status.get("local_connected")):
            return f"{operation_label} blocked: the local IB Gateway/TWS API connection is not active."
        if status.get("upstream_connected") is not True:
            detail = str(status.get("message") or "Gateway-to-IBKR server connectivity is unavailable.")
            code = status.get("error_code")
            code_text = f" IBKR code {code}." if code not in (None, "") else ""
            return f"{operation_label} blocked: IBKR server connectivity is not confirmed.{code_text} {detail}".strip()
        if require_reconciliation_complete and self._upstream_recovery_pending:
            return f"{operation_label} blocked: post-reconnect broker reconciliation is still in progress."
        return None

    def _require_broker_operation_connectivity(
        self,
        operation: str,
        *,
        require_reconciliation_complete: bool = False,
    ) -> bool:
        """Log and expose a connectivity block for a GUI-requested operation."""
        message = self._broker_operation_connectivity_message(
            operation,
            require_reconciliation_complete=require_reconciliation_complete,
        )
        if not message:
            return True
        self.status = message
        self._log("WARN", message, self.active_cycle)
        self.emit_snapshot(force=True)
        return False

    def _order_submission_connectivity_message(self, side: str) -> Optional[str]:
        side_label = str(side or "ORDER").upper()
        return self._broker_operation_connectivity_message(
            f"{side_label} order",
            require_reconciliation_complete=True,
        )

    def _connect(self, settings: ConnectionSettings) -> bool:
        errors = settings.validate()
        if errors:
            raise ValueError(" ".join(errors))
        self.connection = settings
        self.storage.save_connection_settings(settings)
        target = self._platform_name(settings)
        self.status = f"Connecting to {target} API at {settings.host}:{settings.port}..."
        self.signals.connection_changed.emit(False, self.status)
        # A user-requested Connect enables the same fixed retry policy as a
        # previously healthy connection.  If the first socket attempt fails,
        # the worker will keep retrying every ten seconds until the operator
        # disconnects or shuts down the app.
        self._auto_reconnect_enabled = True
        try:
            self.adapter.connect(settings.host, settings.port, settings.client_id, settings.market_data_type)
        except Exception as exc:
            self.connected = False
            self._reconnect_failures = 1
            self._last_reconnect_attempt_monotonic = time.monotonic()
            helper = connection_helper_text(settings.normalized_platform(), settings.host, settings.port, exc)
            self.status = (
                f"{target} connection unavailable. {helper} BouncyBot will retry every "
                f"{self.RECONNECT_INTERVAL_SECONDS:g} seconds until connected or manually disconnected."
            )
            self._update_audit_condition(
                key="broker_reconnect",
                family="IBKR connection outage",
                reason_code="INITIAL_CONNECT_FAILED",
                message=self.status,
                cycle=self.active_cycle,
                initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                summary_level="WARN",
                first_summary_level="INFO",
                immediate_level="WARN",
                immediate_token="outage_started",
                context={
                    "attempt_count": self._reconnect_failures,
                    "host": settings.host,
                    "port": settings.port,
                },
                kind="broker_reconnect",
            )
            self.signals.connection_changed.emit(False, self.status)
            raise BrokerAdapterError(self.status) from exc
        failed_attempts = int(self._reconnect_failures)
        self._reconnect_failures = 0
        self._last_reconnect_attempt_monotonic = 0.0
        self.connected = True
        self._reset_price_feed_after_reconnect()
        process_events = getattr(self.adapter, "process_events", None)
        if callable(process_events):
            try:
                process_events(0.0)
            except Exception:
                pass
        self._drain_broker_events()
        connectivity = self._refresh_broker_connectivity_snapshot(detect_transition=False)
        if connectivity.get("upstream_connected") is not True:
            code = connectivity.get("error_code")
            code_text = f" (IBKR {code})" if code not in (None, "") else ""
            detail = str(connectivity.get("message") or "Gateway-to-IBKR server connectivity is not confirmed.")
            self.status = (
                f"Connected locally to {target} {settings.host}:{settings.port}, but the IBKR server link is unavailable{code_text}. "
                "Trading and broker recovery are paused."
            )
            self._update_audit_condition(
                key="broker_reconnect",
                family="IBKR connection outage",
                reason_code="UPSTREAM_UNAVAILABLE_AFTER_CONNECT",
                message=self.status,
                cycle=self.active_cycle,
                initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                summary_level="WARN",
                first_summary_level="INFO",
                immediate_level="WARN",
                immediate_token="outage_started",
                context={"connectivity": connectivity, "detail": detail},
                kind="broker_reconnect",
            )
            self.signals.connection_changed.emit(False, self.status)
            self.emit_snapshot(force=True)
            return False
        self._refresh_display_accounts()
        self.status = f"Connected to {target} {settings.host}:{settings.port} ({settings.trading_mode})"
        cleared = self._clear_audit_condition(
            "broker_reconnect",
            cycle=self.active_cycle,
            recovery_message=self.status,
            context={
                "host": settings.host,
                "port": settings.port,
                "failed_attempts": failed_attempts,
            },
        )
        if cleared is None or int(cleared.get("audit_emissions", 0) or 0) <= 0:
            self.storage.add_event("INFO", self.status)
        self.signals.connection_changed.emit(True, self.status)
        if self._upstream_recovery_pending:
            if not self._recover_upstream_session_if_needed():
                self.emit_snapshot(force=True)
                return False
        elif self._startup_resume_required and self.active_cycle is not None:
            self.status = (
                f"Connected. Stored active cycle found for {self.active_cycle.ticker}; "
                "click 4. Start strategy to resume monitoring/recovery."
            )
            self._log("WARN", self.status, self.active_cycle)
        else:
            self._recover_after_connect()
        self.emit_snapshot(force=True)
        return True

    def _start_ibkr_platform(self, settings: ConnectionSettings) -> None:
        self.connection = settings
        self.storage.save_connection_settings(settings)
        target = self._platform_name(settings)
        result = launch_platform(settings.normalized_platform(), settings.platform_path)
        level = "INFO" if result.started else "WARN"
        self.status = result.message
        self.storage.add_event(level, self.status, raw={"platform": settings.normalized_platform(), "path": result.executable})
        self.signals.connection_changed.emit(False, self.status)
        # After launching, keep trying to connect. This lets the user complete
        # login/2FA while the bot waits for the API socket to become available.
        if result.started:
            self._auto_reconnect_enabled = True
            self.connected = False
            self._reconnect_failures = 0
            self._last_reconnect_attempt_monotonic = 0.0
            self._log(
                "INFO",
                f"Waiting for {target} login/API socket; BouncyBot will retry every "
                f"{self.RECONNECT_INTERVAL_SECONDS:g} seconds until connected or manually disconnected.",
            )
        self.emit_snapshot(force=True)

    def _disconnect(self) -> None:
        self._auto_reconnect_enabled = False
        self._clear_audit_condition(
            "broker_reconnect",
            cycle=self.active_cycle,
            emit_recovery=False,
        )
        if self.adapter.is_connected():
            self.adapter.disconnect()
        self.connected = False
        self._broker_display_accounts = []
        self._broker_connectivity = self._adapter_connectivity_snapshot()
        self._broker_connectivity_initialized = True
        self._upstream_recovery_pending = False
        self._invalidate_market_data_freshness("The broker API connection was closed.")
        if self._last_recovery_probe:
            probe = dict(self._last_recovery_probe)
            probe["invalidated_at"] = utc_now_iso()
            probe["invalidation_reason"] = "The broker API connection was closed after this probe."
            self._last_recovery_probe = probe
        self.status = "Disconnected"
        self.storage.add_event("INFO", self.status)
        self.signals.connection_changed.emit(False, self.status)
        self.emit_snapshot(force=True)

    @staticmethod
    def _polled_order_snapshot(order: Any) -> dict[str, Any]:
        return {
            "order_ref": getattr(order, "order_ref", "") or "",
            "order_id": getattr(order, "order_id", None),
            "perm_id": getattr(order, "perm_id", None),
            "status": getattr(order, "status", "") or "",
            "filled": getattr(order, "filled", None),
            "remaining": getattr(order, "remaining", None),
            "avg_fill_price": getattr(order, "avg_fill_price", None),
            "commission": getattr(order, "commission", None),
            "raw": getattr(order, "raw", {}) or {},
        }

    @staticmethod
    def _polled_order_is_working(order: Any) -> bool:
        status = str(getattr(order, "status", "") or "").strip().lower()
        if status in {"filled", "cancelled", "apicancelled", "inactive", "rejected"}:
            return False
        try:
            remaining = float(getattr(order, "remaining", 0.0))
        except Exception:
            remaining = 0.0
        try:
            filled = float(getattr(order, "filled", 0.0))
        except Exception:
            filled = 0.0
        return remaining > 0 or (remaining == 0 and filled <= 0 and status not in {"", "filled"})

    def _update_recovery_probe_from_order_poll(self, order: Any) -> None:
        """Supersede stale recovery-probe order rows with newer broker polls.

        The Recovery screen's broker probe is a point-in-time snapshot. Normal
        order monitoring can later receive a fill, cancellation, or updated
        remaining quantity. Keeping the older row would falsely present a
        completed cycle as having a working order until the operator manually
        refreshed the probe.
        """
        if not self._last_recovery_probe:
            return
        updated = self._polled_order_snapshot(order)

        def same_order(row: dict[str, Any]) -> bool:
            for key in ("order_ref", "order_id", "perm_id"):
                left = row.get(key)
                right = updated.get(key)
                if left in (None, "") or right in (None, ""):
                    continue
                if str(left).strip() == str(right).strip():
                    return True
            return False

        working = self._polled_order_is_working(order)
        rows: list[dict[str, Any]] = []
        matched = False
        for row in self._last_recovery_probe.get("open_app_orders") or []:
            if not isinstance(row, dict):
                continue
            if same_order(row):
                matched = True
                if working:
                    rows.append(updated)
                continue
            rows.append(row)
        if working and not matched:
            rows.append(updated)
        probe = dict(self._last_recovery_probe)
        probe["open_app_orders"] = rows
        probe["open_order_refs"] = sorted(
            {str(row.get("order_ref") or "") for row in rows if str(row.get("order_ref") or "")}
        )
        probe["order_state_updated_at"] = utc_now_iso()
        self._last_recovery_probe = probe

    @staticmethod
    def _execution_summary_for_recovery(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "execution_id": row.get("execution_id") or row.get("execId") or row.get("exec_id"),
            "order_ref": row.get("order_ref") or row.get("orderRef"),
            "order_id": row.get("order_id") or row.get("orderId"),
            "perm_id": row.get("perm_id") or row.get("permId"),
            "side": row.get("side") or row.get("action"),
            "shares": row.get("shares") or row.get("quantity"),
            "price": row.get("price") or row.get("avg_price"),
            "time": row.get("time") or row.get("executed_at") or row.get("timestamp"),
            "account": row.get("account") or row.get("acctNumber") or row.get("acct_number"),
        }

    def _capture_recovery_probe(self, cycle: Optional[CycleState], open_orders: list[Any], *, error: str = "") -> None:
        """Store one complete broker/local reconciliation probe for the GUI.

        The local signature lets the GUI invalidate a recent probe after an
        order, fill, stage, or recovery change without treating price-only cycle
        updates as stale. This remains display/audit state and does not modify
        or cancel broker orders.
        """
        probe: dict[str, Any] = {
            "checked_at": utc_now_iso(),
            "connected": bool(self.connected),
            "error": error,
            "open_app_orders": [self._polled_order_snapshot(order) for order in (open_orders or [])],
            "open_order_refs": sorted({str(getattr(order, "order_ref", "") or "") for order in (open_orders or []) if getattr(order, "order_ref", "")}),
            "local_cycle_signature": recovery_cycle_signature(cycle),
        }
        if cycle is not None:
            probe["cycle_id"] = cycle.id
            probe["cycle_ticker"] = cycle.ticker
            try:
                contract = self.contract
                if contract is None or getattr(contract, "ticker", "").upper() != str(cycle.ticker).upper():
                    contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
                    self.contract = contract
                method = getattr(self.adapter, "position_size", None)
                if callable(method):
                    probe["position_size"] = method(contract, account=cycle.account)
                    probe["position_account"] = cycle.account
            except Exception as exc:
                probe["position_error"] = str(exc)
            try:
                recent = self._adapter_recent_executions()[:10]
                probe["recent_executions"] = [self._execution_summary_for_recovery(row) for row in recent]
                if recent:
                    self._remember_recovery_account_values(row.get("account") for row in recent)
            except Exception as exc:
                probe["recent_executions_error"] = str(exc)
        refresh_succeeded = bool(probe["connected"]) and not any(
            str(probe.get(key) or "").strip()
            for key in ("error", "position_error", "recent_executions_error")
        )
        if refresh_succeeded:
            self._last_successful_recovery_refresh_at = str(probe["checked_at"])
        probe["last_successful_checked_at"] = self._last_successful_recovery_refresh_at or None
        self._remember_recovery_account_values([probe.get("position_account")])
        self._last_recovery_probe = probe

    def _refresh_broker_state_for_recovery(self) -> None:
        self.active_cycle = self.storage.get_latest_active_cycle()
        if not self.connected:
            self.status = "Recovery broker refresh requires an active IBKR API connection."
            self._last_recovery_probe = {
                "checked_at": utc_now_iso(),
                "connected": False,
                "error": self.status,
                "open_app_orders": [],
                "open_order_refs": [],
                "cycle_id": self.active_cycle.id if self.active_cycle is not None else None,
                "cycle_ticker": self.active_cycle.ticker if self.active_cycle is not None else None,
                "local_cycle_signature": recovery_cycle_signature(self.active_cycle),
                "last_successful_checked_at": self._last_successful_recovery_refresh_at or None,
            }
            self._log("WARN", self.status, self.active_cycle)
            self.emit_snapshot(force=True)
            return
        connectivity_message = self._broker_operation_connectivity_message("Recovery broker refresh")
        if connectivity_message:
            self.status = connectivity_message
            self._last_recovery_probe = {
                "checked_at": utc_now_iso(),
                "connected": True,
                "upstream_connected": self._broker_connectivity.get("upstream_connected"),
                "error": self.status,
                "open_app_orders": [],
                "open_order_refs": [],
                "cycle_id": self.active_cycle.id if self.active_cycle is not None else None,
                "cycle_ticker": self.active_cycle.ticker if self.active_cycle is not None else None,
                "local_cycle_signature": recovery_cycle_signature(self.active_cycle),
                "last_successful_checked_at": self._last_successful_recovery_refresh_at or None,
            }
            self._log("WARN", self.status, self.active_cycle)
            self.emit_snapshot(force=True)
            return
        try:
            open_orders = self._local_open_app_orders(self.adapter.open_app_orders())
            self._capture_recovery_probe(self.active_cycle, open_orders)
            self._recovery_required = bool(self.active_cycle is None and open_orders)
            self.status = "Broker state refreshed for Recovery screen."
            self._log("INFO", self.status, self.active_cycle)
        except Exception as exc:
            self.status = f"Recovery broker refresh failed: {exc}"
            self._capture_recovery_probe(self.active_cycle, [], error=str(exc))
            self._log("WARN", self.status, self.active_cycle)
        self.emit_snapshot(force=True)

    def _resume_recovery_monitoring(self) -> None:
        if not self.connected:
            if not self._connect(self.connection):
                return
        if not self._require_broker_operation_connectivity("Resume recovery monitoring"):
            return
        if bool(getattr(self, "_stale_active_cycle_detected", False)):
            self._log("WARN", "Operator requested recovery resume for a stale startup cycle after broker/local review.", self.active_cycle)
        self._startup_resume_required = False
        self._stale_active_cycle_detected = False
        try:
            self._recover_after_connect()
        except Exception as exc:
            self._upstream_recovery_pending = True
            self.status = f"Recovery resume failed; monitoring remains paused: {exc}"
            self._log("WARN", self.status, self.active_cycle)
            self.emit_snapshot(force=True)
            return
        self._upstream_recovery_pending = False
        cycle = self.active_cycle
        if cycle is None:
            self.status = "No active SQLite cycle to resume."
            self._log("INFO", self.status)
        elif cycle.stage == Stage.MANUAL_REVIEW or bool(getattr(cycle, "recovery_required", False)) or self._recovery_required:
            self.status = "Recovery still requires manual review after broker refresh."
            self._log("WARN", self.status, cycle)
        else:
            self.status = f"Recovery resumed: monitoring {cycle.ticker} at {cycle.stage.value}."
            self._log("INFO", self.status, cycle)
        self.emit_snapshot(force=True)

    def _cancel_recovery_app_order(self) -> None:
        if not self.connected:
            self.status = "Cancelling visible app-owned orders requires an active IBKR API connection."
            self._log("WARN", self.status, self.active_cycle)
            self.emit_snapshot(force=True)
            return
        if not self._require_broker_operation_connectivity("Cancel visible app-owned orders"):
            return
        self.active_cycle = self.storage.get_latest_active_cycle()
        if self.active_cycle is not None:
            self._apply_stop_action(StopAction.CANCEL_OPEN_BOT_ORDERS)
            self.emit_snapshot(force=True)
            return
        try:
            open_orders = self._local_open_app_orders(self.adapter.open_app_orders())
            self._capture_recovery_probe(None, open_orders)
        except Exception as exc:
            self.status = f"Could not load app-owned open orders for cancellation: {exc}"
            self._log("ERROR", self.status)
            self.emit_snapshot(force=True)
            return
        if not open_orders:
            self._recovery_required = False
            self.status = "No app-owned open order is visible to cancel."
            self._log("INFO", self.status)
            self.emit_snapshot(force=True)
            return
        cancelled = 0
        for order in open_orders:
            ref = getattr(order, "order_ref", "") or ""
            order_id = getattr(order, "order_id", None)
            if not ref:
                continue
            try:
                if self._broker_mutation_blocked_by_storage_fault(
                    "Recovery order cancellation",
                    self.active_cycle,
                ):
                    break
                self.adapter.cancel_order(ref, order_id)
                cancelled += 1
                self.storage.add_event("WARN", f"Recovery cancel requested for orphan app-owned order {ref}.", raw={"order_ref": ref, "order_id": order_id})
            except Exception as exc:
                self.storage.add_event("ERROR", f"Recovery cancel failed for orphan app-owned order {ref}: {exc}", raw={"order_ref": ref, "order_id": order_id})
        self.status = f"Cancel requested for {cancelled} app-owned order(s) visible in recovery." if cancelled else "No cancellable app-owned order was found."
        try:
            refreshed = self._local_open_app_orders(self.adapter.open_app_orders())
            self._capture_recovery_probe(None, refreshed)
            self._recovery_required = bool(refreshed)
            if refreshed:
                self.status += f" {len(refreshed)} app-owned order(s) still visible after the cancel request."
        except Exception:
            self._recovery_required = not bool(cancelled)
        self.emit_snapshot(force=True)

    def _mark_recovery_manually_handled(self, note: str = "") -> None:
        cycle = self.active_cycle or self.storage.get_latest_active_cycle()
        if cycle is None:
            self._recovery_required = False
            self.status = "No active SQLite cycle to mark manually handled."
            self._log("INFO", self.status)
            self.emit_snapshot(force=True)
            return
        stage_before = cycle.stage.value
        message = "Recovery marked manually handled by operator. No broker order was sent by the app."
        clean_note = str(note or "").strip()
        if clean_note:
            message = f"{message} Note: {clean_note}"
        cycle.stage = Stage.STOPPED
        cycle.recovery_required = False
        cycle.close_position_market_requested = False
        cycle.close_before_rth_liquidation_requested = False
        cycle.close_before_rth_cancel_requested = False
        cycle.stop_after_current_cycle = True
        cycle.error_message = message
        cycle.touch()
        self.storage.upsert_cycle(cycle)
        self.storage.add_decision_event(
            event_type="MANUALLY_HANDLED",
            message=message,
            cycle=cycle,
            stage_before=stage_before,
            stage_after=Stage.STOPPED.value,
            decision_result="manual_operator_confirmed",
            raw={"operator_note": clean_note, "broker_recovery": self._last_recovery_probe},
        )
        self.storage.add_event("WARN", message, ticker=cycle.ticker, cycle_id=cycle.id, raw={"operator_note": clean_note})
        self.active_cycle = None
        self._recovery_required = False
        self.status = "Recovery marked manually handled; active cycle stopped in SQLite. No broker order was sent."
        self.emit_snapshot(force=True)

    def _local_open_app_orders(self, orders: list[PolledOrderState]) -> list[PolledOrderState]:
        """Filter a shared Master feed to exact OrderRefs known by this copy."""
        known = self.storage.known_order_refs()
        return [
            order
            for order in list(orders or [])
            if str(getattr(order, "order_ref", "") or "") in known
        ]

    def _recover_after_connect(self) -> None:
        """Reconcile SQLite state with app-owned TWS orders and executions.

        This method runs after the operator clicks Start for a stored cycle and
        after reconnects once monitoring has been explicitly resumed. It is the
        main recovery path after app restarts, TWS/Gateway restarts, and Windows
        restarts, but startup recovery is intentionally gated by the Start
        button. It intentionally refuses to create new orders when the state is
        unclear.
        """
        self._recovery_required = False
        self.active_cycle = self.storage.get_latest_active_cycle()
        open_orders = self._local_open_app_orders(self.adapter.open_app_orders())
        self._capture_recovery_probe(self.active_cycle, open_orders)
        open_refs = {o.order_ref for o in open_orders}
        if self.active_cycle is None:
            if open_orders:
                self._recovery_required = True
                self._log("WARN", "RECOVERY REQUIRED: app-owned open order found without matching active SQLite cycle. Manual review required.")
                self.status = "Recovery required: orphan app order found."
            return

        cycle = self.active_cycle
        if cycle.stage == Stage.BUY_TRAIL_ACTIVE:
            if cycle.buy_order_ref in open_refs:
                self._log("INFO", f"Recovered active BUY trailing order for {cycle.ticker}.", cycle)
            else:
                polled = self.adapter.poll_order(cycle.buy_order_ref or "") if cycle.buy_order_ref else None
                if polled and polled.filled > 0:
                    self._handle_buy_order_poll(cycle, polled)
                    recovered = self.active_cycle or cycle
                    if recovered.stage == Stage.BUY_TRAIL_ACTIVE:
                        self._log(
                            "INFO",
                            f"Recovered partial BUY fill for {cycle.ticker}; waiting for the original BUY order to become terminal.",
                            recovered,
                        )
                    else:
                        self._log(
                            "INFO",
                            f"Recovered settled BUY order for {cycle.ticker}; resumed minimum-profit stage.",
                            recovered,
                        )
                elif self._recover_buy_from_executions(cycle) is not None:
                    pass
                else:
                    self._mark_recovery_required(cycle, "SQLite expected active BUY order, but no matching TWS open order or recent app execution was found.")
        elif cycle.stage == Stage.SELL_TRAIL_ACTIVE:
            if cycle.sell_order_ref in open_refs:
                self._log("INFO", f"Recovered active SELL order for {cycle.ticker}.", cycle)
            else:
                polled = self.adapter.poll_order(cycle.sell_order_ref or "") if cycle.sell_order_ref else None
                close_workflow = bool(getattr(cycle, "close_before_rth_liquidation_requested", False)) or self._is_close_before_rth_market_order_ref(
                    cycle.sell_order_ref
                )
                if polled is not None and close_workflow:
                    self._handle_sell_order_poll(cycle, polled)
                    recovered_cycle = self.active_cycle
                    if recovered_cycle is not None and recovered_cycle.stage == Stage.CYCLE_COMPLETE:
                        self._log("INFO", f"Recovered completed SELL order for {cycle.ticker}.", recovered_cycle)
                elif polled and polled.filled > 0 and polled.remaining == 0:
                    cycle = StrategyEngine.on_sell_fill(cycle, polled.filled, polled.avg_fill_price, polled.status, polled.commission)
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                    self._log("INFO", f"Recovered completed SELL order for {cycle.ticker}.", cycle)
                    self._maybe_start_next_cycle()
                elif self._recover_sell_from_executions(cycle) is not None:
                    self._maybe_start_next_cycle()
                else:
                    self._mark_recovery_required(cycle, "SQLite expected active SELL order, but no matching TWS open order or recent app execution was found.")
        elif cycle.stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}:
            try:
                self.contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
            except Exception:
                pass
            self._check_position_for_waiting_cycle(cycle)
            if cycle.stage == Stage.WAIT_RISE_TRIGGER and cycle.protective_sell_order_ref:
                if cycle.protective_sell_order_ref in open_refs:
                    self._log("INFO", f"Recovered active protective SELL trailing order for {cycle.ticker}.", cycle)
                else:
                    polled = self.adapter.poll_order(cycle.protective_sell_order_ref) if cycle.protective_sell_order_ref else None
                    if polled and polled.filled > 0:
                        # Reuse the live poll gate so recovery cannot complete a
                        # protective exit until the broker reports the exact
                        # app-owned quantity with no remainder. Terminal partials
                        # and inconsistent quantities therefore fail closed in
                        # the same way as they do during uninterrupted trading.
                        self._handle_protective_sell_order_poll(cycle, polled)
                        recovered = self.active_cycle or cycle
                        if recovered.stage == Stage.CYCLE_COMPLETE:
                            self._log("INFO", f"Recovered protective SELL fill for {cycle.ticker}.", recovered)
                        elif recovered.stage == Stage.WAIT_RISE_TRIGGER:
                            self._log(
                                "INFO",
                                f"Recovered partial protective SELL fill for {cycle.ticker}: "
                                f"{int(polled.filled)} filled, {int(polled.remaining)} remaining. "
                                "Waiting for the protective order to reach a terminal status.",
                                recovered,
                            )
                        return
                    if self._recover_protective_sell_from_executions(cycle) is not None:
                        self._maybe_start_next_cycle()
                        return
                    self._mark_recovery_required(
                        cycle,
                        "SQLite expected an active protective SELL order, but no matching TWS open order or recent protective SELL execution was found.",
                    )
                    return
            self._log("INFO", f"Recovered local stage {cycle.stage.value} for {cycle.ticker}.", self.active_cycle or cycle)


    def _adapter_qualify_stock(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        primary_exchange: str = "",
        con_id: Optional[int] = None,
    ) -> QualifiedContract:
        try:
            return self.adapter.qualify_stock(ticker, exchange, currency, primary_exchange, con_id)
        except TypeError:
            # Compatibility test doubles and minimal adapters may omit con_id.
            return self.adapter.qualify_stock(ticker, exchange, currency, primary_exchange)

    def _validate_exact_contract_selection(
        self,
        settings: StrategySettings,
        *,
        claim_database_currency: bool = False,
    ) -> str:
        """Validate the exact SMART/STK/USD-or-EUR contract selected in the GUI."""
        ticker = settings.normalized_ticker()
        if not ticker:
            raise ValueError("Ticker is required.")
        if str(settings.exchange or "").upper().strip() != "SMART":
            raise ValueError("BouncyBot supports SMART-routed stock contracts only.")
        if str(settings.sec_type or "").upper().strip() != "STK":
            raise ValueError("Only ordinary STK contracts are supported.")
        currency = normalize_contract_currency(settings.currency, fallback="")
        if currency not in SUPPORTED_CONTRACT_CURRENCIES:
            raise ValueError("Select an exact USD or EUR stock contract from the IBKR API results.")
        try:
            con_id = int(settings.contract_con_id or 0)
        except Exception:
            con_id = 0
        exact_required = bool(getattr(self.adapter, "requires_exact_contract_selection", False))
        if settings.contract_con_id is not None and con_id <= 0:
            raise ValueError("IBKR conId must be blank or a positive integer.")
        if exact_required and con_id <= 0:
            raise ValueError(
                "Select an exact IBKR API contract result before confirming or starting; a positive conId is required."
            )
        if claim_database_currency:
            self.storage.claim_database_contract_currency(
                currency,
                allow_rebind_if_no_cycles=True,
            )
        settings.exchange = "SMART"
        settings.currency = currency
        settings.sec_type = "STK"
        return currency

    @staticmethod
    def _qualified_contract_text(
        contract: QualifiedContract,
        field: str,
        raw_field: str,
    ) -> str:
        value = getattr(contract, field, "")
        if value in (None, ""):
            value = getattr(getattr(contract, "raw", None), raw_field, "")
        return str(value or "")

    def _verify_qualified_contract(
        self,
        contract: QualifiedContract,
        settings: StrategySettings,
    ) -> None:
        """Fail closed if IBKR resolves a contract other than the selected one."""
        expected_ticker = settings.normalized_ticker()
        actual_ticker = str(getattr(contract, "ticker", "") or "").upper().strip()
        if actual_ticker and actual_ticker != expected_ticker:
            raise ValueError(
                f"IBKR resolved ticker {actual_ticker}, not the selected ticker {expected_ticker}."
            )

        try:
            expected_con_id = int(settings.contract_con_id or 0)
        except Exception:
            expected_con_id = 0
        try:
            actual_con_id = int(getattr(contract, "con_id", 0) or 0)
        except Exception:
            actual_con_id = 0
        if expected_con_id > 0 and actual_con_id != expected_con_id:
            raise ValueError(
                f"IBKR resolved conId {actual_con_id or '-'}, not the selected conId {expected_con_id}."
            )

        expected_currency = normalize_contract_currency(settings.currency, fallback="")
        actual_currency = normalize_contract_currency(
            self._qualified_contract_text(contract, "currency", "currency"),
            fallback=expected_currency,
        )
        if actual_currency != expected_currency:
            raise ValueError(
                f"IBKR resolved contract currency {actual_currency or '-'}, not {expected_currency}."
            )

        actual_exchange = self._qualified_contract_text(
            contract,
            "exchange",
            "exchange",
        ).upper().strip()
        if actual_exchange and actual_exchange != "SMART":
            raise ValueError(
                f"IBKR resolved order exchange {actual_exchange}; BouncyBot requires SMART routing."
            )

        expected_primary_exchange = str(settings.primary_exchange or "").upper().strip()
        actual_primary_exchange = self._qualified_contract_text(
            contract,
            "primary_exchange",
            "primaryExchange",
        ).upper().strip()
        if (
            expected_primary_exchange
            and actual_primary_exchange
            and actual_primary_exchange != expected_primary_exchange
        ):
            raise ValueError(
                f"IBKR resolved primary exchange {actual_primary_exchange}, "
                f"not the selected primary exchange {expected_primary_exchange}."
            )

        actual_sec_type = self._qualified_contract_text(
            contract,
            "sec_type",
            "secType",
        ).upper().strip()
        if actual_sec_type and actual_sec_type != "STK":
            raise ValueError(
                f"IBKR resolved security type {actual_sec_type}; only ordinary STK contracts are supported."
            )

    def _contract_for_strategy(self, settings: StrategySettings) -> QualifiedContract:
        ticker = settings.normalized_ticker()
        self._validate_exact_contract_selection(settings, claim_database_currency=True)
        contract = self._adapter_qualify_stock(
            ticker,
            settings.exchange,
            settings.currency,
            settings.primary_exchange,
            settings.contract_con_id,
        )
        self._verify_qualified_contract(contract, settings)
        return contract

    def _search_contracts(self, connection: ConnectionSettings, query: str) -> None:
        self.connection = connection
        self.storage.save_connection_settings(connection)
        if not self.connected:
            if not self._connect(connection):
                return
        if not self._require_broker_operation_connectivity(
            "Contract search",
            require_reconciliation_complete=True,
        ):
            return
        pattern = query.strip().upper()
        if not pattern:
            raise ValueError("Ticker/search text is required.")
        results = self.adapter.search_stock_contracts(pattern)
        currency_info = self.storage.database_contract_currency_info()
        database_currency = normalize_contract_currency(currency_info.get("currency"), fallback="")
        currency_locked = bool(currency_info.get("locked", False))
        payload: list[dict[str, Any]] = []
        for result in results:
            row = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            row_currency = normalize_contract_currency(row.get("currency"), fallback="")
            compatible = not currency_locked or not database_currency or row_currency == database_currency
            row["database_currency"] = database_currency
            row["database_currency_locked"] = currency_locked
            row["database_currency_compatible"] = compatible
            if not compatible:
                row["supported"] = False
                row["label"] = (
                    f"{row.get('label') or row.get('symbol') or 'Contract'} | "
                    f"unsupported: this database is locked to {database_currency}"
                )
            payload.append(row)
        self.signals.contract_search_updated.emit(payload)
        self.signals.ticker_search_updated.emit(payload)
        self._log("INFO", f"Contract search for {pattern} returned {len(payload)} result(s).")
        self.emit_snapshot(force=True)

    def _confirm_ticker_price(self, settings: StrategySettings) -> None:
        ticker = settings.normalized_ticker()
        if not ticker:
            raise ValueError("Ticker is required.")
        self._validate_exact_contract_selection(settings, claim_database_currency=True)
        self.adapter.set_market_data_type(self.connection.market_data_type)
        contract = self._contract_for_strategy(settings)
        self.contract = contract
        self._update_rth_status(contract)
        snapshot = self.adapter.price_snapshot(contract, timeout=12.0)
        self._record_price_snapshot(snapshot, contract)
        fresh_price = snapshot.price if bool((self.price_snapshot or {}).get("strategy_price_usable")) else None
        if fresh_price is None:
            self._log(
                "WARN",
                f"Confirmed contract {ticker} conId={contract.con_id or '-'}, but TWS/API returned no usable price in the selected market-data mode.",
            )
        else:
            self._log(
                "INFO",
                f"Confirmed contract {ticker} conId={contract.con_id or '-'}; fresh price {fresh_price:.4f} from {snapshot.source}.",
            )
        self.emit_snapshot(force=True)

    def _start_strategy(self, settings: StrategySettings) -> None:
        errors = settings.validate()
        if errors:
            raise ValueError(" ".join(errors))
        ticker = settings.normalized_ticker()
        active = self.storage.get_latest_active_cycle(ticker)
        if active and active.stage not in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED}:
            # Re-clicking Start after a restart or reconnect should resume the
            # stored active cycle, not fail with a duplicate-cycle error.
            self.active_cycle = active
            try:
                self.storage.claim_database_contract_currency(active.currency)
            except Exception as exc:
                self._recovery_required = True
                self.status = (
                    "Stored active cycle conflicts with the portable database currency lock; "
                    f"automatic recovery is blocked: {exc}"
                )
                self._log("ERROR", self.status, active)
                self.emit_snapshot(force=True)
                return
            if self._active_cycle_is_stale(active) and bool(getattr(self, "_stale_active_cycle_detected", False)):
                self._startup_resume_required = False
                self._stale_active_cycle_detected = True
                self._recovery_required = True
                self.status = (
                    f"Stale active cycle found for {active.ticker}; refresh broker state on the Reconciliation screen "
                    "and explicitly resume recovery before monitoring continues."
                )
                self._log("WARN", self.status, active)
                try:
                    if self.connected:
                        self._refresh_broker_state_for_recovery()
                except Exception as exc:
                    self._log("WARN", f"Stale-cycle broker refresh failed: {exc}", active)
                self._recovery_required = True
                self.status = (
                    f"Stale active cycle found for {active.ticker}; refresh broker state on the Reconciliation screen "
                    "and explicitly resume recovery before monitoring continues."
                )
                self.emit_snapshot(force=True)
                return
            if self._retired_account_position_block_message(active.error_message):
                active.error_message = None
                active.touch()
                self.storage.upsert_cycle(active)
            self._startup_resume_required = False
            exact_required = bool(getattr(self.adapter, "requires_exact_contract_selection", False))
            if exact_required and not self._optional_int(active.con_id):
                active.stage = Stage.MANUAL_REVIEW
                active.recovery_required = True
                active.error_message = (
                    "Stored active cycle has no exact IBKR conId. Automatic recovery is blocked; "
                    "verify the position and orders in IBKR before taking manual action."
                )
                active.touch()
                self.active_cycle = active
                self._recovery_required = True
                self.storage.upsert_cycle(active)
                self.status = active.error_message
                self._log("ERROR", self.status, active)
                self.emit_snapshot(force=True)
                return
            try:
                self.contract = self._adapter_qualify_stock(
                    active.ticker,
                    active.exchange,
                    active.currency,
                    active.primary_exchange,
                    active.con_id,
                )
                resume_identity = StrategySettings(
                    ticker=active.ticker,
                    investment_amount=max(0.01, float(active.investment_amount or 0.01)),
                    exchange=active.exchange,
                    primary_exchange=active.primary_exchange,
                    contract_con_id=active.con_id,
                    currency=active.currency,
                    sec_type="STK",
                )
                self._verify_qualified_contract(self.contract, resume_identity)
            except Exception as exc:
                if exact_required:
                    active.stage = Stage.MANUAL_REVIEW
                    active.recovery_required = True
                    active.error_message = (
                        "Active-cycle contract qualification failed; automatic recovery is blocked: "
                        f"{exc}"
                    )
                    active.touch()
                    self.active_cycle = active
                    self._recovery_required = True
                    self.storage.upsert_cycle(active)
                    self.status = active.error_message
                    self._log("ERROR", self.status, active)
                    self.emit_snapshot(force=True)
                    return
                self._log("WARN", f"Active cycle resumed, but contract qualification failed: {exc}", active)
            self._recover_after_connect()
            self._apply_active_strategy_edits(settings)
            current = self.active_cycle or active
            self._log("INFO", f"Resumed existing active cycle {current.cycle_number} for {current.ticker} in stage {current.stage.value}.", current)
            self.emit_snapshot(force=True)
            return

        self._validate_exact_contract_selection(settings, claim_database_currency=True)
        self.adapter.set_market_data_type(self.connection.market_data_type)
        contract = self._contract_for_strategy(settings)
        self._update_rth_status(contract)
        price_snapshot = self.adapter.price_snapshot(contract, timeout=10.0)
        self._record_price_snapshot(price_snapshot, contract)
        # _record_price_snapshot may have replaced self.strategy with freshly
        # calculated ATR-adaptive percentages.  Build the new cycle from that
        # effective settings object rather than the stale command payload.
        settings = self.strategy
        last_price = price_snapshot.price if bool((self.price_snapshot or {}).get("strategy_price_usable")) else None
        realized = self.storage.get_realized_net_profit_for_ticker(ticker, con_id=contract.con_id)
        cycle_number = self.storage.get_next_cycle_number(ticker)
        if last_price is None or last_price <= 0:
            # Do not fail Start just because TWS has not delivered a price yet.
            # Some symbols/data modes take longer to populate than AAPL-like
            # highly liquid symbols. The cycle will start with no anchor and the
            # first usable price tick will set the anchor/drop trigger.
            cycle = StrategyEngine.start_cycle_waiting_for_price(settings, cycle_number, self.connection.account, realized)
            log_level = "WARN"
            message = (
                f"Started cycle {cycle.cycle_number} for {cycle.ticker}, but no usable market price has arrived yet. "
                "Waiting for TWS to supply last/mark/close/bid-ask data."
            )
        else:
            cycle = StrategyEngine.start_cycle(settings, cycle_number, self.connection.account, last_price, realized)
            log_level = "INFO"
            message = f"Started cycle {cycle.cycle_number} for {cycle.ticker}; anchor set to {last_price:.4f} from {price_snapshot.source}."
            atr_blocker = self._atr_warmup_guard_blocker_for_buy(cycle)
            if atr_blocker is not None:
                cycle = StrategyEngine.pause_initial_drop_until_ready(cycle, last_price, atr_blocker["message"])
                log_level = "WARN"
                message += (
                    " ATR warmup guard is active; initial-drop evaluation is paused and no drop trigger "
                    "is armed until enough RTH data is available."
                )
        cycle.con_id = contract.con_id
        self.contract = contract
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        self._log(log_level, message, cycle)
        self.emit_snapshot(force=True)


    def _apply_active_strategy_edits(
        self, settings: StrategySettings, *, reevaluate_market_state: bool = True
    ) -> None:
        cycle = self.active_cycle
        if cycle is None or cycle.stage in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED, Stage.ERROR, Stage.MANUAL_REVIEW}:
            return
        if settings.normalized_ticker() != cycle.ticker:
            return
        realized = self.storage.get_realized_net_profit_for_ticker(cycle.ticker, con_id=cycle.con_id)
        updated, changed_fields = StrategyEngine.apply_editable_settings(cycle, settings, realized)
        changed = bool(changed_fields) or updated.to_dict() != cycle.to_dict()
        self.active_cycle = updated
        self.storage.upsert_cycle(updated)
        if changed:
            details = ", ".join(changed_fields) if changed_fields else "safe active-cycle fields"
            self._log("INFO", f"Applied editable strategy changes to active cycle {updated.cycle_number} in stage {updated.stage.value}: {details}.", updated)
        if not reevaluate_market_state:
            return
        latest_age = (
            max(0.0, time.monotonic() - self._api_last_data_monotonic)
            if self._api_last_data_monotonic > 0
            else None
        )
        max_age = max(0.1, float(getattr(updated, "max_selected_price_age_seconds", 3.0) or 3.0))
        legacy_untracked_edit = bool(
            self.connected
            and not self._broker_connectivity_initialized
            and self._api_last_data_monotonic <= 0
            and self.price_snapshot is None
        )
        recent_price_event = bool(
            legacy_untracked_edit
            or (
                not self._api_data_invalidated
                and self._upstream_trading_ready()
                and latest_age is not None
                and latest_age <= max_age
            )
        )
        if (
            updated.stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}
            and updated.last_price
            and updated.last_price > 0
            and recent_price_event
        ):
            # A settings edit can make the recently observed price satisfy an entry or
            # exit condition. Re-evaluate the cycle immediately, but use the same
            # RTH guard path as the normal market-data tick. This prevents an
            # off-hours edit from bypassing the RTH-only submission guard.
            try:
                if self.contract is None:
                    self.contract = self._adapter_qualify_stock(
                        updated.ticker,
                        updated.exchange,
                        updated.currency,
                        updated.primary_exchange,
                        updated.con_id,
                    )
                rth_status = self._update_rth_status(self.contract)
            except Exception as exc:
                rth_status = {"is_open": False, "message": f"RTH status unavailable after strategy edit: {exc}"}
            advanced, actions = self._advance_waiting_cycle_from_price(
                updated,
                float(updated.last_price),
                is_rth=bool(rth_status.get("is_open", False)),
                rth_message=str(rth_status.get("message") or rth_status.get("source") or "RTH status unavailable"),
            )
            if advanced.to_dict() != updated.to_dict() or actions:
                self.active_cycle = advanced
                self.storage.upsert_cycle(advanced)
                self._execute_actions(actions, advanced)

    def _adapter_recent_executions(self) -> list[dict[str, Any]]:
        method = getattr(self.adapter, "recent_executions", None)
        if not callable(method):
            return []
        try:
            rows = method()
        except Exception as exc:
            self._log("WARN", f"Could not request recent executions for recovery: {exc}")
            return []
        result: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, dict):
                result.append(row)
        return result

    def _order_identity_for_side(self, cycle: CycleState, side: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
        side = side.upper().strip()
        if side == "BUY":
            return cycle.buy_order_ref, cycle.buy_order_id, cycle.buy_perm_id
        if side == "PROTECTIVE_SELL":
            return cycle.protective_sell_order_ref, cycle.protective_sell_order_id, cycle.protective_sell_perm_id
        return cycle.sell_order_ref, cycle.sell_order_id, cycle.sell_perm_id

    def _execution_matches_order(self, cycle: CycleState, execution: dict[str, Any], side: str) -> bool:
        side = side.upper().strip()
        side_text = str(execution.get("side") or execution.get("action") or "").upper()
        if side == "BUY" and side_text not in {"BOT", "BUY", "B"}:
            return False
        if side in {"SELL", "PROTECTIVE_SELL"} and side_text not in {"SLD", "SELL", "S", "PROTECTIVE_SELL"}:
            return False
        expected_ref, expected_order_id, expected_perm_id = self._order_identity_for_side(cycle, side)
        ref = str(execution.get("order_ref") or execution.get("orderRef") or "")
        if ref:
            return bool(expected_ref and ref == expected_ref)
        try:
            perm_id = int(execution.get("perm_id") or execution.get("permId") or 0)
        except Exception:
            perm_id = 0
        if expected_perm_id and perm_id == int(expected_perm_id):
            return True
        try:
            order_id = int(execution.get("order_id") or execution.get("orderId") or 0)
        except Exception:
            order_id = 0
        return bool(expected_order_id and order_id == int(expected_order_id))

    def _aggregate_recovered_executions(self, cycle: CycleState, side: str) -> tuple[int, float, float, list[dict[str, Any]]]:
        matches = [row for row in self._adapter_recent_executions() if self._execution_matches_order(cycle, row, side)]
        total_shares = 0.0
        total_value = 0.0
        total_commission = 0.0
        for row in matches:
            try:
                shares = abs(float(row.get("shares") or row.get("qty") or 0.0))
                price = float(row.get("price") or row.get("avg_price") or row.get("avgPrice") or 0.0)
            except Exception:
                continue
            if shares <= 0 or price <= 0:
                continue
            total_shares += shares
            total_value += shares * price
            try:
                commission_value = float(row.get("commission") or 0.0)
            except Exception:
                commission_value = 0.0
            if commission_value:
                accepted_commission = self._commission_in_cycle_currency(
                    cycle,
                    commission_value,
                    row.get("currency"),
                    execution_id=str(row.get("execution_id") or row.get("execId") or "RECOVERED"),
                    source="RECOVERED_EXECUTION_AGGREGATE",
                )
                if accepted_commission is not None:
                    total_commission += accepted_commission
        if total_shares <= 0:
            return 0, 0.0, 0.0, matches
        return int(total_shares), total_value / total_shares, total_commission, matches

    def _record_recovered_executions(self, cycle: CycleState, rows: list[dict[str, Any]], side: str) -> None:
        for row in rows:
            execution_id = str(row.get("execution_id") or row.get("execId") or "")
            shares = float(row.get("shares") or 0.0)
            price = float(row.get("price") or row.get("avg_price") or row.get("avgPrice") or 0.0)
            if shares <= 0 or price <= 0:
                continue
            commission_value = row.get("commission")
            try:
                commission = (
                    float(commission_value)
                    if commission_value not in (None, "")
                    else None
                )
            except Exception:
                commission = None
            if commission is not None and abs(commission) > 0.0:
                commission = self._commission_in_cycle_currency(
                    cycle,
                    commission,
                    row.get("currency"),
                    execution_id=execution_id or "RECOVERED",
                    source="RECOVERED_EXECUTION_ROW",
                )
            self.storage.upsert_execution(
                cycle=cycle,
                ticker=cycle.ticker,
                side=side,
                shares=shares,
                price=price,
                avg_price=float(row.get("avg_price") or row.get("avgPrice") or price),
                commission=commission,
                currency=cycle.currency,
                order_ref=str(row.get("order_ref") or self._order_identity_for_side(cycle, side)[0] or ""),
                order_id=int(row.get("order_id") or row.get("orderId") or 0) or None,
                perm_id=int(row.get("perm_id") or row.get("permId") or 0) or None,
                execution_id=execution_id or None,
                executed_at=str(row.get("executed_at") or row.get("time") or utc_now_iso()),
                raw=row,
            )

    def _recover_buy_from_executions(self, cycle: CycleState) -> Optional[CycleState]:
        qty, avg_price, commission, rows = self._aggregate_recovered_executions(cycle, "BUY")
        if qty <= 0 or avg_price <= 0:
            return None
        self._record_recovered_executions(cycle, rows, "BUY")
        recovered, actions = StrategyEngine.on_buy_fill(cycle, qty, avg_price, "Filled", commission)
        self.active_cycle = recovered
        self.storage.upsert_cycle(recovered)
        # With no matching open order, do not try to cancel an already-gone remainder.
        safe_actions = [a for a in actions if a.action_type != "CANCEL_ORDER"]
        self._execute_actions(safe_actions, recovered)
        self._log("INFO", f"Recovered BUY fill from recent executions: {qty} @ {avg_price:.4f}.", recovered)
        return recovered

    def _recover_sell_from_executions(self, cycle: CycleState) -> Optional[CycleState]:
        qty, avg_price, commission, rows = self._aggregate_recovered_executions(cycle, "SELL")
        if qty <= 0 or avg_price <= 0:
            return None
        self._record_recovered_executions(cycle, rows, "SELL")
        close_workflow = bool(getattr(cycle, "close_before_rth_liquidation_requested", False)) or self._is_close_before_rth_market_order_ref(
            cycle.sell_order_ref
        )
        if close_workflow:
            total_qty, total_avg, total_commission = self._close_before_rth_sell_totals(cycle)
            target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
            if total_qty > target_qty > 0:
                self._move_close_before_rth_to_error(
                    cycle,
                    f"recovered executions report {total_qty} SELL shares for an app-owned quantity of {target_qty}",
                )
                return self.active_cycle
            if target_qty <= 0 or total_qty < target_qty:
                cycle.sell_filled_qty = total_qty
                cycle.avg_sell_price = total_avg if total_avg > 0 else cycle.avg_sell_price
                cycle.sell_commission = total_commission
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
                return None
            qty, avg_price, commission = total_qty, total_avg, total_commission
        target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
        if target_qty <= 0 or qty != target_qty:
            # Recovered executions must exactly match the app-owned position.
            # A lower quantity leaves shares unsold; a higher quantity indicates
            # duplicate attribution or an over-sell. Persist the observed facts
            # and let the caller enter formal recovery-required mode.
            cycle.sell_filled_qty = qty
            if avg_price > 0:
                cycle.avg_sell_price = avg_price
            cycle.sell_commission = commission
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log(
                "WARN",
                f"Recovered {qty} SELL shares for {target_qty} app-owned shares from recent executions for {cycle.ticker}; "
                "the cycle was not completed and broker/local reconciliation is required.",
                cycle,
            )
            return None
        recovered = StrategyEngine.on_sell_fill(cycle, qty, avg_price, "Filled", commission)
        self.active_cycle = recovered
        self.storage.upsert_cycle(recovered)
        self._log("INFO", f"Recovered SELL fill from recent executions: {qty} @ {avg_price:.4f}.", recovered)
        return recovered

    def _recover_protective_sell_from_executions(self, cycle: CycleState) -> Optional[CycleState]:
        qty, avg_price, commission, rows = self._aggregate_recovered_executions(cycle, "PROTECTIVE_SELL")
        if qty <= 0 or avg_price <= 0:
            return None
        self._record_recovered_executions(cycle, rows, "PROTECTIVE_SELL")
        target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
        if target_qty <= 0 or qty != target_qty:
            # See _recover_sell_from_executions: recovery requires an exact
            # quantity match, not merely a positive or at-least-target total.
            cycle.protective_sell_filled_qty = qty
            if avg_price > 0:
                cycle.protective_avg_sell_price = avg_price
            cycle.protective_sell_commission = commission
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log(
                "WARN",
                f"Recovered {qty} protective SELL shares for {target_qty} app-owned shares from recent executions for {cycle.ticker}; "
                "the cycle was not completed and broker/local reconciliation is required.",
                cycle,
            )
            return None
        recovered = StrategyEngine.on_protective_sell_fill(cycle, qty, avg_price, "Filled", commission)
        self.active_cycle = recovered
        self.storage.upsert_cycle(recovered)
        self._log("INFO", f"Recovered protective SELL fill from recent executions: {qty} @ {avg_price:.4f}.", recovered)
        return recovered

    def _check_position_for_waiting_cycle(self, cycle: CycleState) -> None:
        method = getattr(self.adapter, "position_size", None)
        if not callable(method) or self.contract is None:
            return
        try:
            position = method(self.contract, account=cycle.account)
        except Exception:
            return
        if position is None:
            return
        if cycle.stage == Stage.WAIT_RISE_TRIGGER and cycle.buy_filled_qty > 0 and float(position) < min(1.0, float(cycle.buy_filled_qty)):
            cycle.stage = Stage.MANUAL_REVIEW
            cycle.error_message = "Recovery check: expected an app-owned position, but TWS position is lower than the stored filled quantity."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log("WARN", cycle.error_message, cycle)

    def _apply_stop_action(self, action: StopAction) -> None:
        cycle = self.active_cycle
        if action == StopAction.LEAVE_ORDERS_WORKING:
            self._log("INFO", "Stop selected: leave bot orders working in TWS and recover later.", cycle)
            self._disconnect()
            return
        if cycle is None:
            self._log("INFO", "Stop selected with no active cycle.")
            return
        if action == StopAction.STOP_NOW_NO_BROKER_ACTION:
            cycle.stage = Stage.STOPPED
            cycle.stop_after_current_cycle = True
            cycle.close_position_market_requested = False
            cycle.close_before_rth_liquidation_requested = False
            cycle.close_before_rth_cancel_requested = False
            cycle.error_message = "Stop selected: strategy stopped locally; no broker order was cancelled or submitted."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log("INFO", cycle.error_message, cycle)
            return
        if action == StopAction.STOP_AFTER_CURRENT_CYCLE:
            self.active_cycle = StrategyEngine.set_stop_after_current_cycle(cycle)
            self.storage.upsert_cycle(self.active_cycle)
            self._log("INFO", "Stop selected: finish current cycle, then do not auto-repeat.", self.active_cycle)
            return
        if action == StopAction.SELL_APP_POSITION_MARKET:
            self._request_market_close_for_app_position(cycle)
            return
        if action == StopAction.CANCEL_OPEN_BOT_ORDERS:
            connectivity_message = self._broker_operation_connectivity_message("Cancel app orders")
            if connectivity_message:
                cycle.error_message = connectivity_message
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
                self.status = connectivity_message
                self._log("WARN", connectivity_message, cycle)
                return
            for ref, order_id in self._open_app_order_refs_for_cycle(cycle):
                try:
                    if self._broker_mutation_blocked_by_storage_fault(
                        "Stop-action order cancellation",
                        cycle,
                    ):
                        break
                    self.adapter.cancel_order(ref, order_id)
                    self.storage.update_order_status(ref, "CancelRequested", order_id=order_id)
                    self._log("INFO", f"Cancel requested for {ref}.", cycle)
                except Exception as exc:
                    self._log("ERROR", f"Cancel failed for {ref}: {exc}", cycle)
            if self._storage_fault_active:
                return
            cycle.stage = Stage.STOPPED
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            return

    @staticmethod
    def _app_unsold_quantity(cycle: CycleState) -> int:
        """Return the remaining app-owned long quantity for the active cycle."""
        try:
            bought = int(getattr(cycle, "buy_filled_qty", 0) or 0)
        except Exception:
            bought = 0
        try:
            final_sold = int(getattr(cycle, "sell_filled_qty", 0) or 0)
        except Exception:
            final_sold = 0
        try:
            protective_sold = int(getattr(cycle, "protective_sell_filled_qty", 0) or 0)
        except Exception:
            protective_sold = 0
        sold = max(final_sold, protective_sold)
        return max(0, bought - sold)

    @staticmethod
    def _is_order_working_for_close(ref: Optional[str], status: Optional[str], _filled_qty: int = 0) -> bool:
        if not ref:
            return False
        # A partially filled order can still have a live remainder. Treat the
        # broker status, not the presence of any fill, as authoritative for
        # whether a cancel-confirm-close workflow must wait. Otherwise a manual
        # market close could submit a second SELL while the original remainder
        # is still working.
        terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        return str(status or "").strip() not in terminal

    def _open_app_order_refs_for_cycle(self, cycle: CycleState) -> list[tuple[str, Optional[int]]]:
        """Return app-owned order refs that may still be working for this cycle."""
        refs: list[tuple[str, Optional[int]]] = []
        if self._is_order_working_for_close(cycle.buy_order_ref, cycle.buy_status, 0):
            refs.append((cycle.buy_order_ref, cycle.buy_order_id))
        if self._is_order_working_for_close(cycle.protective_sell_order_ref, cycle.protective_sell_status, cycle.protective_sell_filled_qty):
            refs.append((cycle.protective_sell_order_ref, cycle.protective_sell_order_id))
        if self._is_order_working_for_close(cycle.sell_order_ref, cycle.sell_status, cycle.sell_filled_qty):
            refs.append((cycle.sell_order_ref, cycle.sell_order_id))
        return refs

    def _working_sell_order_exists(self, cycle: CycleState) -> bool:
        return (
            self._is_order_working_for_close(cycle.protective_sell_order_ref, cycle.protective_sell_status, cycle.protective_sell_filled_qty)
            or self._is_order_working_for_close(cycle.sell_order_ref, cycle.sell_status, cycle.sell_filled_qty)
        )

    def _request_market_close_for_app_position(self, cycle: CycleState) -> None:
        """Cancel app orders and sell the app-bought unsold quantity with a market order.

        If a protective/final SELL is already working, the app first requests
        cancellation and waits for TWS to confirm it is no longer working before
        sending the market SELL. That avoids two app-created SELL orders working
        for the same shares at the same time.
        """
        if bool(getattr(cycle, "close_before_rth_liquidation_requested", False)):
            self._set_close_before_rth_wait_message(
                cycle,
                "Close-before-RTH liquidation is already in progress. No second market SELL was submitted; "
                "the existing cancel-confirm-liquidate workflow remains responsible for the app-owned position.",
            )
            return

        connectivity_message = self._broker_operation_connectivity_message(
            "Close app position by market order",
            require_reconciliation_complete=True,
        )
        if connectivity_message:
            cycle.error_message = connectivity_message
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.status = connectivity_message
            self._log("WARN", connectivity_message, cycle)
            return
        unsold = self._app_unsold_quantity(cycle)
        cycle.close_position_market_requested = True
        cycle.close_before_rth_liquidation_requested = False
        cycle.close_before_rth_cancel_requested = False
        cycle.stop_after_current_cycle = True
        if cycle.protective_sell_order_ref and self._is_order_working_for_close(cycle.protective_sell_order_ref, cycle.protective_sell_status, cycle.protective_sell_filled_qty):
            cycle.protective_sell_cancel_requested = True
        self.storage.upsert_cycle(cycle)
        self.active_cycle = cycle

        for ref, order_id in self._open_app_order_refs_for_cycle(cycle):
            try:
                if self._broker_mutation_blocked_by_storage_fault(
                    "Close-by-market order cancellation",
                    cycle,
                ):
                    break
                self.adapter.cancel_order(ref, order_id)
                self.storage.update_order_status(ref, "CancelRequested", order_id=order_id)
                if ref == cycle.protective_sell_order_ref:
                    cycle.protective_sell_status = "CancelRequested"
                    cycle.protective_sell_cancel_requested = True
                elif ref == cycle.sell_order_ref:
                    cycle.sell_status = "CancelRequested"
                elif ref == cycle.buy_order_ref:
                    cycle.buy_status = "CancelRequested"
                self._log("INFO", f"Close-by-market selected: cancel requested for {ref} before closing app-owned position.", cycle)
            except Exception as exc:
                self._log("ERROR", f"Close-by-market cancel failed for {ref}: {exc}", cycle)

        if self._storage_fault_active:
            return

        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)

        if unsold <= 0:
            cycle.close_position_market_requested = False
            cycle.close_before_rth_liquidation_requested = False
            cycle.close_before_rth_cancel_requested = False
            cycle.stage = Stage.STOPPED
            cycle.error_message = "Close-by-market selected, but this app has no unsold bought quantity for the active cycle."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log("INFO", cycle.error_message, cycle)
            return

        if self._working_sell_order_exists(cycle):
            cycle.error_message = "Close-by-market requested. Waiting for existing app SELL order cancellation before submitting market SELL."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log("WARN", cycle.error_message, cycle)
            return

        self._submit_requested_market_close(cycle)

    def _submit_requested_market_close(self, cycle: CycleState) -> bool:
        """Submit the requested market-close SELL when no app SELL is working."""
        if not bool(getattr(cycle, "close_position_market_requested", False)):
            return False
        if self._working_sell_order_exists(cycle):
            return False
        connectivity_message = self._order_submission_connectivity_message("SELL")
        if connectivity_message:
            cycle.error_message = connectivity_message
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.status = connectivity_message
            self._log("WARN", connectivity_message, cycle)
            return False
        remaining = self._app_unsold_quantity(cycle)
        if remaining <= 0:
            cycle.close_position_market_requested = False
            cycle.close_before_rth_liquidation_requested = False
            cycle.close_before_rth_cancel_requested = False
            cycle.stage = Stage.STOPPED
            cycle.error_message = "Close-by-market request completed without sending a SELL because no app-owned quantity remains."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log("INFO", cycle.error_message, cycle)
            return True
        cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "FORCED_SELL_MARKET")
        cycle.sell_order_id = None
        cycle.sell_perm_id = None
        cycle.sell_status = None
        cycle.sell_initial_trail_stop_price = None
        cycle.stage = Stage.SELL_TRAIL_ACTIVE
        cycle.error_message = "Submitting market SELL to close the app-bought unsold position."
        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        action = StrategyAction(
            "PLACE_SELL_MARKET",
            {
                "ticker": cycle.ticker,
                "quantity": int(remaining),
                "order_type": "MKT",
                "trailing_percent": 0.0,
                "initial_stop_price": None,
                "reference_price": float((self.price_snapshot or {}).get("price") or cycle.last_price or 0.0),
                "order_ref": cycle.sell_order_ref,
                "manual_close": True,
            },
        )
        self._place_market_order(cycle, action, "SELL")
        self._log("WARN", f"Close-by-market submitted SELL MKT for {remaining} app-bought unsold shares.", self.active_cycle or cycle)
        return True

    def _handle_broker_connection_problem(self, exc: Exception | str) -> None:
        message = str(exc)
        self.connected = False
        if self._last_recovery_probe:
            probe = dict(self._last_recovery_probe)
            probe["invalidated_at"] = utc_now_iso()
            probe["invalidation_reason"] = "The broker API connection failed after this probe."
            self._last_recovery_probe = probe
        self.status = (
            f"{self._platform_name()} connection problem: {message}. "
            f"Auto-reconnect will retry every {self.RECONNECT_INTERVAL_SECONDS:g} seconds until connected."
        )
        self.signals.connection_changed.emit(False, self.status)
        self._update_audit_condition(
            key="broker_reconnect",
            family="IBKR connection outage",
            reason_code="LOCAL_API_DISCONNECTED",
            message=self.status,
            cycle=self.active_cycle,
            initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
            repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
            summary_level="WARN",
            first_summary_level="INFO",
            immediate_level="WARN",
            immediate_token="outage_started",
            context={"attempt_count": self._reconnect_failures, "error": message},
            kind="broker_reconnect",
        )

    def _attempt_reconnect_if_due(self) -> bool:
        if not self._auto_reconnect_enabled:
            return False
        now = time.monotonic()
        if (
            self._reconnect_failures > 0
            and now - self._last_reconnect_attempt_monotonic < self.RECONNECT_INTERVAL_SECONDS
        ):
            return False
        self._last_reconnect_attempt_monotonic = now
        try:
            self.status = f"Reconnecting to {self._platform_name()}... attempt {self._reconnect_failures + 1}"
            self.signals.connection_changed.emit(False, self.status)
            self.adapter.connect(self.connection.host, self.connection.port, self.connection.client_id, self.connection.market_data_type)
            failed_attempts = int(self._reconnect_failures)
            self.connected = True
            self._reconnect_failures = 0
            self._last_reconnect_attempt_monotonic = 0.0
            self._reset_price_feed_after_reconnect()
            process_events = getattr(self.adapter, "process_events", None)
            if callable(process_events):
                try:
                    process_events(0.0)
                except Exception:
                    pass
            self._drain_broker_events()
            connectivity = self._refresh_broker_connectivity_snapshot(detect_transition=False)
            if connectivity.get("upstream_connected") is not True:
                code = connectivity.get("error_code")
                code_text = f" (IBKR {code})" if code not in (None, "") else ""
                self.status = (
                    f"Reconnected locally to {self._platform_name()} {self.connection.host}:{self.connection.port}, "
                    f"but the IBKR server link is unavailable{code_text}. Trading and broker recovery are paused."
                )
                self._update_audit_condition(
                    key="broker_reconnect",
                    family="IBKR connection outage",
                    reason_code="UPSTREAM_UNAVAILABLE_AFTER_RECONNECT",
                    message=self.status,
                    cycle=self.active_cycle,
                    initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                    repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                    summary_level="WARN",
                    first_summary_level="INFO",
                    immediate_level="WARN",
                    immediate_token="outage_started",
                    context={
                        "attempt_count": failed_attempts,
                        "connectivity": connectivity,
                    },
                    kind="broker_reconnect",
                )
                self.signals.connection_changed.emit(False, self.status)
                self.emit_snapshot(force=True)
                return True
            self.status = f"Reconnected to {self._platform_name()} {self.connection.host}:{self.connection.port} ({self.connection.trading_mode})"
            cleared = self._clear_audit_condition(
                "broker_reconnect",
                cycle=self.active_cycle,
                recovery_message=self.status,
                context={
                    "host": self.connection.host,
                    "port": self.connection.port,
                    "failed_attempts": failed_attempts,
                },
            )
            if cleared is None or int(cleared.get("audit_emissions", 0) or 0) <= 0:
                self.storage.add_event("INFO", self.status)
            self.signals.connection_changed.emit(True, self.status)
            if self._upstream_recovery_pending:
                if not self._recover_upstream_session_if_needed():
                    self.emit_snapshot(force=True)
                    return False
            elif self._startup_resume_required and self.active_cycle is not None:
                self.status = (
                    f"Reconnected. Stored active cycle found for {self.active_cycle.ticker}; "
                    "click 4. Start strategy to resume monitoring/recovery."
                )
                self._log("WARN", self.status, self.active_cycle)
            else:
                self._recover_after_connect()
            self._refresh_confirmed_market_data_if_due(force=True)
            self.emit_snapshot(force=True)
            return True
        except Exception as exc:
            self.connected = False
            self._reconnect_failures += 1
            helper = connection_helper_text(self.connection.normalized_platform(), self.connection.host, self.connection.port, exc)
            self.status = (
                f"Reconnect failed: {helper} BouncyBot will retry again in "
                f"{self.RECONNECT_INTERVAL_SECONDS:g} seconds."
            )
            self.signals.connection_changed.emit(False, self.status)
            self._update_audit_condition(
                key="broker_reconnect",
                family="IBKR connection outage",
                reason_code="RECONNECT_ATTEMPT_FAILED",
                message=self.status,
                cycle=self.active_cycle,
                initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                summary_level="WARN",
                first_summary_level="INFO",
                context={"attempt_count": self._reconnect_failures, "error": str(exc)},
                kind="broker_reconnect",
            )
            return False

    def _ensure_connection_alive(self) -> bool:
        if self.connected and self.adapter.is_connected():
            return True
        if self.connected and not self.adapter.is_connected():
            self._handle_broker_connection_problem("TWS API connection is no longer connected")
        if not self.connected and self._auto_reconnect_enabled:
            return self._attempt_reconnect_if_due()
        return False

    def _rth_status_for_contract(self, contract: Optional[QualifiedContract]) -> dict[str, Any]:
        if contract is None:
            status = RthStatus(False, "no_contract", "No qualified contract; trading is blocked.", utc_now_iso())
            return status.to_dict()
        method = getattr(self.adapter, "regular_trading_hours_status", None)
        if not callable(method):
            status = RthStatus(True, "adapter_unknown", "Adapter does not expose RTH status; assuming open.", utc_now_iso())
            return status.to_dict()
        try:
            result = method(contract)
        except Exception as exc:
            status = RthStatus(False, "rth_check_error", f"Could not determine RTH status; trading is blocked: {exc}", utc_now_iso())
            return status.to_dict()
        if hasattr(result, "to_dict"):
            return result.to_dict()
        if isinstance(result, dict):
            return dict(result)
        return RthStatus(bool(result), "adapter_result", str(result), utc_now_iso()).to_dict()

    def _update_rth_status(self, contract: Optional[QualifiedContract]) -> dict[str, Any]:
        self._latest_rth_status = self._rth_status_for_contract(contract)
        return self._latest_rth_status

    def _reset_price_feed_after_reconnect(self) -> None:
        """Force the next price read to rebuild diagnostics after reconnect."""
        self.price_snapshot = None
        self._last_price_poll_monotonic = 0.0
        self._api_data_seen_count = 0
        self._api_data_change_count = 0
        self._api_last_data_monotonic = 0.0
        self._api_last_data_wall_time = ""
        self._api_last_change_monotonic = 0.0
        self._api_last_change_wall_time = ""
        self._api_last_field_signature = None
        self._last_market_data_event_token = None
        self._api_data_invalidated = True
        self._api_data_invalidated_reason = "Waiting for the first fresh market-data update after reconnect."
        self._api_data_invalidated_at = utc_now_iso()
        self._stage3_sell_confirmation.clear()

    def _refresh_confirmed_market_data_if_due(
        self,
        *,
        force: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        """Keep the confirmed-ticker market-data monitor alive outside active stages.

        Stop/cancel/manual position handling should not freeze the price monitor.
        As long as the local and upstream connections are available and a contract
        is confirmed, the GUI reads the subscription object for diagnostics. Only
        a new adapter event sequence refreshes age or becomes strategy-usable.
        After a reconnect, force=True rebuilds the data snapshot immediately.
        """
        if self.contract is None or ((not force) and self._seconds_until_next_price_poll() > 0):
            return
        read_timeout = 0.75 if timeout is None else max(0.0, float(timeout))
        try:
            self._update_rth_status(self.contract)
            self.adapter.set_market_data_type(self.connection.market_data_type)
            snapshot = self.adapter.price_snapshot(self.contract, timeout=read_timeout)
            self._record_price_snapshot(snapshot, self.contract)
        except BrokerAdapterError as exc:
            self._set_price_error_snapshot(exc)
            self._handle_broker_connection_problem(exc)
        except Exception as exc:
            self._set_price_error_snapshot(exc)

    def _handle_connectivity_broker_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "").upper()
        if event_type in {"IBKR_UPSTREAM_DISCONNECTED", "IBKR_API_PORT_RESET"}:
            status = {
                "local_connected": bool(event.get("local_connected", self.adapter.is_connected())),
                "upstream_connected": False,
                "state": str(event.get("upstream_state") or "upstream_disconnected"),
                "message": str(event.get("message") or "IB Gateway/TWS lost connectivity to IBKR servers."),
                "error_code": event.get("error_code"),
                "changed_at": str(event.get("created_at") or utc_now_iso()),
                "market_data_resubscribe_required": bool(event.get("market_data_resubscribe_required")),
                "awaiting_fresh_market_data": True,
                "market_data_event_tracking": bool(self._broker_connectivity.get("market_data_event_tracking")),
                "trading_ready": False,
            }
            self._broker_connectivity = status
            self._broker_connectivity_initialized = True
            self._handle_upstream_connectivity_lost(status)
        elif event_type in {
            "IBKR_UPSTREAM_RESTORED_DATA_LOST",
            "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED",
        }:
            status = {
                "local_connected": True,
                "upstream_connected": True,
                "state": str(event.get("upstream_state") or "restored"),
                "message": str(event.get("message") or "IBKR server connectivity was restored."),
                "error_code": event.get("error_code"),
                "changed_at": str(event.get("created_at") or utc_now_iso()),
                "market_data_resubscribe_required": bool(event.get("market_data_resubscribe_required")),
                "awaiting_fresh_market_data": True,
                "market_data_event_tracking": bool(self._broker_connectivity.get("market_data_event_tracking")),
                "trading_ready": True,
            }
            self._broker_connectivity = status
            self._broker_connectivity_initialized = True
            self._handle_upstream_connectivity_restored(status)
        elif event_type in {
            "IBKR_MARKET_DATA_COMPETING_SESSION",
            "IBKR_MARKET_DATA_FARM_DISCONNECTED",
            "IBKR_MARKET_DATA_FARM_RESTORED",
        }:
            default_messages = {
                "IBKR_MARKET_DATA_COMPETING_SESSION": (
                    "IBKR stopped this API session's live market data because another session has priority."
                ),
                "IBKR_MARKET_DATA_FARM_DISCONNECTED": (
                    "The IBKR market-data farm is disconnected for this API session."
                ),
                "IBKR_MARKET_DATA_FARM_RESTORED": (
                    "The IBKR market-data farm reports ready; waiting for a fresh streaming update."
                ),
            }
            message = str(event.get("message") or default_messages[event_type])
            event_local_connected = event.get("local_connected")
            if event_local_connected is None:
                try:
                    event_local_connected = self.adapter.is_connected()
                except Exception:
                    event_local_connected = self.connected
            local_connected = bool(event_local_connected)
            event_upstream_connected = event.get("upstream_connected")
            previous_upstream_connected = self._broker_connectivity.get("upstream_connected")
            previous_upstream_state = str(self._broker_connectivity.get("state") or "")
            previous_error_code = self._broker_connectivity.get("error_code")
            known_full_upstream_outage = bool(
                previous_upstream_connected is False
                and (
                    previous_upstream_state in {"upstream_disconnected", "api_port_reset"}
                    or previous_error_code in {1100, 2110, 1300}
                )
            )
            upstream_connected = not (
                event_upstream_connected is False
                or known_full_upstream_outage
            )
            status = {
                "local_connected": local_connected,
                # These messages concern quote delivery. The order/API channel
                # can remain usable, so preserve a confirmed upstream link while
                # independently invalidating every cached market-data value.
                # They must not, however, upgrade a stronger 1100/2110 outage.
                "upstream_connected": upstream_connected,
                "state": str(event.get("upstream_state") or event_type.lower()),
                "message": message,
                "error_code": event.get("error_code"),
                "changed_at": str(event.get("created_at") or utc_now_iso()),
                "market_data_resubscribe_required": bool(event.get("market_data_resubscribe_required")),
                "awaiting_fresh_market_data": True,
                "market_data_event_tracking": bool(self._broker_connectivity.get("market_data_event_tracking")),
                "trading_ready": bool(local_connected and upstream_connected),
            }
            self._broker_connectivity = status
            self._broker_connectivity_initialized = True
            self._invalidate_market_data_freshness(message)
            if not upstream_connected:
                self.status = (
                    f"{self._platform_name()} remains disconnected from IBKR servers. "
                    f"{message} Trading and broker-state polling remain paused."
                )
                self._log("WARN", self.status, self.active_cycle)
            elif event_type == "IBKR_MARKET_DATA_FARM_RESTORED":
                self.status = "IBKR market-data connection restored; waiting for the next actual streaming update."
                self._log("INFO", self.status, self.active_cycle)
            else:
                self.status = f"Live market data unavailable: {message} Trading decisions that require a quote are paused."
                self._log("WARN", self.status, self.active_cycle)

    def _recover_upstream_session_if_needed(self) -> bool:
        """Reconcile broker facts once after 1101/1102 before normal ticks resume."""
        if not self._upstream_recovery_pending:
            return True
        now = time.monotonic()
        if self._last_upstream_recovery_attempt_monotonic and now - self._last_upstream_recovery_attempt_monotonic < 2.0:
            return False
        self._last_upstream_recovery_attempt_monotonic = now

        # A stored cycle still requires the operator's explicit Start action.
        # Market data may be resubscribed now, but broker recovery remains gated.
        if self._startup_resume_required:
            try:
                self._refresh_confirmed_market_data_if_due(force=True)
            except Exception:
                pass
            self._upstream_recovery_pending = False
            self.status = "IBKR server connection restored. Stored cycle still requires an explicit Start action."
            return True

        self._upstream_recovery_pending = False
        try:
            self._refresh_display_accounts()
            self._recover_after_connect()
            self._refresh_confirmed_market_data_if_due(force=True)
        except BrokerAdapterError as exc:
            self._upstream_recovery_pending = True
            if not self.adapter.is_connected():
                self._handle_broker_connection_problem(exc)
            else:
                self.status = f"Post-reconnect broker reconciliation failed; trading remains paused: {exc}"
                self._log("WARN", self.status, self.active_cycle)
            return False
        except Exception as exc:
            self._upstream_recovery_pending = True
            self.status = f"Post-reconnect broker reconciliation failed; trading remains paused: {exc}"
            self._log("WARN", self.status, self.active_cycle)
            return False

        if self._recovery_required:
            self.status = "IBKR server connection restored, but broker/local state requires reconciliation before trading can resume."
        elif self._api_data_invalidated:
            self.status = "IBKR server connection restored and broker state reconciled; waiting for a fresh market-data update."
        else:
            self.status = "IBKR server connection restored; broker state and fresh market data are confirmed."
        self._log("INFO", self.status, self.active_cycle)
        return True

    def _drain_broker_events(self) -> None:
        method = getattr(self.adapter, "drain_broker_events", None)
        if not callable(method):
            return
        try:
            events = method() or []
        except Exception as exc:
            self._log("WARN", f"Could not drain broker callback events: {exc}")
            return
        for event in events:
            if self._storage_fault_active:
                return
            if not isinstance(event, dict):
                continue
            self._handle_connectivity_broker_event(event)
            order_ref = str(event.get("order_ref") or event.get("orderRef") or "") or None
            event_type = str(event.get("event_type") or event.get("type") or "BROKER_EVENT")
            cycle = self._cycle_for_order_ref(order_ref)
            try:
                self.storage.add_broker_event(
                    event_type=event_type,
                    raw=event,
                    ticker=(cycle.ticker if cycle else event.get("ticker")),
                    cycle_id=(cycle.id if cycle else None),
                    order_ref=order_ref,
                    order_id=self._optional_int(event.get("order_id") or event.get("orderId")),
                    perm_id=self._optional_int(event.get("perm_id") or event.get("permId")),
                    execution_id=str(event.get("execution_id") or event.get("execId") or "") or None,
                    created_at=str(event.get("created_at") or event.get("timestamp") or "") or None,
                )
            except Exception as exc:
                self._log("WARN", f"Could not persist broker callback event {event_type}: {exc}", cycle)
                if self._storage_fault_active:
                    return
            if event_type in {"EXEC_DETAILS", "COMMISSION_REPORT"}:
                try:
                    self._apply_execution_callback_event(event_type, event, cycle)
                except Exception as exc:
                    self._log("ERROR", f"Could not reconcile broker execution callback {event_type}: {exc}", cycle)
                    if self._storage_fault_active:
                        return
            if event_type == "ORDER_ERROR":
                if cycle is None:
                    # A shared Master API feed can expose another portable
                    # installation's IBKRBOT-prefixed order.  Persist it only as
                    # an unowned broker event; never mutate this instance's
                    # status, decision stream, or active cycle.
                    continue
                code = self._optional_int(event.get("error_code") or event.get("errorCode"))
                message = str(event.get("message") or event.get("error_string") or "IBKR rejected the order request.")
                prefix = f"IBKR order error {code}" if code is not None else "IBKR order error"
                if order_ref:
                    prefix += f" for {order_ref}"
                text = f"{prefix}: {message}"
                self.status = text
                try:
                    self.storage.add_decision_event(
                        event_type="BROKER_ORDER_ERROR",
                        message=text,
                        cycle=cycle,
                        stage_before=(cycle.stage.value if cycle else None),
                        stage_after=(cycle.stage.value if cycle else None),
                        decision_result="broker_error",
                        broker_order_id=self._optional_int(event.get("order_id") or event.get("orderId")),
                        perm_id=self._optional_int(event.get("perm_id") or event.get("permId")),
                        raw=event,
                    )
                except Exception as exc:
                    self._log("WARN", f"Could not persist structured broker-order error: {exc}", cycle)
                self._log("ERROR", text, cycle)

    def _cycle_for_order_ref(self, order_ref: Optional[str]) -> Optional[CycleState]:
        if not order_ref:
            return None
        cycle = self.active_cycle
        if cycle and order_ref in {cycle.buy_order_ref, cycle.sell_order_ref, cycle.protective_sell_order_ref}:
            return cycle
        return self.storage.get_cycle_for_order_ref(order_ref)

    @staticmethod
    def _execution_role_for_order_ref(cycle: CycleState, order_ref: str) -> Optional[str]:
        if order_ref and order_ref == cycle.buy_order_ref:
            return "BUY"
        if order_ref and order_ref == cycle.protective_sell_order_ref:
            return "PROTECTIVE_SELL"
        if order_ref and order_ref == cycle.sell_order_ref:
            return "SELL"
        return None

    def _remember_pending_commission(self, execution_id: str, event: dict[str, Any]) -> None:
        if not execution_id:
            return
        if len(self._pending_commissions_by_execution_id) >= 512:
            oldest = next(iter(self._pending_commissions_by_execution_id), None)
            if oldest is not None:
                self._pending_commissions_by_execution_id.pop(oldest, None)
        self._pending_commissions_by_execution_id[execution_id] = dict(event)

    def _commission_in_cycle_currency(
        self,
        cycle: CycleState,
        commission: Optional[float],
        currency: Any,
        *,
        execution_id: str,
        source: str,
    ) -> Optional[float]:
        """Return a commission only when no FX conversion would be required."""
        if commission is None:
            return None
        commission_value = float(commission)
        if commission_value == 0.0:
            return 0.0
        commission_currency = normalize_contract_currency(currency, fallback="")
        cycle_currency = normalize_contract_currency(cycle.currency, fallback="")
        if not commission_currency or commission_currency == cycle_currency:
            return commission_value

        key = f"{cycle.id}|{execution_id}|{commission_currency}"
        already_recorded = key in self._commission_currency_mismatch_keys
        if not already_recorded:
            try:
                already_recorded = self.storage.has_decision_event_dedupe_key(
                    cycle_id=cycle.id,
                    event_type="COMMISSION_CURRENCY_MISMATCH",
                    dedupe_key=key,
                )
            except Exception:
                already_recorded = False
            self._commission_currency_mismatch_keys.add(key)

        if not cycle.stop_after_current_cycle:
            cycle.stop_after_current_cycle = True
            cycle.touch()
            self.storage.upsert_cycle(cycle)
        if bool(getattr(self.strategy, "auto_repeat", False)):
            self.strategy.auto_repeat = False
            try:
                self.storage.save_strategy_settings(self.strategy)
            except Exception:
                pass
        active_cycle = self.active_cycle
        if (
            active_cycle is not None
            and active_cycle.id != cycle.id
            and not active_cycle.stop_after_current_cycle
        ):
            active_cycle.stop_after_current_cycle = True
            active_cycle.touch()
            try:
                self.storage.upsert_cycle(active_cycle)
            except Exception:
                pass

        if not already_recorded:
            message = (
                f"Commission for execution {execution_id or '-'} was reported in {commission_currency}, "
                f"but this database and cycle use {cycle_currency}. The commission was not added to net P/L, "
                "and Auto-repeat was disabled because BouncyBot does not perform FX conversion."
            )
            try:
                self.storage.add_decision_event(
                    event_type="COMMISSION_CURRENCY_MISMATCH",
                    message=message,
                    cycle=cycle,
                    stage_before=cycle.stage.value,
                    stage_after=cycle.stage.value,
                    decision_result="commission_excluded_no_fx_conversion",
                    raw={
                        "dedupe_key": key,
                        "execution_id": execution_id,
                        "commission": commission_value,
                        "commission_currency": commission_currency,
                        "cycle_currency": cycle_currency,
                        "source": source,
                    },
                )
            except Exception:
                pass
            self._log("WARN", message, cycle)
        return None

    def _apply_execution_callback_event(
        self,
        event_type: str,
        event: dict[str, Any],
        cycle: Optional[CycleState],
    ) -> None:
        """Persist and apply execution/commission callbacks exactly once.

        Ownership has already been resolved by exact OrderRef.  Foreign events
        are deliberately retained only in ``broker_events`` with no cycle ID.
        """
        if cycle is None:
            return
        order_ref = str(event.get("order_ref") or event.get("orderRef") or "").strip()
        role = self._execution_role_for_order_ref(cycle, order_ref)
        if role is None:
            return
        event_ticker = str(event.get("ticker") or "").strip().upper()
        if event_ticker and event_ticker != cycle.ticker.upper():
            return
        if event_type == "EXEC_DETAILS":
            callback_side = str(event.get("side") or "").strip().upper()
            if role == "BUY" and callback_side not in {"", "BUY", "BOT"}:
                return
            if role in {"SELL", "PROTECTIVE_SELL"} and callback_side not in {"", "SELL", "SLD"}:
                return
        execution_id = str(event.get("execution_id") or event.get("execId") or "").strip()
        if not execution_id:
            return

        existing = self.storage.get_execution(execution_id)
        buy_quantity_before = int(cycle.buy_filled_qty or 0) if role == "BUY" else 0
        commission_value = event.get("commission")
        try:
            commission = float(commission_value) if commission_value not in (None, "") else None
        except Exception:
            commission = None

        shares_value = event.get("shares")
        price_value = event.get("price")
        try:
            shares = abs(float(shares_value or 0.0))
            price = float(price_value or 0.0)
        except Exception:
            shares = 0.0
            price = 0.0

        if event_type == "COMMISSION_REPORT" and (existing is None or shares <= 0 or price <= 0):
            if existing is None:
                self._remember_pending_commission(execution_id, event)
                return
            shares = abs(float(existing.get("shares") or 0.0))
            price = float(existing.get("price") or 0.0)

        pending = self._pending_commissions_by_execution_id.pop(execution_id, None)
        commission_currency = event.get("currency")
        if pending is not None:
            try:
                pending_value = pending.get("commission")
                pending_commission = float(pending_value) if pending_value not in (None, "") else None
                # A commissionReport callback is authoritative.  An earlier or
                # simultaneously materialized execDetails Fill may still expose
                # the library's default 0.0 commission placeholder.
                if pending_commission is not None:
                    commission = pending_commission
                    commission_currency = pending.get("currency") or commission_currency
            except Exception:
                pass

        if commission is not None and (
            event_type == "COMMISSION_REPORT"
            or pending is not None
            or abs(float(commission)) > 0.0
        ):
            commission = self._commission_in_cycle_currency(
                cycle,
                commission,
                commission_currency,
                execution_id=execution_id,
                source=event_type,
            )

        if existing is not None:
            if shares <= 0:
                shares = abs(float(existing.get("shares") or 0.0))
            if price <= 0:
                price = float(existing.get("price") or 0.0)
        if shares <= 0 or price <= 0:
            return

        avg_price_value = event.get("avg_price") or event.get("avgPrice") or price
        try:
            avg_price = float(avg_price_value or price)
        except Exception:
            avg_price = price
        executed_at = str(
            event.get("executed_at")
            or event.get("time")
            or (existing or {}).get("executed_at")
            or event.get("created_at")
            or utc_now_iso()
        )
        raw = dict(event)
        if pending is not None:
            raw["pending_commission_callback"] = pending
        self.storage.upsert_execution(
            cycle=cycle,
            ticker=cycle.ticker,
            side=role,
            shares=shares,
            price=price,
            avg_price=avg_price,
            commission=commission,
            currency=cycle.currency,
            order_ref=order_ref,
            order_id=self._optional_int(event.get("order_id") or event.get("orderId")),
            perm_id=self._optional_int(event.get("perm_id") or event.get("permId")),
            execution_id=execution_id,
            executed_at=executed_at,
            raw=raw,
        )
        self.storage.rebalance_cumulative_execution_placeholder(
            cycle=cycle,
            side=role,
            order_ref=order_ref,
        )
        self._reconcile_cycle_execution_ledger(cycle.id)
        if event_type == "EXEC_DETAILS" and role == "BUY" and existing is None and buy_quantity_before <= 0:
            updated_cycle = self.storage.get_cycle(cycle.id) or cycle
            if int(updated_cycle.buy_filled_qty or 0) > 0:
                self._start_trade_market_data_capture(
                    "BUY_FILL",
                    updated_cycle,
                    extra={"source": "EXEC_DETAILS_CALLBACK", "execution": dict(event)},
                )
                try:
                    self.storage.backup_database("after_buy_partial_fill")
                except Exception:
                    pass

    def _reconcile_cycle_execution_ledger(self, cycle_id: str) -> None:
        """Project the idempotent execution ledger back onto cycle totals.

        Broker order-status polling can report a cumulative fill before every
        individual execDetails callback has arrived.  Callback projection is
        therefore monotonic: a partial ledger must never reduce a quantity or
        replace a broker cumulative average with an incomplete subset.
        """
        cycle = self.storage.get_cycle(cycle_id)
        if cycle is None:
            return
        original_buy_qty = int(cycle.buy_filled_qty or 0)
        buy = self.storage.get_execution_totals(cycle.id, "BUY")
        ledger_buy_qty = int(round(float(buy.get("shares", 0.0) or 0.0)))
        buy_qty = max(original_buy_qty, ledger_buy_qty)
        if ledger_buy_qty > 0:
            if ledger_buy_qty >= original_buy_qty:
                cycle.avg_buy_price = float(buy.get("avg_price", 0.0) or cycle.avg_buy_price or 0.0)
            cycle.buy_filled_qty = buy_qty
            cycle.buy_commission = max(
                float(cycle.buy_commission or 0.0),
                float(buy.get("commission", 0.0) or 0.0),
            )
            cycle.buy_filled_at = cycle.buy_filled_at or utc_now_iso()
            cycle.rise_trigger_price = StrategyEngine.recalculate_rise_trigger_price(cycle)

        follow_actions: list[StrategyAction] = []
        if ledger_buy_qty > 0 and cycle.stage == Stage.BUY_TRAIL_ACTIVE:
            cycle, follow_actions = StrategyEngine.on_buy_fill(
                cycle,
                buy_qty,
                float(cycle.avg_buy_price or buy.get("avg_price", 0.0) or 0.0),
                str(cycle.buy_status or "Submitted"),
                float(cycle.buy_commission or 0.0),
            )

        protective = self.storage.get_execution_totals(cycle.id, "PROTECTIVE_SELL")
        ledger_protective_qty = int(round(float(protective.get("shares", 0.0) or 0.0)))
        current_protective_qty = int(cycle.protective_sell_filled_qty or 0)
        protective_qty = max(current_protective_qty, ledger_protective_qty)
        protective_avg = float(cycle.protective_avg_sell_price or 0.0)
        if ledger_protective_qty > 0:
            if ledger_protective_qty >= current_protective_qty:
                protective_avg = float(protective.get("avg_price", 0.0) or protective_avg)
            cycle.protective_sell_filled_qty = protective_qty
            cycle.protective_avg_sell_price = protective_avg
            cycle.protective_sell_commission = max(
                float(cycle.protective_sell_commission or 0.0),
                float(protective.get("commission", 0.0) or 0.0),
            )
            cycle.protective_sell_filled_at = cycle.protective_sell_filled_at or utc_now_iso()

        normal_sell = self.storage.get_execution_totals(cycle.id, "SELL")
        ledger_normal_sell_qty = int(round(float(normal_sell.get("shares", 0.0) or 0.0)))
        normal_sell_avg = float(normal_sell.get("avg_price", 0.0) or 0.0)
        normal_sell_commission = float(normal_sell.get("commission", 0.0) or 0.0)
        ledger_total_sell_qty = ledger_normal_sell_qty + ledger_protective_qty
        current_total_sell_qty = int(cycle.sell_filled_qty or 0)
        total_sell_qty = max(current_total_sell_qty, ledger_total_sell_qty)
        if ledger_total_sell_qty > 0:
            if ledger_total_sell_qty >= current_total_sell_qty:
                total_notional = ledger_normal_sell_qty * normal_sell_avg + ledger_protective_qty * float(
                    protective.get("avg_price", 0.0) or protective_avg or 0.0
                )
                cycle.avg_sell_price = total_notional / ledger_total_sell_qty
            cycle.sell_filled_qty = total_sell_qty
            cycle.sell_commission = max(
                float(cycle.sell_commission or 0.0),
                normal_sell_commission + float(protective.get("commission", 0.0) or 0.0),
            )
            cycle.sell_filled_at = cycle.sell_filled_at or cycle.protective_sell_filled_at or utc_now_iso()

        if buy_qty > original_buy_qty and cycle.stage in {
            Stage.WAIT_RISE_TRIGGER,
            Stage.SELL_TRAIL_ACTIVE,
            Stage.CYCLE_COMPLETE,
        }:
            has_exit_order = bool(cycle.protective_sell_order_ref or cycle.sell_order_ref)
            if has_exit_order and buy_qty > total_sell_qty:
                cycle.stage = Stage.ERROR
                cycle.error_message = (
                    "A late BUY execution increased the app-owned quantity after a SELL order had already been created. "
                    "Trading is paused for manual review so the exit quantity cannot be understated."
                )

        if total_sell_qty > buy_qty > 0:
            cycle.stage = Stage.ERROR
            cycle.error_message = (
                f"The execution ledger contains {total_sell_qty} SELL shares for only {buy_qty} app-owned BUY shares. "
                "Trading is paused for manual review to prevent an unintended short position."
            )

        if cycle.avg_buy_price and cycle.avg_sell_price and cycle.sell_filled_qty > 0:
            overlap = min(cycle.buy_filled_qty, cycle.sell_filled_qty)
            cycle.gross_pnl = (cycle.avg_sell_price - cycle.avg_buy_price) * overlap
            cycle.net_pnl = cycle.gross_pnl - cycle.buy_commission - cycle.sell_commission

        cycle.touch()
        self.storage.upsert_cycle(cycle)
        if self.active_cycle is not None and self.active_cycle.id == cycle.id:
            self.active_cycle = cycle
        if follow_actions and self.active_cycle is not None and self.active_cycle.id == cycle.id:
            self._execute_actions(follow_actions, cycle)

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            if value in (None, ""):
                return None
            ivalue = int(value)
            return ivalue if ivalue > 0 else None
        except Exception:
            return None

    def _pump_broker_callbacks(self, process_timeout: float = 0.0) -> dict[str, Any]:
        """Pump broker callbacks and update connectivity without running strategy work."""
        process_events = getattr(self.adapter, "process_events", None)
        if callable(process_events):
            try:
                process_events(max(0.0, float(process_timeout)))
            except Exception as exc:
                if not self.adapter.is_connected():
                    self._handle_broker_connection_problem(exc)
                    return self._adapter_connectivity_snapshot()
        self._drain_broker_events()
        if self._storage_fault_active:
            connectivity = self._adapter_connectivity_snapshot()
            self._broker_connectivity = connectivity
            self._last_broker_refresh_monotonic = time.monotonic()
            return connectivity
        return self._refresh_broker_connectivity_snapshot()

    def _pump_broker_transport_only(self, process_timeout: float = 0.0) -> dict[str, Any]:
        """Keep the IBKR socket serviced without applying events during a storage fault.

        Draining order/execution callbacks can mutate cycle state and requires
        SQLite persistence.  Once storage is unhealthy, those callbacks remain
        queued only for the short watchdog-restart window; the replacement
        process then obtains authoritative positions, open orders and executions
        through the existing reconciliation path.
        """
        process_events = getattr(self.adapter, "process_events", None)
        if callable(process_events):
            try:
                process_events(max(0.0, float(process_timeout)))
            except Exception as exc:
                self._write_emergency_log(
                    "IBKR transport pumping failed while SQLite was unavailable.",
                    exc=exc,
                    context={"storage_operation": self._storage_fault_operation},
                )
        try:
            connectivity = self._adapter_connectivity_snapshot()
        except Exception as exc:
            self._write_emergency_log(
                "Could not inspect IBKR connectivity during a storage fault.",
                exc=exc,
            )
            return dict(self._broker_connectivity or {})
        self._broker_connectivity = connectivity
        self._last_broker_refresh_monotonic = time.monotonic()
        return connectivity

    def _run_broker_cycle(self, process_timeout: float = 0.0) -> bool:
        """Run the independent broker cadence and report strategy readiness.

        The local API socket, Gateway-to-IBKR server link, and actual streaming
        quote events are separate facts. Broker callbacks are always pumped
        before strategy work. No strategy advancement, order polling, or new
        submission occurs while the upstream link is unavailable or
        post-reconnect reconciliation is pending.
        """
        if self._storage_fault_active:
            if self.connected and self.adapter.is_connected():
                self._pump_broker_transport_only(process_timeout)
            return False
        if not self._ensure_connection_alive():
            return False
        connectivity = self._pump_broker_callbacks(process_timeout)
        if connectivity.get("upstream_connected") is not True:
            if str(connectivity.get("state") or "") not in {"upstream_disconnected", "api_port_reset"}:
                self._handle_upstream_connectivity_lost(connectivity)
            return False
        if self._storage_fault_active:
            # Keep processing IBKR callbacks and connectivity while all trading
            # and reconciliation paths remain fail-closed.
            return False
        if self._upstream_recovery_pending:
            self._recover_upstream_session_if_needed()
            return False
        if self._watchdog_auto_recovery_pending:
            self._attempt_watchdog_auto_recovery()
            return False
        return True

    def _run_strategy_cycle(self, *, price_timeout: float = 0.0) -> None:
        """Evaluate one strategy cadence using the latest broker callback state.

        Scheduled reads use a zero timeout and inspect the existing streaming
        subscription without sleeping. Explicit confirmation, startup recovery,
        and the compatibility ``_tick`` wrapper retain their bounded wait.
        """
        if self._storage_fault_active or self._watchdog_auto_recovery_pending:
            return
        if not self.connected or not self.adapter.is_connected():
            return
        connectivity = self._broker_connectivity
        if connectivity.get("upstream_connected") is not True or self._upstream_recovery_pending:
            return
        read_timeout = max(0.0, float(price_timeout))
        if self._startup_resume_required:
            self._refresh_confirmed_market_data_if_due(timeout=read_timeout)
            return
        if self.active_cycle is None:
            # A confirmed ticker remains observable without an active cycle.
            # Only a newly delivered event is counted as fresh; cached Ticker
            # fields can still be displayed but cannot drive strategy logic.
            self._refresh_confirmed_market_data_if_due(timeout=read_timeout)
            return
        cycle = self.active_cycle
        if cycle.stage != Stage.WAIT_RISE_TRIGGER:
            self._reset_stage3_sell_confirmation()
        if cycle.stage in {Stage.ERROR, Stage.MANUAL_REVIEW, Stage.STOPPED, Stage.IDLE}:
            self._refresh_confirmed_market_data_if_due(timeout=read_timeout)
            return
        if cycle.stage == Stage.CYCLE_COMPLETE:
            if cycle.stop_after_current_cycle or not self.strategy.auto_repeat:
                self._refresh_confirmed_market_data_if_due(timeout=read_timeout)
                return

        fetched_price, last_price = self._poll_price_if_due(cycle, timeout=read_timeout)
        cycle = self.active_cycle or cycle
        snapshot = self.price_snapshot or {}
        stage3_tracked_event = bool(
            cycle.stage == Stage.WAIT_RISE_TRIGGER
            and snapshot.get("market_data_field_tracking")
            and snapshot.get("api_data_received_in_latest_read")
        )
        stage3_current_quote: Optional[dict[str, Any]] = None
        if stage3_tracked_event:
            # The generic selected price can be intentionally unusable when
            # marketPrice falls back to an unchanged Last. Stage 3 nevertheless
            # needs to inspect this actual quote event so it can validate or
            # reset the consecutive bid confirmations.
            stage3_current_quote, _ = self._stage3_sell_quote_evidence(
                cycle,
                require_latest_event=True,
                require_trigger=False,
            )
            current_bid = (
                self._positive_float(stage3_current_quote.get("bid"))
                if stage3_current_quote is not None
                else None
            )
            if current_bid is not None:
                fetched_price = True
                last_price = current_bid
            elif not fetched_price:
                # _advance_waiting_cycle_from_price reads the field-tracked
                # snapshot directly and will fail closed. Supply only an
                # existing actionable cycle price so the invalid quote can be
                # audited without promoting the stale selected fallback.
                last_price = self._positive_float(cycle.last_price)
                fetched_price = last_price is not None

        if cycle.stage in {Stage.WAIT_INITIAL_DROP, Stage.WAIT_RISE_TRIGGER}:
            if cycle.stage == Stage.WAIT_RISE_TRIGGER and cycle.protective_sell_order_ref:
                try:
                    polled = self.adapter.poll_order(cycle.protective_sell_order_ref)
                except BrokerAdapterError as exc:
                    self._handle_broker_connection_problem(exc)
                    return
                if polled:
                    if self._handle_protective_sell_order_poll(cycle, polled):
                        return
                    cycle = self.active_cycle or cycle
                elif int(cycle.protective_sell_filled_qty or 0) > 0:
                    # Once a protective order has partially filled, absence of
                    # a current broker status is not permission to create a
                    # second exit from price logic. Wait fail-closed until the
                    # original order can be polled or formal reconciliation
                    # determines the remaining app-owned quantity.
                    self._log_price_warning_throttled(
                        cycle,
                        "A partially filled protective SELL order is temporarily unavailable from IBKR. "
                        "Price-triggered exit logic remains paused until its status is reconciled.",
                        throttle_key=f"{cycle.id}|protective_partial_poll_unavailable",
                    )
                    return
            if not fetched_price:
                # A newly consumed event that cannot supply an actionable
                # strategy price (for example CHIP's unchanged cached Last
                # fallback after one quote side disappeared) breaks the
                # consecutive Stage-3 confirmation sequence. Cached reads
                # without a new event do not reset an otherwise valid first
                # confirmation.
                if stage3_tracked_event:
                    self._reset_stage3_sell_confirmation(cycle.id)
                return
            if last_price is None:
                self._log_price_warning_throttled(
                    cycle,
                    "Waiting for a fresh market-data event. Cached TWS/API fields cannot advance the strategy.",
                )
                return
            if cycle.stage == Stage.WAIT_RISE_TRIGGER:
                # For field-tracked Stage 3, close-before-RTH may use only a
                # fresh complete spread-checked quote. An invalid quote still
                # reaches the normal Stage-3 guard below so confirmations reset,
                # but it cannot trigger a market liquidation from an old price.
                close_reference = (
                    self._positive_float(stage3_current_quote.get("bid"))
                    if stage3_tracked_event and stage3_current_quote is not None
                    else (None if stage3_tracked_event else last_price)
                )
                if close_reference is not None and self._liquidate_profitable_stage3_before_close_if_needed(
                    cycle,
                    close_reference,
                ):
                    return
            rth_status = self._update_rth_status(self.contract)
            is_rth = bool(rth_status.get("is_open", True))
            message = str(rth_status.get("message") or rth_status.get("source") or "")
            next_cycle, actions = self._advance_waiting_cycle_from_price(
                cycle,
                last_price,
                is_rth=is_rth,
                rth_message=message,
            )
            self._commit_waiting_transition(next_cycle, actions)
        elif cycle.stage == Stage.BUY_TRAIL_ACTIVE and cycle.buy_order_ref:
            self._cancel_buy_before_close_if_needed(cycle)
            cycle = self.active_cycle or cycle
            if fetched_price and last_price is not None:
                cycle.last_price = last_price
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
            try:
                polled = self.adapter.poll_order(cycle.buy_order_ref)
            except BrokerAdapterError as exc:
                self._handle_broker_connection_problem(exc)
                return
            if polled:
                self._handle_buy_order_poll(cycle, polled)
        elif cycle.stage == Stage.SELL_TRAIL_ACTIVE and cycle.sell_order_ref:
            self._cancel_sell_and_liquidate_before_close_if_needed(cycle)
            cycle = self.active_cycle or cycle
            if cycle.stage != Stage.SELL_TRAIL_ACTIVE or not cycle.sell_order_ref:
                return
            if fetched_price and last_price is not None:
                cycle.last_price = last_price
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
            try:
                polled = self.adapter.poll_order(cycle.sell_order_ref)
            except BrokerAdapterError as exc:
                self._handle_broker_connection_problem(exc)
                return
            if polled:
                self._handle_sell_order_poll(cycle, polled)
        elif cycle.stage == Stage.CYCLE_COMPLETE:
            self._maybe_start_next_cycle()

    def _tick(self) -> None:
        """Compatibility wrapper for tests and explicit single-cycle callers."""
        if self._run_broker_cycle(process_timeout=0.05):
            self._run_strategy_cycle(price_timeout=0.75)

    def _seconds_until_next_price_poll(self) -> float:
        if self.PRICE_POLL_INTERVAL_SECONDS <= 0:
            return 0.0
        if self._last_price_poll_monotonic <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last_price_poll_monotonic
        return max(0.0, self.PRICE_POLL_INTERVAL_SECONDS - elapsed)

    @staticmethod
    def _price_field_signature(fields: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        signature: list[tuple[str, Any]] = []
        for key in sorted(fields):
            value = fields.get(key)
            try:
                value = round(float(value), 8) if value is not None else None
            except Exception:
                value = str(value) if value is not None else None
            signature.append((str(key), value))
        return tuple(signature)

    def _atr_rth_open_for_updates(self) -> bool:
        """Return whether ATR observations may be collected and applied.

        The controller maintains its current-session ATR input buffer regardless
        of whether adaptation is enabled, but it appends observations only while
        regular trading hours are open. Before the first contract-status probe,
        a missing RTH snapshot is treated as provisionally open so startup and
        test adapters can initialize; afterward, only an explicit open state
        permits collection or adaptive percentage updates.
        """
        status = self._latest_rth_status
        if status is None:
            return True
        return bool(status.get("is_open"))

    def _apply_atr_adaptive_settings_if_ready(self, atr_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Apply ATR-derived percentages to draft settings and safe active-cycle fields.

        The app keeps one strategy path: the four percentage settings are still
        the values used by the strategy engine.  ATR mode only rewrites those
        percentages when enough app-observed API bars are available.
        """
        if not bool(getattr(self.strategy, "atr_adaptive_enabled", False)):
            return {}
        if not bool(atr_snapshot.get("ready")):
            return {}
        updated, adaptive = strategy_with_atr_adaptive_percentages(self.strategy, atr_snapshot.get("atr_pct"))
        if not adaptive:
            return {}
        old_values = {
            "initial_drop_pct": float(getattr(self.strategy, "initial_drop_pct", 0.0) or 0.0),
            "buy_rebound_trail_pct": float(getattr(self.strategy, "buy_rebound_trail_pct", 0.0) or 0.0),
            "rise_trigger_pct": float(getattr(self.strategy, "rise_trigger_pct", 0.0) or 0.0),
            "sell_trailing_stop_pct": float(getattr(self.strategy, "sell_trailing_stop_pct", 0.0) or 0.0),
        }
        if bool(adaptive.get("atr_adapt_protective_sell_enabled")):
            old_values["protective_sell_trailing_stop_pct"] = float(getattr(self.strategy, "protective_sell_trailing_stop_pct", 0.0) or 0.0)
        changed = any(abs(float(adaptive.get(key, old_values.get(key, 0.0))) - old_values.get(key, 0.0)) >= 0.005 for key in old_values)
        if changed:
            self.strategy = updated
            self.storage.save_strategy_settings(updated)
            self._last_atr_adaptive_values = dict(adaptive)
            cycle = self.active_cycle
            if cycle is not None and cycle.ticker == updated.normalized_ticker() and cycle.stage not in {Stage.IDLE, Stage.CYCLE_COMPLETE, Stage.STOPPED, Stage.ERROR, Stage.MANUAL_REVIEW}:
                realized = self.storage.get_realized_net_profit_for_ticker(cycle.ticker, con_id=cycle.con_id)
                next_cycle, changed_fields = StrategyEngine.apply_editable_settings(cycle, updated, realized)
                if changed_fields or next_cycle.to_dict() != cycle.to_dict():
                    self.active_cycle = next_cycle
                    self.storage.upsert_cycle(next_cycle)
            self.storage.add_decision_event(
                event_type="ATR_ADAPTIVE_UPDATE",
                message=(
                    "ATR adaptive mode updated strategy percentages: "
                    f"drop {adaptive['initial_drop_pct']:.2f}%, buy rebound {adaptive['buy_rebound_trail_pct']:.2f}%, "
                    + (f"min profit {adaptive['rise_trigger_pct']:.2f}%" if bool(adaptive.get("atr_adapt_minimum_profit_enabled", True)) else f"min profit manual {adaptive['rise_trigger_pct']:.2f}%")
                    + f", sell trail {adaptive['sell_trailing_stop_pct']:.2f}%"
                    + (f", protective SELL {adaptive['protective_sell_trailing_stop_pct']:.2f}%" if bool(adaptive.get("atr_adapt_protective_sell_enabled")) else "")
                    + "."
                ),
                cycle=self.active_cycle,
                stage_before=(self.active_cycle.stage.value if self.active_cycle else None),
                stage_after=(self.active_cycle.stage.value if self.active_cycle else None),
                decision_result="applied",
                raw={"atr": atr_snapshot, "adaptive_percentages": adaptive},
            )
        return adaptive

    @staticmethod
    def _price_history_signature_for(points: "deque[tuple[float, float]]") -> tuple[Any, ...]:
        """Return a cheap signature used to detect a reset or direct replacement."""
        if not points:
            return (0, None, None)
        return (len(points), points[0], points[-1])

    @staticmethod
    def _merge_atr_observation(
        bars: "deque[dict[str, float]]",
        timestamp: float,
        price: float,
        bar_seconds: int,
    ) -> None:
        """Merge one ordered observation into an incremental fixed-time OHLC bar."""
        bucket = int(float(timestamp) // int(bar_seconds))
        if bars and int(bars[-1]["bucket"]) == bucket:
            bar = bars[-1]
            bar["high"] = max(float(bar["high"]), float(price))
            bar["low"] = min(float(bar["low"]), float(price))
            bar["close"] = float(price)
            bar["end_ts"] = float(timestamp)
            return
        bars.append(
            {
                "bucket": float(bucket),
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
                "start_ts": float(timestamp),
                "end_ts": float(timestamp),
            }
        )

    def _rebuild_incremental_atr_bars(self, bar_seconds: int) -> None:
        """Rebuild bounded OHLC state after a bar-size or history reset."""
        self._atr_bars.clear()
        for timestamp, price in self._price_history:
            try:
                timestamp_value = float(timestamp)
                price_value = float(price)
            except Exception:
                continue
            if not isfinite(timestamp_value) or not isfinite(price_value) or price_value <= 0:
                continue
            self._merge_atr_observation(self._atr_bars, timestamp_value, price_value, bar_seconds)
        self._atr_bar_seconds_cache = int(bar_seconds)
        self._atr_history_signature = self._price_history_signature_for(self._price_history)

    def _update_incremental_atr_bars(self, timestamp: float, price: float, bar_seconds: int) -> None:
        """Update cached OHLC bars for a newly appended RTH price observation."""
        bucket = int(float(timestamp) // int(bar_seconds))
        if self._atr_bar_seconds_cache != int(bar_seconds):
            self._rebuild_incremental_atr_bars(bar_seconds)
            return
        if self._atr_bars and bucket < int(self._atr_bars[-1]["bucket"]):
            self._rebuild_incremental_atr_bars(bar_seconds)
            return
        self._merge_atr_observation(self._atr_bars, timestamp, price, bar_seconds)
        self._atr_history_signature = self._price_history_signature_for(self._price_history)

    def _ensure_incremental_atr_bars(self, bar_seconds: int) -> None:
        """Synchronize OHLC cache when configuration or tests replace history."""
        signature = self._price_history_signature_for(self._price_history)
        if self._atr_bar_seconds_cache != int(bar_seconds) or signature != self._atr_history_signature:
            self._rebuild_incremental_atr_bars(bar_seconds)

    @staticmethod
    def _atr_result_from_bars(
        bars: list[dict[str, float]],
        *,
        period: int,
        bar_seconds: int,
    ) -> dict[str, Any]:
        """Calculate simple-average ATR from pre-aggregated OHLC bars."""
        required = period + 1
        if not bars:
            return {
                "ready": False,
                "source": "app_observed_api_prices",
                "reason": "no usable price observations",
                "atr": None,
                "atr_pct": None,
                "bars_available": 0,
                "bars_required": required,
                "period": period,
                "bar_seconds": bar_seconds,
            }
        if len(bars) < required:
            return {
                "ready": False,
                "source": "app_observed_api_prices",
                "reason": f"need at least {required} bars; have {len(bars)}",
                "atr": None,
                "atr_pct": None,
                "bars_available": len(bars),
                "bars_required": required,
                "period": period,
                "bar_seconds": bar_seconds,
                "latest_close": bars[-1]["close"],
            }
        true_ranges: list[float] = []
        for index in range(1, len(bars)):
            high = float(bars[index]["high"])
            low = float(bars[index]["low"])
            previous_close = float(bars[index - 1]["close"])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        used_ranges = true_ranges[-period:]
        atr = sum(used_ranges) / period
        latest_close = float(bars[-1]["close"])
        atr_pct = (atr / latest_close) * 100.0 if latest_close > 0 else None
        ready = bool(atr_pct is not None and atr_pct > 0 and isfinite(float(atr_pct)))
        return {
            "ready": ready,
            "source": "app_observed_api_prices",
            "reason": "ok",
            "atr": atr,
            "atr_pct": atr_pct,
            "bars_available": len(bars),
            "bars_required": required,
            "period": period,
            "bar_seconds": bar_seconds,
            "latest_close": latest_close,
            "latest_bar_high": bars[-1]["high"],
            "latest_bar_low": bars[-1]["low"],
            "true_ranges_used": len(used_ranges),
        }

    def _build_atr_snapshot(self, now_monotonic: float) -> dict[str, Any]:
        """Build ATR diagnostics from incremental in-memory RTH OHLC bars.

        Price observations are collected independently of whether adaptive
        percentages are enabled. This lets the operator enable adaptation later
        without discarding bars already observed during the current app session.
        Only an explicit RTH-open state can make the result order-driving.
        """
        period = max(2, int(getattr(self.strategy, "atr_period", 14) or 14))
        bar_seconds = max(5, int(getattr(self.strategy, "atr_bar_seconds", 60) or 60))
        adaptive_enabled = bool(getattr(self.strategy, "atr_adaptive_enabled", False))
        rth_open = self._atr_rth_open_for_updates()
        self._ensure_incremental_atr_bars(bar_seconds)
        cutoff = now_monotonic - max(float((period + 4) * bar_seconds), 300.0)
        recent_bars = [bar for bar in self._atr_bars if float(bar.get("end_ts", 0.0)) >= cutoff]
        result = self._atr_result_from_bars(recent_bars, period=period, bar_seconds=bar_seconds)
        calculation_ready = bool(result.get("ready"))
        result["source"] = "app_observed_api_prices_rth_only"
        result["rth_only"] = True
        result["rth_open"] = rth_open
        result["collecting"] = rth_open
        result["collection_enabled"] = True
        result["adaptive_enabled"] = adaptive_enabled
        result["data_ready"] = calculation_ready
        if not rth_open:
            result["ready"] = False
            result["reason"] = (
                "ATR data collection and adaptive updates pause outside RTH; "
                "only in-memory regular-trading-hours observations are used."
            )
        elif not adaptive_enabled:
            if calculation_ready:
                result["reason"] = "ATR data is ready; adaptive percentage updates are disabled."
            else:
                base_reason = str(result.get("reason") or "waiting for enough RTH-only observations")
                result["reason"] = f"{base_reason} Adaptive percentage updates are disabled."
        return result

    def _record_price_snapshot(self, snapshot: MarketPriceSnapshot, contract: Optional[QualifiedContract]) -> None:
        """Record a market-data read without mistaking cached fields for an update."""
        data = snapshot.to_dict()
        fields = data.get("fields") or {}
        non_null_fields = {key: value for key, value in fields.items() if value is not None}
        api_data_present = bool(non_null_fields or data.get("price") is not None)
        monotonic_now = time.monotonic()
        wall_now = utc_now_iso()

        event_tracking = bool(data.get("market_data_event_tracking"))
        raw_sequence = data.get("market_data_update_sequence")
        sequence: Optional[int]
        try:
            sequence = int(raw_sequence) if raw_sequence is not None else None
        except Exception:
            sequence = None
        subscription_id = str(data.get("market_data_subscription_id") or "legacy")
        event_seen = bool(data.get("api_data_received"))
        if event_tracking:
            token = (subscription_id, int(sequence or 0))
            actual_update = bool(event_seen and (sequence or 0) > 0 and token != self._last_market_data_event_token)
        else:
            # Deterministic test adapters predating event identity remain usable.
            # The production adapter always takes the event-tracked branch.
            token = None
            actual_update = bool(event_seen or api_data_present)

        snapshot_upstream = data.get("upstream_connected")
        upstream_ready = snapshot_upstream is not False
        if event_tracking:
            upstream_ready = snapshot_upstream is True
        if not upstream_ready:
            actual_update = False

        if actual_update:
            if token is not None:
                self._last_market_data_event_token = token
            self._api_data_seen_count += 1
            event_age = data.get("market_data_update_age_seconds")
            if event_tracking and isinstance(event_age, (int, float)):
                self._api_last_data_monotonic = monotonic_now - max(0.0, float(event_age))
            else:
                self._api_last_data_monotonic = monotonic_now
            self._api_last_data_wall_time = str(data.get("market_data_update_received_at") or wall_now)
            self._api_data_invalidated = False
            self._api_data_invalidated_reason = ""
            signature = self._price_field_signature(fields)
            if signature != self._api_last_field_signature:
                self._api_last_field_signature = signature
                self._api_data_change_count += 1
                self._api_last_change_monotonic = self._api_last_data_monotonic or monotonic_now
                self._api_last_change_wall_time = self._api_last_data_wall_time

        data_age = max(0.0, monotonic_now - self._api_last_data_monotonic) if self._api_last_data_monotonic > 0 else None
        change_age = max(0.0, monotonic_now - self._api_last_change_monotonic) if self._api_last_change_monotonic > 0 else None
        data["api_data_present"] = api_data_present
        data["api_non_null_field_count"] = len(non_null_fields)
        data["api_data_seen_count"] = self._api_data_seen_count
        data["api_data_update_count"] = self._api_data_seen_count
        # Kept as a compatibility alias for older audit readers; the value now
        # counts actual update events, not cached reads with non-null fields.
        data["api_data_reads_with_values"] = self._api_data_seen_count
        data["api_data_change_count"] = self._api_data_change_count
        data["api_last_data_received_at"] = self._api_last_data_wall_time
        data["api_data_last_received_at"] = self._api_last_data_wall_time
        data["api_last_value_change_at"] = self._api_last_change_wall_time
        data["api_data_last_change_at"] = self._api_last_change_wall_time
        data["api_data_age_seconds"] = data_age
        data["api_data_last_received_age_seconds"] = data_age
        data["api_data_change_age_seconds"] = change_age
        data["api_data_last_change_age_seconds"] = change_age
        data["api_data_received_in_latest_read"] = actual_update
        data["market_data_update_consumed"] = actual_update
        data["api_data_field_count"] = len(non_null_fields)
        data["cached_fields_only"] = bool(api_data_present and not actual_update)
        data["api_data_invalidated"] = bool(self._api_data_invalidated)
        data["api_data_invalidated_reason"] = self._api_data_invalidated_reason
        data["api_data_invalidated_at"] = self._api_data_invalidated_at

        field_tracking = bool(data.get("market_data_field_tracking"))
        basis_updated_value = data.get("selected_price_basis_updated_in_event")
        if basis_updated_value is None:
            # Compatibility with field-tracked snapshots created before the
            # update/change split: a recorded change was also an update.
            basis_updated_value = data.get("selected_price_basis_changed_in_update")
        selected_basis_updated = bool(basis_updated_value) if field_tracking else True
        selected_basis = str(data.get("selected_price_basis") or data.get("source") or "unknown")
        data["selected_price_basis_event_fresh"] = bool(actual_update and selected_basis_updated)
        data["selected_price_cached_in_fresh_event"] = bool(
            actual_update and field_tracking and not selected_basis_updated
        )

        freshness_limit = max(0.1, float(getattr(self.strategy, "max_selected_price_age_seconds", 3.0) or 3.0))
        if snapshot_upstream is False:
            data["api_data_state"] = "upstream_disconnected"
            data["api_data_indicator_text"] = "IBKR SERVER LINK LOST - cached quote fields are invalid"
        elif self._api_data_invalidated:
            data["api_data_state"] = "invalidated"
            data["api_data_indicator_text"] = self._api_data_invalidated_reason or "WAITING FOR A FRESH MARKET-DATA UPDATE"
        elif actual_update:
            if field_tracking and not selected_basis_updated:
                data["api_data_state"] = "selected_field_unchanged"
                data["api_data_indicator_text"] = (
                    f"FRESH API EVENT - selected {selected_basis} field did not update; strategy price is paused"
                )
            else:
                data["api_data_state"] = "receiving"
                data["api_data_indicator_text"] = f"FRESH API UPDATE - {len(non_null_fields)} raw fields"
        elif data_age is not None and data_age > freshness_limit:
            data["api_data_state"] = "stale"
            data["api_data_indicator_text"] = f"API DATA STALE - last actual update {data_age:.1f}s ago"
        elif self._api_last_data_monotonic > 0:
            data["api_data_state"] = "recent"
            data["api_data_indicator_text"] = "NO NEW UPDATE IN THIS READ - last actual update remains recent"
        elif api_data_present:
            data["api_data_state"] = "cached_only"
            data["api_data_indicator_text"] = "CACHED API FIELDS ONLY - waiting for an actual update event"
        else:
            data["api_data_state"] = "none"
            data["api_data_indicator_text"] = "NO API PRICE DATA RECEIVED"

        try:
            selected_price = float(data.get("price") or 0.0)
        except Exception:
            selected_price = 0.0
        data["strategy_price_usable"] = bool(
            actual_update
            and upstream_ready
            and selected_price > 0
            and selected_basis_updated
        )
        if actual_update and field_tracking and not selected_basis_updated:
            data["strategy_price_block_reason"] = (
                f"The selected {selected_basis} price came from a field that did not update in this market-data event."
            )
        else:
            data["strategy_price_block_reason"] = ""

        if contract is not None:
            raw = contract.raw
            data["contract"] = {
                "ticker": contract.ticker,
                "con_id": contract.con_id,
                "primary_exchange": contract.primary_exchange,
                "local_symbol": contract.local_symbol,
                "trading_class": contract.trading_class,
                "description": getattr(contract, "description", ""),
                "industry": getattr(contract, "industry", ""),
                "category": getattr(contract, "category", ""),
                "subcategory": getattr(contract, "subcategory", ""),
                "min_tick": getattr(contract, "min_tick", 0.01),
                "exchange": str(
                    getattr(contract, "exchange", "")
                    or getattr(raw, "exchange", "")
                    or ""
                ),
                "currency": str(
                    getattr(contract, "currency", "")
                    or getattr(raw, "currency", "")
                    or ""
                ),
                "sec_type": str(
                    getattr(contract, "sec_type", "")
                    or getattr(raw, "secType", "")
                    or ""
                ),
                "min_size": getattr(contract, "min_size", 1.0),
                "size_increment": getattr(contract, "size_increment", 1.0),
            }
        if self._latest_rth_status is not None:
            data["rth_status"] = dict(self._latest_rth_status)
            data["rth_open"] = bool(self._latest_rth_status.get("is_open"))
            data["rth_message"] = self._latest_rth_status.get("message")
        data["poll_interval_seconds"] = self.PRICE_POLL_INTERVAL_SECONDS
        data["next_refresh_seconds"] = self.PRICE_POLL_INTERVAL_SECONDS
        atr_rth_open = self._atr_rth_open_for_updates()
        data["atr_rth_only"] = True
        data["atr_rth_open"] = atr_rth_open
        if data["strategy_price_usable"] and atr_rth_open:
            # Preserve the adapter callback time rather than the later controller
            # read time. A busy worker can consume a legitimate event several
            # seconds after it arrived; shifting that event forward would distort
            # ATR buckets and short-window volatility diagnostics.
            observation_monotonic = (
                self._api_last_data_monotonic
                if actual_update and self._api_last_data_monotonic > 0
                else monotonic_now
            )
            self._price_history.append((observation_monotonic, selected_price))
            max_age = 6 * 60 * 60
            while self._price_history and monotonic_now - self._price_history[0][0] > max_age:
                self._price_history.popleft()
            bar_seconds = max(5, int(getattr(self.strategy, "atr_bar_seconds", 60) or 60))
            self._update_incremental_atr_bars(observation_monotonic, selected_price, bar_seconds)

        atr_config = (
            int(getattr(self.strategy, "atr_period", 14) or 14),
            int(getattr(self.strategy, "atr_bar_seconds", 60) or 60),
            bool(getattr(self.strategy, "atr_adaptive_enabled", False)),
            atr_rth_open,
        )
        atr_cache_expired = (
            not self._last_atr_snapshot
            or self._last_atr_snapshot_config != atr_config
            or monotonic_now - self._last_atr_snapshot_monotonic >= self.ATR_CALCULATION_INTERVAL_SECONDS
        )
        if atr_cache_expired:
            atr_snapshot = self._build_atr_snapshot(monotonic_now)
            self._last_atr_snapshot = dict(atr_snapshot)
            self._last_atr_snapshot_monotonic = monotonic_now
            self._last_atr_snapshot_config = atr_config
        else:
            atr_snapshot = dict(self._last_atr_snapshot)
        adaptive = self._apply_atr_adaptive_settings_if_ready(atr_snapshot)
        data["atr"] = atr_snapshot
        data["atr_ready"] = bool(atr_snapshot.get("ready"))
        data["atr_value"] = atr_snapshot.get("atr")
        data["atr_pct"] = atr_snapshot.get("atr_pct")
        data["atr_bars_available"] = atr_snapshot.get("bars_available")
        data["atr_bars_required"] = atr_snapshot.get("bars_required")
        data["atr_adaptive_enabled"] = bool(getattr(self.strategy, "atr_adaptive_enabled", False))
        data["atr_adaptive_applied"] = bool(adaptive)
        data["atr_adaptive_percentages"] = adaptive or dict(self._last_atr_adaptive_values)
        cycle = self.active_cycle
        if cycle is not None:
            data["cycle_id"] = cycle.id
            data["cycle_number"] = cycle.cycle_number
            data["ticker"] = cycle.ticker
            data["stage"] = cycle.stage.value
            data["anchor_price"] = cycle.anchor_price
            data["drop_trigger_price"] = cycle.drop_trigger_price
            data["buy_initial_trail_stop_price"] = cycle.buy_initial_trail_stop_price
            data["avg_buy_price"] = cycle.avg_buy_price
            data["rise_trigger_price"] = cycle.rise_trigger_price
            data["sell_initial_trail_stop_price"] = cycle.sell_initial_trail_stop_price
            data["protective_sell_initial_stop_price"] = cycle.protective_sell_initial_stop_price
            data["native_order_trigger"] = native_trailing_order_diagnostics(
                stage=cycle.stage,
                fields=fields,
                selected_price=data.get("price"),
                buy_initial_stop=cycle.buy_initial_trail_stop_price,
                sell_initial_stop=cycle.sell_initial_trail_stop_price,
                buy_order_ref=cycle.buy_order_ref,
                sell_order_ref=cycle.sell_order_ref,
                trigger_method=2,
            )
        else:
            data["native_order_trigger"] = native_trailing_order_diagnostics(
                stage=None,
                fields=fields,
                selected_price=data.get("price"),
                trigger_method=2,
            )
        try:
            self._market_capture.record_snapshot(data, monotonic_ts=monotonic_now, wall_time_utc=wall_now)
        except Exception as exc:
            self._log("WARN", f"Market-data capture buffer could not record snapshot: {exc}", self.active_cycle)
        self.price_snapshot = data
        self._last_price_poll_monotonic = monotonic_now

    def _start_trade_market_data_capture(self, event_type: str, cycle: CycleState, polled: Optional[PolledOrderState] = None, extra: Optional[dict[str, Any]] = None) -> None:
        """Start a RAM-only 15m-before/15m-after debug capture for a fill.

        The capture manager writes no file until the full post-trade window has
        been observed.  If the app closes before then, this pending capture is
        intentionally lost rather than flushed partially.
        """
        try:
            now_mono = time.monotonic()
            payload = {
                "event_type": event_type,
                "event_time_utc": utc_now_iso(),
                "cycle": cycle.snapshot(),
                "price_snapshot_at_event": self.price_snapshot or {},
                "connection": asdict(self.connection),
                "strategy": asdict(self.strategy),
                "order_poll": polled.raw if polled is not None else None,
                "order_ref": polled.order_ref if polled is not None else (cycle.buy_order_ref or cycle.sell_order_ref or cycle.protective_sell_order_ref or ""),
                "order_id": polled.order_id if polled is not None else None,
                "perm_id": polled.perm_id if polled is not None else None,
                "status": polled.status if polled is not None else None,
                "filled": polled.filled if polled is not None else None,
                "avg_fill_price": polled.avg_fill_price if polled is not None else None,
                "commission": polled.commission if polled is not None else None,
            }
            if extra:
                payload.update(extra)
            event_id = self._market_capture.start_capture(
                event_type=event_type,
                event_monotonic=now_mono,
                ticker=cycle.ticker,
                cycle_id=cycle.id,
                cycle_number=cycle.cycle_number,
                order_ref=str(payload.get("order_ref") or ""),
                perm_id=payload.get("perm_id"),
                payload=payload,
            )
            if event_id:
                self.storage.add_decision_event(
                    event_type="MARKET_DATA_CAPTURE_STARTED",
                    message=f"Started RAM-only market-data capture for {event_type}; file will be written after the 15-minute post-trade window completes.",
                    cycle=cycle,
                    decision_result="capture_pending",
                    raw={"capture_event_id": event_id, "pre_window_seconds": 900, "post_window_seconds": 900},
                )
        except Exception as exc:
            self._log("WARN", f"Could not start market-data capture for {event_type}: {exc}", cycle)

    def _set_price_error_snapshot(self, exc: Exception | str) -> None:
        data = {
            "price": None,
            "source": "error",
            "requested_market_data_type": self.connection.market_data_type,
            "subscription_market_data_type": None,
            "fields": {},
            "timestamp": utc_now_iso(),
            "age_seconds": 0.0,
            "status": "Market-data request failed",
            "error": str(exc),
            "poll_interval_seconds": self.PRICE_POLL_INTERVAL_SECONDS,
            "next_refresh_seconds": self.PRICE_POLL_INTERVAL_SECONDS,
        }
        now = time.monotonic()
        data_age = max(0.0, now - self._api_last_data_monotonic) if self._api_last_data_monotonic > 0 else None
        change_age = max(0.0, now - self._api_last_change_monotonic) if self._api_last_change_monotonic > 0 else None
        data.update({
            "api_data_present": False,
            "api_non_null_field_count": 0,
            "api_data_seen_count": self._api_data_seen_count,
            "api_data_update_count": self._api_data_seen_count,
            "api_data_reads_with_values": self._api_data_seen_count,
            "api_data_change_count": self._api_data_change_count,
            "api_last_data_received_at": self._api_last_data_wall_time,
            "api_data_last_received_at": self._api_last_data_wall_time,
            "api_last_value_change_at": self._api_last_change_wall_time,
            "api_data_last_change_at": self._api_last_change_wall_time,
            "api_data_age_seconds": data_age,
            "api_data_last_received_age_seconds": data_age,
            "api_data_change_age_seconds": change_age,
            "api_data_last_change_age_seconds": change_age,
            "api_data_received_in_latest_read": False,
            "market_data_update_consumed": False,
            "api_data_field_count": 0,
            "strategy_price_usable": False,
            "api_data_invalidated": True,
            "api_data_invalidated_reason": str(exc),
            "api_data_state": "stale" if self._api_last_data_monotonic > 0 else "none",
            "api_data_indicator_text": "API DATA STALE - market-data request failed" if self._api_last_data_monotonic > 0 else "NO API PRICE DATA RECEIVED",
        })
        if self._latest_rth_status is not None:
            data["rth_status"] = dict(self._latest_rth_status)
            data["rth_open"] = bool(self._latest_rth_status.get("is_open"))
            data["rth_message"] = self._latest_rth_status.get("message")
        self.price_snapshot = data
        self._last_price_poll_monotonic = now

    def _poll_price_if_due(self, cycle: CycleState, timeout: float = 0.75, force: bool = False) -> tuple[bool, Optional[float]]:
        if not force and self._seconds_until_next_price_poll() > 0:
            return False, None
        try:
            if self.contract is None or self.contract.ticker != cycle.ticker:
                self.contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
            self._update_rth_status(self.contract)
            self.adapter.set_market_data_type(self.connection.market_data_type)
            snapshot = self.adapter.price_snapshot(self.contract, timeout=timeout)
            self._record_price_snapshot(snapshot, self.contract)
            usable = bool((self.price_snapshot or {}).get("strategy_price_usable"))
            return usable, snapshot.price if usable else None
        except BrokerAdapterError as exc:
            self._set_price_error_snapshot(exc)
            self._handle_broker_connection_problem(exc)
            return True, None
        except Exception as exc:
            self._set_price_error_snapshot(exc)
            self._log_price_warning_throttled(cycle, f"Market-data request failed: {exc}")
            return True, None


    def _poll_confirmed_contract_price_if_due(self, timeout: float = 0.75, force: bool = False) -> None:
        if self.contract is None:
            return
        if not force and self._seconds_until_next_price_poll() > 0:
            return
        try:
            self._update_rth_status(self.contract)
            self.adapter.set_market_data_type(self.connection.market_data_type)
            snapshot = self.adapter.price_snapshot(self.contract, timeout=timeout)
            self._record_price_snapshot(snapshot, self.contract)
            if snapshot.price is None:
                ticker = self.contract.ticker
                key = f"{ticker}|CONFIRMED_PRICE|NO_PRICE"
                now = time.monotonic()
                last = self._last_price_warning_at.get(key, 0.0)
                if now - last >= 30.0:
                    self._last_price_warning_at[key] = now
                    self._log("WARN", f"Confirmed contract {ticker}: TWS/API still has no usable price in market-data mode {self.connection.market_data_type}.")
        except Exception as exc:
            self._set_price_error_snapshot(exc)
            self._log("WARN", f"Confirmed contract price refresh failed: {exc}")

    def _log_price_warning_throttled(
        self,
        cycle: CycleState,
        message: str,
        interval_seconds: float = 30.0,
        *,
        throttle_key: Optional[str] = None,
    ) -> None:
        key = throttle_key or f"{cycle.ticker}|{cycle.stage.value}|{message}"
        now = time.monotonic()
        last = self._last_price_warning_at.get(key)
        # Long-running bots can see many transient ticker/stage/error combinations.
        # Keep the warning-throttle cache bounded so it cannot grow indefinitely.
        if len(self._last_price_warning_at) > 512:
            for old_key in list(self._last_price_warning_at)[:256]:
                self._last_price_warning_at.pop(old_key, None)
        if last is None or now - last >= interval_seconds:
            self._last_price_warning_at[key] = now
            self._log("WARN", message, cycle)

    @staticmethod
    def _format_diagnostic_duration(seconds: float) -> str:
        seconds = max(0.0, float(seconds or 0.0))
        if seconds < 60.0:
            return f"{seconds:.0f}s"
        minutes = seconds / 60.0
        if minutes < 60.0:
            return f"{minutes:.1f}m"
        return f"{minutes / 60.0:.1f}h"

    def _emit_diagnostic_event(
        self,
        level: str,
        message: str,
        cycle: Optional[CycleState],
        raw: dict[str, Any],
    ) -> None:
        """Emit a structured diagnostic while tolerating older test doubles."""
        try:
            self._log(level, message, cycle, raw=raw)
        except TypeError as exc:
            # A few focused tests replace _log with a legacy three-argument
            # callable. Production always uses the structured form above.
            detail = str(exc).lower()
            if "raw" not in detail or "keyword" not in detail:
                raise
            self._log(level, message, cycle)

    def _update_audit_condition(
        self,
        *,
        key: str,
        family: str,
        reason_code: str,
        message: str,
        cycle: Optional[CycleState] = None,
        initial_delay_seconds: float = DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
        repeat_interval_seconds: float = DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
        summary_level: str = "WARN",
        first_summary_level: Optional[str] = None,
        immediate_level: Optional[str] = None,
        immediate_message: Optional[str] = None,
        immediate_token: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        kind: str = "generic",
    ) -> dict[str, Any]:
        """Coalesce a persistent condition into bounded operator/audit events.

        Stable keys and reason codes prevent changing ages, prices, and
        timestamps from bypassing suppression. Every observation remains
        available to the GUI through the returned state, while SQLite receives
        only an operationally relevant immediate event and periodic summaries.
        """
        now = time.monotonic()
        wall_now = utc_now_iso()
        normalized_key = str(key or family or "diagnostic")
        normalized_reason = str(reason_code or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._audit_conditions.get(normalized_key)
        if state is None:
            state = {
                "key": normalized_key,
                "kind": str(kind or "generic"),
                "family": str(family or "Diagnostic condition"),
                "cycle_id": cycle.id if cycle else "",
                "ticker": cycle.ticker if cycle else "",
                "first_seen_at": wall_now,
                "last_seen_at": wall_now,
                "first_seen_monotonic": now,
                "last_seen_monotonic": now,
                "occurrence_count": 0,
                "audit_emissions": 0,
                "immediate_emissions": 0,
                "summary_emissions": 0,
                "last_immediate_monotonic": 0.0,
                "last_summary_monotonic": 0.0,
                "reason_counts": {},
                "immediate_reason_codes": set(),
                "latest_reason_code": normalized_reason,
                "latest_message": str(message or ""),
                "latest_context": {},
                "max_metrics": {},
                "initial_delay_seconds": max(0.0, float(initial_delay_seconds or 0.0)),
                "repeat_interval_seconds": max(1.0, float(repeat_interval_seconds or 1.0)),
                "summary_level": str(summary_level or "WARN").upper(),
                "first_summary_level": str(
                    first_summary_level or summary_level or "WARN"
                ).upper(),
            }
            self._audit_conditions[normalized_key] = state
        else:
            # A later observation may increase operational relevance (for
            # example a normal native order wait later crosses its diagnostic
            # trigger).  Escalate timing and severity policies without ever
            # making an existing condition less visible.
            state["initial_delay_seconds"] = min(
                float(state.get("initial_delay_seconds", initial_delay_seconds) or 0.0),
                max(0.0, float(initial_delay_seconds or 0.0)),
            )
            state["repeat_interval_seconds"] = min(
                float(state.get("repeat_interval_seconds", repeat_interval_seconds) or 1.0),
                max(1.0, float(repeat_interval_seconds or 1.0)),
            )
            severity_rank = {"INFO": 1, "WARN": 2, "ERROR": 3}
            existing_level = str(state.get("summary_level") or "WARN").upper()
            requested_level = str(summary_level or "WARN").upper()
            if severity_rank.get(requested_level, 2) > severity_rank.get(existing_level, 2):
                state["summary_level"] = requested_level
            if first_summary_level is not None:
                existing_first_level = str(
                    state.get("first_summary_level") or existing_level
                ).upper()
                requested_first_level = str(first_summary_level).upper()
                if severity_rank.get(requested_first_level, 2) > severity_rank.get(
                    existing_first_level,
                    2,
                ):
                    state["first_summary_level"] = requested_first_level

        state["last_seen_at"] = wall_now
        state["last_seen_monotonic"] = now
        state["occurrence_count"] = int(state.get("occurrence_count", 0) or 0) + 1
        state["latest_reason_code"] = normalized_reason
        state["latest_message"] = str(message or "")
        reason_counts = dict(state.get("reason_counts") or {})
        reason_counts[normalized_reason] = int(reason_counts.get(normalized_reason, 0) or 0) + 1
        state["reason_counts"] = reason_counts

        latest_context = dict(context or {})
        state["latest_context"] = latest_context
        max_metrics = dict(state.get("max_metrics") or {})
        for metric_name, metric_value in latest_context.items():
            if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float)):
                continue
            number = float(metric_value)
            if not isfinite(number):
                continue
            previous = max_metrics.get(metric_name)
            if previous is None or number > float(previous):
                max_metrics[metric_name] = number
        state["max_metrics"] = max_metrics

        immediate_codes = state.get("immediate_reason_codes")
        if not isinstance(immediate_codes, set):
            immediate_codes = set(immediate_codes or [])
            state["immediate_reason_codes"] = immediate_codes
        immediate_identity = str(immediate_token or normalized_reason).strip() or normalized_reason
        immediate_emitted_now = False
        if immediate_level and immediate_identity not in immediate_codes:
            immediate_codes.add(immediate_identity)
            raw = {
                "diagnostic_condition": {
                    "phase": "immediate",
                    "key": normalized_key,
                    "kind": state.get("kind"),
                    "family": state.get("family"),
                    "reason_code": normalized_reason,
                    "reason_counts": dict(reason_counts),
                    "first_seen_at": state.get("first_seen_at"),
                    "last_seen_at": state.get("last_seen_at"),
                    "occurrence_count": int(state.get("occurrence_count", 0) or 0),
                    "latest_context": latest_context,
                    "max_metrics": dict(max_metrics),
                }
            }
            self._emit_diagnostic_event(
                str(immediate_level).upper(),
                str(immediate_message or f"{state['family']}: {message}"),
                cycle,
                raw,
            )
            state["audit_emissions"] = int(state.get("audit_emissions", 0) or 0) + 1
            state["immediate_emissions"] = int(state.get("immediate_emissions", 0) or 0) + 1
            state["last_immediate_monotonic"] = now
            immediate_emitted_now = True

        elapsed = max(0.0, now - float(state.get("first_seen_monotonic", now)))
        summary_emissions = int(state.get("summary_emissions", 0) or 0)
        last_immediate = float(state.get("last_immediate_monotonic", 0.0) or 0.0)
        last_summary = float(state.get("last_summary_monotonic", 0.0) or 0.0)
        initial_delay = float(state.get("initial_delay_seconds", initial_delay_seconds) or 0.0)
        repeat_interval = float(state.get("repeat_interval_seconds", repeat_interval_seconds) or 1.0)
        if summary_emissions == 0:
            summary_reference = max(
                float(state.get("first_seen_monotonic", now)),
                last_immediate,
            )
            summary_interval = initial_delay
        else:
            summary_reference = max(last_summary, last_immediate)
            summary_interval = repeat_interval
        summary_due = bool(
            not immediate_emitted_now
            and now - summary_reference >= summary_interval
        )
        if summary_due:
            occurrence_count = int(state.get("occurrence_count", 0) or 0)
            audit_emissions = int(state.get("audit_emissions", 0) or 0)
            immediate_emissions = int(state.get("immediate_emissions", 0) or 0)
            # This summary represents the current observation in SQLite, so
            # count it together with earlier immediate/summary emissions when
            # reporting how many cadence observations were actually suppressed.
            suppressed = max(0, occurrence_count - (audit_emissions + 1))
            summary = (
                f"{state['family']} has remained active for "
                f"{self._format_diagnostic_duration(elapsed)}: {message} "
                f"Observed {occurrence_count} times; {suppressed} repeated observations suppressed."
            )
            raw = {
                "diagnostic_condition": {
                    "phase": "summary",
                    "key": normalized_key,
                    "kind": state.get("kind"),
                    "family": state.get("family"),
                    "reason_code": normalized_reason,
                    "reason_counts": dict(reason_counts),
                    "first_seen_at": state.get("first_seen_at"),
                    "last_seen_at": state.get("last_seen_at"),
                    "duration_seconds": elapsed,
                    "occurrence_count": occurrence_count,
                    "suppressed_count": suppressed,
                    "immediate_emissions": immediate_emissions,
                    "latest_context": latest_context,
                    "max_metrics": dict(max_metrics),
                }
            }
            self._emit_diagnostic_event(
                str(
                    state.get("first_summary_level")
                    if summary_emissions == 0
                    else state.get("summary_level")
                )
                or "WARN",
                summary,
                cycle,
                raw,
            )
            state["audit_emissions"] = audit_emissions + 1
            state["summary_emissions"] = summary_emissions + 1
            state["last_summary_monotonic"] = now
        return state

    def _clear_audit_condition(
        self,
        key: str,
        *,
        cycle: Optional[CycleState] = None,
        recovery_message: str = "",
        emit_recovery: bool = True,
        context: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        state = self._audit_conditions.pop(str(key or ""), None)
        if state is None:
            return None
        audit_emissions = int(state.get("audit_emissions", 0) or 0)
        if not emit_recovery or audit_emissions <= 0:
            return state
        now = time.monotonic()
        elapsed = max(0.0, now - float(state.get("first_seen_monotonic", now)))
        occurrences = int(state.get("occurrence_count", 0) or 0)
        immediate_emissions = int(state.get("immediate_emissions", 0) or 0)
        suppressed = max(0, occurrences - audit_emissions)
        suffix = f" {recovery_message.strip()}" if recovery_message.strip() else ""
        message = (
            f"{state.get('family') or 'Diagnostic condition'} recovered after "
            f"{self._format_diagnostic_duration(elapsed)}; {suppressed} repeated observations "
            f"were suppressed.{suffix}"
        )
        raw = {
            "diagnostic_condition_recovery": {
                "key": state.get("key"),
                "kind": state.get("kind"),
                "family": state.get("family"),
                "first_seen_at": state.get("first_seen_at"),
                "recovered_at": utc_now_iso(),
                "duration_seconds": elapsed,
                "occurrence_count": occurrences,
                "suppressed_count": suppressed,
                "immediate_emissions": immediate_emissions,
                "reason_counts": dict(state.get("reason_counts") or {}),
                "max_metrics": dict(state.get("max_metrics") or {}),
                "context": dict(context or {}),
            }
        }
        self._emit_diagnostic_event("INFO", message, cycle, raw)
        return state

    def _audit_condition_snapshot(self, key: str) -> dict[str, Any]:
        state = self._audit_conditions.get(str(key or ""))
        if state is None:
            return {}
        now = time.monotonic()
        occurrences = int(state.get("occurrence_count", 0) or 0)
        audit_emissions = int(state.get("audit_emissions", 0) or 0)
        return {
            "active": True,
            "key": state.get("key"),
            "kind": state.get("kind"),
            "family": state.get("family"),
            "reason_code": state.get("latest_reason_code"),
            "message": state.get("latest_message"),
            "first_seen_at": state.get("first_seen_at"),
            "last_seen_at": state.get("last_seen_at"),
            "duration_seconds": max(
                0.0,
                now - float(state.get("first_seen_monotonic", now)),
            ),
            "observations": occurrences,
            "suppressed_observations": max(0, occurrences - audit_emissions),
            "reason_counts": dict(state.get("reason_counts") or {}),
            "latest_context": dict(state.get("latest_context") or {}),
            "max_metrics": dict(state.get("max_metrics") or {}),
        }

    def _mark_recovery_required(self, cycle: CycleState, message: str) -> None:
        """Move an unclear state into a formal recovery-required/manual-review mode."""
        cycle.stage = Stage.MANUAL_REVIEW
        cycle.error_message = "RECOVERY REQUIRED: " + message
        try:
            cycle.recovery_required = True
        except Exception:
            pass
        cycle.touch()
        self._recovery_required = True
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        self.storage.add_decision_event(
            event_type="RECOVERY_REQUIRED",
            message=cycle.error_message,
            cycle=cycle,
            stage_before=None,
            stage_after=cycle.stage.value,
            decision_result="manual_review",
        )
        self._log("WARN", cycle.error_message, cycle)

    @staticmethod
    def _safe_parse_utc_iso(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    def _rth_status_age_seconds(self) -> Optional[float]:
        status = self._latest_rth_status or {}
        parsed = self._safe_parse_utc_iso(status.get("checked_at"))
        if parsed is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())

    def _session_minutes_from_rth_status(self, now_utc: Optional[datetime] = None) -> dict[str, Any]:
        """Return session timing from the adapter's contract-hours result.

        The production adapter stores the current contract's regular-session
        boundaries parsed from IBKR ``liquidHours``.  When IBKR metadata is
        unavailable, the adapter's existing conservative fallback supplies the
        boundaries.  Keeping the calculation here limited to those returned
        boundaries prevents the first/last-minute and cancel-before-close guards
        from silently assuming a normal 16:00 close on an early-close day.
        """
        status = self._latest_rth_status or {}
        open_dt = self._safe_parse_utc_iso(status.get("session_open"))
        close_dt = self._safe_parse_utc_iso(status.get("session_close"))
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        source = str(status.get("source") or "rth_status")
        if open_dt is None or close_dt is None or close_dt <= open_dt:
            return {
                "available": False,
                "minutes_since_open": None,
                "minutes_to_close": None,
                "local_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "session_open_display": "",
                "session_close_display": "",
                "source": source,
                "message": str(status.get("message") or "Regular-session boundaries are unavailable."),
            }

        zone_name = str(status.get("time_zone") or "").strip()
        try:
            display_zone = ZoneInfo(zone_name) if zone_name else timezone.utc
        except Exception:
            display_zone = timezone.utc
        local_now = now.astimezone(display_zone)
        local_open = open_dt.astimezone(display_zone)
        local_close = close_dt.astimezone(display_zone)
        return {
            "available": True,
            "minutes_since_open": (now - open_dt).total_seconds() / 60.0,
            "minutes_to_close": (close_dt - now).total_seconds() / 60.0,
            "local_time": local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "session_open_display": local_open.strftime("%H:%M %Z"),
            "session_close_display": local_close.strftime("%H:%M %Z"),
            "source": source,
            "message": str(status.get("message") or ""),
        }

    def _session_timing_guard_message_for_buy(self, cycle: CycleState) -> Optional[str]:
        if not bool(getattr(cycle, "session_timing_guard_enabled", False)):
            return None
        timing = self._session_minutes_from_rth_status()
        if not timing.get("available"):
            detail = str(timing.get("message") or "IBKR regular-session boundaries are unavailable.")
            return f"Session timing guard blocked BUY: could not determine the contract's regular-session open/close from RTH status ({detail})."
        since_open = timing.get("minutes_since_open")
        to_close = timing.get("minutes_to_close")
        if since_open is None or to_close is None:
            return "Session timing guard blocked BUY: could not determine minutes from open/close."
        first = int(getattr(cycle, "no_new_buy_first_minutes", 0) or 0)
        last = int(getattr(cycle, "no_new_buy_last_minutes", 0) or 0)
        if first > 0 and 0 <= since_open < first:
            open_text = str(timing.get("session_open_display") or "session open")
            return f"Session timing guard blocked BUY: within first {first} minutes after the {open_text} regular-session open ({timing.get('local_time')})."
        if last > 0 and 0 <= to_close < last:
            close_text = str(timing.get("session_close_display") or "session close")
            return f"Session timing guard blocked BUY: within last {last} minutes before the {close_text} regular-session close ({timing.get('local_time')})."
        return None

    def _stale_data_guard_message_for_buy(self, cycle: CycleState) -> Optional[str]:
        if not bool(getattr(cycle, "stale_data_guard_enabled", False)):
            return None
        snapshot = self.price_snapshot or {}
        if self._api_data_invalidated or bool(snapshot.get("api_data_invalidated")):
            reason = str(snapshot.get("api_data_invalidated_reason") or self._api_data_invalidated_reason or "freshness is invalidated")
            return f"Stale-data guard blocked BUY: {reason}"
        now = time.monotonic()
        # Use the controller's monotonic receive timestamp instead of relying
        # on the static age value stored inside the last snapshot. Without this,
        # a stale-data guard could keep seeing the age value from the moment the
        # snapshot was recorded and pass incorrectly during a prolonged stall.
        if self._api_last_data_monotonic > 0:
            age = max(0.0, now - self._api_last_data_monotonic)
        else:
            age = snapshot.get("api_data_age_seconds")
        max_price_age = float(getattr(cycle, "max_selected_price_age_seconds", 3.0) or 3.0)
        if snapshot.get("price") is None:
            return "Stale-data guard blocked BUY: selected strategy price is missing."
        if not isinstance(age, (int, float)) or float(age) > max_price_age:
            return f"Stale-data guard blocked BUY: selected/API price age is not fresh enough (max {max_price_age:.1f}s)."
        max_ba_age = float(getattr(cycle, "max_bid_ask_age_seconds", 3.0) or 3.0)
        bid = self._snapshot_field(snapshot, "bid", "delayedBid")
        ask = self._snapshot_field(snapshot, "ask", "delayedAsk")
        if bid is None or ask is None or float(age) > max_ba_age:
            return f"Stale-data guard blocked BUY: bid/ask data is missing or older than {max_ba_age:.1f}s."
        max_rth_age = float(getattr(cycle, "max_rth_status_age_seconds", 60.0) or 60.0)
        rth_age = self._rth_status_age_seconds()
        if rth_age is None or rth_age > max_rth_age:
            return f"Stale-data guard blocked BUY: RTH status is missing or older than {max_rth_age:.1f}s."
        return None

    def _volatility_guard_message_for_buy(self, cycle: CycleState) -> Optional[str]:
        if not bool(getattr(cycle, "volatility_filter_enabled", False)):
            return None
        window = int(getattr(cycle, "volatility_window_seconds", 300) or 300)
        max_move = float(getattr(cycle, "max_recent_price_move_pct", 5.0) or 5.0)
        now = time.monotonic()
        values = [price for ts, price in self._price_history if now - ts <= window and price > 0]
        if len(values) < 3:
            return None
        lo = min(values)
        hi = max(values)
        if lo <= 0:
            return None
        move = ((hi / lo) - 1.0) * 100.0
        if move > max_move:
            return f"Volatility guard blocked BUY: recent {window}s price range {move:.2f}% exceeds max {max_move:.2f}%."
        return None

    def _buy_partial_fill_elapsed_seconds(
        self,
        cycle: CycleState,
        *,
        now_utc: Optional[datetime] = None,
    ) -> float:
        """Return elapsed time since the first positive BUY fill.

        ``buy_filled_at`` already survives reconnects, watchdog replacement,
        and application restarts. Reusing it keeps the timeout deterministic
        without adding another database column. A missing legacy timestamp
        starts a new grace period rather than cancelling from an unknown age.
        """
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        started = self._safe_parse_utc_iso(cycle.buy_filled_at)
        if started is None or started > now:
            # A missing/malformed legacy value or a wall-clock rollback must
            # not leave a marketable remainder working indefinitely. Start a
            # fresh bounded grace period from the current observation instead.
            cycle.buy_filled_at = now.isoformat()
            return 0.0
        return max(0.0, (now - started).total_seconds())

    def _buy_partial_market_session_safety_reason(
        self,
        cycle: CycleState,
    ) -> Optional[tuple[str, str]]:
        """Return a configured safety reason for cancelling a BUY remainder.

        Portfolio limits and what-if checks are intentionally excluded: those
        decide whether a new order may be submitted. After a positive fill,
        only market-data and session conditions that make the still-working
        marketable remainder unsafe are reconsidered.
        """
        if bool(getattr(cycle, "rth_only", True)):
            # Refresh through the adapter on every partial-fill safety check.
            # The adapter keeps its own bounded cache and closes cached windows
            # at the exact session boundary, so an old GUI snapshot cannot keep
            # a remainder working after RTH has ended.
            rth = self._update_rth_status(self.contract)
            if not bool(rth.get("is_open", False)):
                detail = str(
                    rth.get("message")
                    or rth.get("source")
                    or "regular trading hours are closed"
                )
                return (
                    "rth_closed",
                    f"RTH safety changed while the BUY was filling: {detail}",
                )

        delayed = self._delayed_live_data_blocker_for_buy(cycle)
        if delayed is not None:
            return (
                str(delayed.get("code") or "non_live_data"),
                str(
                    delayed.get("message")
                    or "Live market-data mode is no longer confirmed."
                ),
            )

        stale_message = self._stale_data_guard_message_for_buy(cycle)
        if stale_message:
            return "stale_data", stale_message

        if bool(getattr(cycle, "session_timing_guard_enabled", False)):
            cutoff = int(
                getattr(cycle, "cancel_buy_before_close_minutes", 0) or 0
            )
            if cutoff > 0:
                timing = self._session_minutes_from_rth_status()
                if not timing.get("available"):
                    detail = str(
                        timing.get("message")
                        or "regular-session boundaries are unavailable"
                    )
                    return (
                        "session_timing_unavailable",
                        "Session timing safety changed while the BUY was filling: "
                        f"{detail}",
                    )
                minutes_to_close = timing.get("minutes_to_close")
                if (
                    minutes_to_close is not None
                    and 0 <= float(minutes_to_close) <= cutoff
                ):
                    close_text = str(
                        timing.get("session_close_display")
                        or "the regular-session close"
                    )
                    return (
                        "session_close",
                        "Session timing safety changed while the BUY was filling: "
                        f"{float(minutes_to_close):.1f} minutes remain before "
                        f"{close_text} (configured cutoff {cutoff} minutes).",
                    )

        volatility_message = self._volatility_guard_message_for_buy(cycle)
        if volatility_message:
            return "volatility", volatility_message

        hard_limits_enabled = bool(
            getattr(cycle, "hard_risk_limits_enabled", False)
        )
        snapshot = self.price_snapshot or {}
        if hard_limits_enabled:
            current_price = self._positive_float(
                snapshot.get("strategy_price")
                or snapshot.get("price")
                or cycle.last_price
            )
            min_trade_price = float(
                getattr(cycle, "min_trade_price", 0.0) or 0.0
            )
            if (
                min_trade_price > 0
                and current_price is not None
                and current_price < min_trade_price
            ):
                return (
                    "min_trade_price",
                    "BUY remainder safety changed: current price "
                    f"{current_price:.4f} is below the configured minimum "
                    f"trade price {min_trade_price:.4f}.",
                )

            max_spread = float(
                getattr(cycle, "max_spread_pct", 0.0) or 0.0
            )
            if max_spread > 0:
                bid = self._positive_float(
                    self._snapshot_field(snapshot, "bid", "delayedBid")
                )
                ask = self._positive_float(
                    self._snapshot_field(snapshot, "ask", "delayedAsk")
                )
                if bid is None or ask is None or ask < bid:
                    return (
                        "spread_unverified",
                        "BUY remainder safety changed: the current bid/ask "
                        "spread cannot be verified.",
                    )
                midpoint = (bid + ask) / 2.0
                spread_pct = (
                    ((ask - bid) / midpoint) * 100.0
                    if midpoint > 0
                    else float("inf")
                )
                if spread_pct > max_spread:
                    return (
                        "spread",
                        "BUY remainder safety changed: spread "
                        f"{spread_pct:.2f}% exceeds max {max_spread:.2f}%.",
                    )

            max_gap = float(
                getattr(cycle, "max_gap_from_prev_close_pct", 0.0) or 0.0
            )
            if max_gap > 0:
                close = self._positive_float(
                    self._snapshot_field(snapshot, "close", "delayedClose")
                )
                market_price = self._positive_float(
                    self._snapshot_field(
                        snapshot,
                        "marketPrice",
                        "bidAskMidpoint",
                        "delayedBidAskMidpoint",
                        "last",
                        "delayedLast",
                    )
                )
                if close is None or market_price is None:
                    return (
                        "gap_unverified",
                        "BUY remainder safety changed: the current gap from "
                        "the previous close cannot be verified.",
                    )
                gap_pct = abs((market_price / close) - 1.0) * 100.0
                if gap_pct > max_gap:
                    return (
                        "gap",
                        "BUY remainder safety changed: current gap from close "
                        f"{gap_pct:.2f}% exceeds max {max_gap:.2f}%.",
                    )
        return None

    def _buy_partial_remainder_cancel_action(
        self,
        cycle: CycleState,
        polled: PolledOrderState,
        cumulative_qty: int,
        *,
        now_utc: Optional[datetime] = None,
    ) -> tuple[Optional[StrategyAction], Optional[dict[str, Any]]]:
        """Build a delayed or safety-triggered remainder cancellation action."""
        status = str(polled.status or "").strip()
        terminal = {
            "Filled",
            "Cancelled",
            "ApiCancelled",
            "Inactive",
            "Rejected",
        }
        if status in terminal:
            return None, None
        if bool(getattr(cycle, "buy_remainder_cancel_requested", False)):
            return None, None
        if status in {"CancelRequested", "PendingCancel"} or str(
            cycle.buy_status or ""
        ).strip() in {"CancelRequested", "PendingCancel"}:
            return None, None

        target_qty = max(0, int(cycle.quantity or 0))
        expected_remaining = max(0, target_qty - max(0, int(cumulative_qty)))
        reported_remaining = max(0, int(polled.remaining or 0))
        remaining_qty = max(expected_remaining, reported_remaining)
        if cumulative_qty <= 0 or remaining_qty <= 0 or not cycle.buy_order_ref:
            return None, None

        elapsed = self._buy_partial_fill_elapsed_seconds(
            cycle,
            now_utc=now_utc,
        )
        grace = max(0.1, float(self.BUY_PARTIAL_FILL_GRACE_SECONDS))
        safety = self._buy_partial_market_session_safety_reason(cycle)
        if safety is not None:
            code, detail = safety
            trigger = "safety"
            reason = (
                f"Partial BUY fill is {cumulative_qty} of {target_qty}; "
                f"cancelling the {remaining_qty}-share remainder immediately "
                "because a market/session safety condition changed: "
                f"{detail}"
            )
        elif elapsed >= grace:
            code = "partial_fill_timeout"
            detail = (
                "The triggered marketable BUY did not reach a terminal status "
                "within the grace period."
            )
            trigger = "timeout"
            reason = (
                f"Partial BUY fill is {cumulative_qty} of {target_qty}; the "
                f"marketable order remained nonterminal for {elapsed:.2f}s "
                "after the first fill, so BouncyBot is cancelling the "
                f"{remaining_qty}-share remainder after the {grace:.1f}s "
                "grace period."
            )
        else:
            return None, {
                "trigger": "grace",
                "code": "partial_fill_grace",
                "detail": (
                    "Waiting briefly for the triggered marketable BUY to "
                    "finish normally."
                ),
                "elapsed_seconds": elapsed,
                "grace_seconds": grace,
                "filled_quantity": cumulative_qty,
                "remaining_quantity": remaining_qty,
            }

        decision = {
            "trigger": trigger,
            "code": code,
            "detail": detail,
            "elapsed_seconds": elapsed,
            "grace_seconds": grace,
            "filled_quantity": cumulative_qty,
            "remaining_quantity": remaining_qty,
        }
        action = StrategyAction(
            "CANCEL_ORDER",
            {
                "order_ref": cycle.buy_order_ref,
                "order_id": cycle.buy_order_id or polled.order_id,
                "role": "buy_remainder",
                "reason": reason,
                "cancel_decision": dict(decision),
            },
        )
        return action, decision

    def _cancel_buy_before_close_if_needed(self, cycle: CycleState) -> None:
        if cycle.stage != Stage.BUY_TRAIL_ACTIVE or not cycle.buy_order_ref:
            return
        if not bool(getattr(cycle, "session_timing_guard_enabled", False)):
            return
        cutoff = int(getattr(cycle, "cancel_buy_before_close_minutes", 0) or 0)
        if cutoff <= 0:
            return
        timing = self._session_minutes_from_rth_status()
        if not timing.get("available"):
            return
        minutes_to_close = timing.get("minutes_to_close")
        if minutes_to_close is None or not (0 <= float(minutes_to_close) <= cutoff):
            return
        if bool(getattr(cycle, "buy_remainder_cancel_requested", False)) or str(
            cycle.buy_status or ""
        ) in {"CancelRequested", "PendingCancel"}:
            return
        if self._broker_mutation_blocked_by_storage_fault(
            "Session-timing BUY cancellation",
            cycle,
        ):
            return
        try:
            self.adapter.cancel_order(cycle.buy_order_ref, cycle.buy_order_id)
            cycle.buy_status = "CancelRequested"
            cycle.buy_remainder_cancel_requested = True
            close_text = str(timing.get("session_close_display") or "the regular-session close")
            cycle.error_message = f"Session timing guard: cancelled active BUY trail {float(minutes_to_close):.1f} minutes before {close_text}."
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.update_order_status(cycle.buy_order_ref, "CancelRequested", cycle.buy_order_id, cycle.buy_perm_id)
            self._log("WARN", cycle.error_message, cycle)
        except BrokerAdapterError as exc:
            self._handle_broker_connection_problem(exc)
            self._log("WARN", f"Session timing guard wanted to cancel BUY before close but TWS was unavailable: {exc}", cycle)

    @staticmethod
    def _is_close_before_rth_market_order_ref(order_ref: Optional[str]) -> bool:
        return str(order_ref or "").endswith("|RTH_CLOSE_SELL_MARKET")

    @staticmethod
    def _is_stage4_trailing_sell_ref(order_ref: Optional[str]) -> bool:
        return str(order_ref or "").endswith("|SELL_TRAIL")

    @staticmethod
    def _close_before_rth_order_is_working(order_ref: Optional[str], status: Optional[str]) -> bool:
        if not order_ref:
            return False
        terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        return str(status or "").strip() not in terminal

    def _set_close_before_rth_wait_message(self, cycle: CycleState, message: str) -> None:
        changed = cycle.error_message != message
        if changed:
            cycle.error_message = message
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
        lowered = str(message or "").lower()
        if "cancel" in lowered:
            reason_code = "WAITING_FOR_CANCEL_CONFIRMATION"
        elif "working" in lowered or "fill" in lowered:
            reason_code = "WAITING_FOR_ORDER_SETTLEMENT"
        else:
            reason_code = "WAITING_FOR_CLOSE_WORKFLOW"
        self._update_audit_condition(
            key=f"close_before_rth_wait|{cycle.id}",
            family="Close-before-RTH workflow",
            reason_code=reason_code,
            message=message,
            cycle=cycle,
            initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
            repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
            summary_level="WARN",
            immediate_level="WARN",
            immediate_message=message if changed else None,
            immediate_token=reason_code,
            context={
                "close_before_rth_liquidation_requested": bool(
                    getattr(cycle, "close_before_rth_liquidation_requested", False)
                ),
                "close_before_rth_cancel_requested": bool(
                    getattr(cycle, "close_before_rth_cancel_requested", False)
                ),
            },
            kind="close_before_rth_wait",
        )

    def _liquidate_profitable_stage3_before_close_if_needed(
        self,
        cycle: CycleState,
        current_price: float,
    ) -> bool:
        """Start the Stage-3 close policy only for a profitable executable bid."""
        if cycle.stage != Stage.WAIT_RISE_TRIGGER:
            return False
        if not bool(getattr(cycle, "cancel_sell_and_liquidate_before_close_enabled", False)):
            return False
        cutoff = int(getattr(cycle, "liquidate_before_close_minutes", 0) or 0)
        if cutoff <= 0:
            return False
        timing = self._session_minutes_from_rth_status()
        minutes_raw = timing.get("minutes_to_close") if timing.get("available") else None
        if minutes_raw is None:
            self._update_audit_condition(
                key=f"stage3_close_boundary|{cycle.id}",
                family="Stage 3 close-before-RTH timing guard",
                reason_code="SESSION_BOUNDARY_UNAVAILABLE",
                message=(
                    "The regular-session boundary is unavailable. No market SELL will be "
                    "submitted without a confirmed cutoff."
                ),
                cycle=cycle,
                initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                summary_level="WARN",
                immediate_level="WARN",
                immediate_message=(
                    "Stage 3 close-before-RTH cannot verify the regular-session boundary; "
                    "no market SELL will be submitted without a confirmed cutoff."
                ),
                immediate_token="session_boundary_unavailable",
                context={"timing": dict(timing)},
                kind="session_timing_guard",
            )
            return False
        self._clear_audit_condition(
            f"stage3_close_boundary|{cycle.id}",
            cycle=cycle,
            recovery_message="The regular-session boundary is available again.",
            context={"timing": dict(timing)},
        )
        minutes_to_close = float(minutes_raw)
        rth_open = bool((self._latest_rth_status or {}).get("is_open"))
        if not rth_open or not (0 < minutes_to_close <= cutoff):
            self._clear_audit_condition(
                f"stage3_close_not_profitable|{cycle.id}",
                cycle=cycle,
                recovery_message="The cycle is outside the configured pre-close liquidation window.",
            )
            return False

        avg_buy = float(cycle.avg_buy_price or 0.0)
        executable_bid = float(current_price or 0.0)
        if avg_buy <= 0 or executable_bid <= avg_buy:
            self._update_audit_condition(
                key=f"stage3_close_not_profitable|{cycle.id}",
                family="Stage 3 close-before-RTH profitability guard",
                reason_code="EXECUTABLE_BID_NOT_ABOVE_BUY",
                message=(
                    "Liquidation was not started because the executable SELL bid "
                    f"({executable_bid:.4f}) is not strictly above the average BUY price "
                    f"({avg_buy:.4f}). Commissions are intentionally ignored for this comparison."
                ),
                cycle=cycle,
                initial_delay_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                repeat_interval_seconds=self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS,
                summary_level="INFO",
                immediate_level="INFO",
                immediate_message=(
                    "Stage 3 close-before-RTH is waiting because the executable bid is not "
                    "above the average BUY price."
                ),
                immediate_token="not_profitable",
                context={
                    "executable_bid": executable_bid,
                    "average_buy_price": avg_buy,
                    "minutes_to_close": minutes_to_close,
                },
                kind="session_timing_guard",
            )
            return False
        self._clear_audit_condition(
            f"stage3_close_not_profitable|{cycle.id}",
            cycle=cycle,
            recovery_message="The executable bid is now above the average BUY price.",
            context={
                "executable_bid": executable_bid,
                "average_buy_price": avg_buy,
                "minutes_to_close": minutes_to_close,
            },
        )

        if not bool(getattr(cycle, "close_before_rth_liquidation_requested", False)):
            cycle.close_before_rth_liquidation_requested = True
            cycle.close_before_rth_cancel_requested = False
            cycle.error_message = (
                f"Stage-3 close-before-RTH liquidation started at executable bid {executable_bid:.4f}, "
                f"above average BUY {avg_buy:.4f}, with {minutes_to_close:.1f} minutes to close."
            )
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.add_decision_event(
                event_type="RTH_CLOSE_STAGE3_PROFITABLE_LIQUIDATION_REQUESTED",
                message=cycle.error_message,
                cycle=cycle,
                stage_before=cycle.stage.value,
                stage_after=cycle.stage.value,
                decision_result="gross_profit_condition_passed",
                raw={
                    # Retain selected_price for backward-compatible audit
                    # readers while making the executable-side meaning explicit.
                    "selected_price": executable_bid,
                    "executable_bid": executable_bid,
                    "price_role": "sell_executable_bid",
                    "average_buy_price": avg_buy,
                    "commissions_ignored": True,
                    "minutes_to_close": minutes_to_close,
                    "configured_minutes": cutoff,
                },
            )
            self._log("WARN", cycle.error_message, cycle)

        protective_working = self._close_before_rth_order_is_working(
            cycle.protective_sell_order_ref,
            cycle.protective_sell_status,
        )
        if protective_working:
            if bool(getattr(cycle, "close_before_rth_cancel_requested", False)):
                return True
            if self._broker_mutation_blocked_by_storage_fault(
                "Protective SELL cancellation before RTH close",
                cycle,
            ):
                return True
            try:
                self.adapter.cancel_order(cycle.protective_sell_order_ref or "", cycle.protective_sell_order_id)
                cycle.protective_sell_status = "CancelRequested"
                cycle.protective_sell_cancel_requested = True
                cycle.close_before_rth_cancel_requested = True
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
                self.storage.update_order_status(
                    cycle.protective_sell_order_ref or "",
                    "CancelRequested",
                    cycle.protective_sell_order_id,
                    cycle.protective_sell_perm_id,
                )
                self._log(
                    "WARN",
                    "Stage-3 close-before-RTH liquidation requested cancellation of the protective SELL "
                    "and will not submit a market SELL until IBKR confirms a terminal status.",
                    cycle,
                )
            except BrokerAdapterError as exc:
                self._handle_broker_connection_problem(exc)
                self._log("WARN", f"Could not confirm protective SELL cancellation before close: {exc}", cycle)
            except Exception as exc:
                self._log("WARN", f"Could not request protective SELL cancellation before close: {exc}", cycle)
            return True

        self._submit_close_before_rth_market_sell(cycle)
        return True

    def _cancel_sell_and_liquidate_before_close_if_needed(self, cycle: CycleState) -> None:
        """Supervise the optional Stage-4 cancel-confirm-market-close workflow."""
        if cycle.stage != Stage.SELL_TRAIL_ACTIVE or not cycle.sell_order_ref:
            return
        requested = bool(getattr(cycle, "close_before_rth_liquidation_requested", False))
        enabled = bool(getattr(cycle, "cancel_sell_and_liquidate_before_close_enabled", False))
        if not enabled and not requested:
            return

        cutoff = int(getattr(cycle, "liquidate_before_close_minutes", 0) or 0)
        if cutoff <= 0:
            return
        timing = self._session_minutes_from_rth_status()
        if not timing.get("available"):
            detail = str(timing.get("message") or "regular-session boundaries are unavailable")
            if requested:
                self._set_close_before_rth_wait_message(
                    cycle,
                    "Close-before-RTH liquidation is pending, but the contract's regular-session boundary "
                    f"cannot be verified ({detail}). No replacement SELL will be submitted without a confirmed open RTH session.",
                )
            else:
                self._update_audit_condition(
                    key=f"stage4_close_boundary|{cycle.id}",
                    family="Stage 4 close-before-RTH timing guard",
                    reason_code="SESSION_BOUNDARY_UNAVAILABLE",
                    message=(
                        "The contract's regular-session boundary cannot be verified "
                        f"({detail}). Automatic liquidation will not start without a "
                        "confirmed boundary."
                    ),
                    cycle=cycle,
                    initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                    repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                    summary_level="WARN",
                    immediate_level="WARN",
                    immediate_message=(
                        "Stage 4 close-before-RTH cannot verify the regular-session "
                        "boundary; automatic liquidation will not start."
                    ),
                    immediate_token="session_boundary_unavailable",
                    context={"timing": dict(timing)},
                    kind="stage4_session_timing_guard",
                )
            return
        self._clear_audit_condition(
            f"stage4_close_boundary|{cycle.id}",
            cycle=cycle,
            recovery_message="The regular-session boundary is available again.",
            context={"timing": dict(timing)},
        )
        minutes_raw = timing.get("minutes_to_close")
        if minutes_raw is None:
            return
        minutes_to_close = float(minutes_raw)
        rth_open = bool((self._latest_rth_status or {}).get("is_open"))
        close_text = str(timing.get("session_close_display") or "the regular-session close")
        replacement = self._is_close_before_rth_market_order_ref(cycle.sell_order_ref)

        if replacement:
            if not requested or (rth_open and minutes_to_close > 0):
                return
            if self._close_before_rth_order_is_working(cycle.sell_order_ref, cycle.sell_status):
                if not bool(getattr(cycle, "close_before_rth_cancel_requested", False)):
                    if self._broker_mutation_blocked_by_storage_fault(
                        "Close-before-RTH market SELL cancellation",
                        cycle,
                    ):
                        return
                    try:
                        self.adapter.cancel_order(cycle.sell_order_ref, cycle.sell_order_id)
                        cycle.sell_status = "CancelRequested"
                        cycle.close_before_rth_cancel_requested = True
                        cycle.touch()
                        self.active_cycle = cycle
                        self.storage.upsert_cycle(cycle)
                        self.storage.update_order_status(
                            cycle.sell_order_ref,
                            "CancelRequested",
                            cycle.sell_order_id,
                            cycle.sell_perm_id,
                        )
                        self.storage.add_decision_event(
                            event_type="RTH_CLOSE_MARKET_CANCEL_REQUESTED",
                            message=(
                                "The close-before-RTH market SELL was still working at the regular-session boundary; "
                                "cancellation was requested."
                            ),
                            cycle=cycle,
                            decision_result="cancel_requested",
                            broker_order_id=cycle.sell_order_id,
                            perm_id=cycle.sell_perm_id,
                            raw={"minutes_to_close": minutes_to_close, "session_close": close_text},
                        )
                    except BrokerAdapterError as exc:
                        self._handle_broker_connection_problem(exc)
                        self._log("WARN", f"Could not cancel the close-before-RTH market SELL at the session boundary: {exc}", cycle)
                    except Exception as exc:
                        self._log("WARN", f"Could not cancel the close-before-RTH market SELL at the session boundary: {exc}", cycle)
            self._set_close_before_rth_wait_message(
                cycle,
                "Close-before-RTH market SELL was not confirmed filled before the regular-session close. "
                "No outside-RTH replacement will be submitted; waiting for the broker's terminal order status before manual review.",
            )
            return

        # If a cancellation request failed or was rejected and the original
        # trail is still working in a later RTH session, resume ordinary Stage-4
        # monitoring and allow a fresh attempt at that session's cutoff.
        if (
            requested
            and rth_open
            and minutes_to_close > cutoff
            and str(cycle.sell_status or "") != "CancelRequested"
            and self._close_before_rth_order_is_working(cycle.sell_order_ref, cycle.sell_status)
        ):
            cycle.close_before_rth_liquidation_requested = False
            cycle.close_before_rth_cancel_requested = False
            cycle.error_message = None
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log(
                "WARN",
                "The prior close-before-RTH cancellation was not confirmed before the close. "
                "The original SELL trail is still working and normal Stage-4 monitoring has resumed for the new session.",
                cycle,
            )
            return

        if requested and (not rth_open or minutes_to_close <= 0):
            self._set_close_before_rth_wait_message(
                cycle,
                "Close-before-RTH cancellation was not confirmed before the regular-session close. "
                "No second SELL was submitted; the original trailing SELL remains the only app exit order while its broker status is monitored.",
            )
            return

        if not self._is_stage4_trailing_sell_ref(cycle.sell_order_ref):
            return
        if not self._close_before_rth_order_is_working(cycle.sell_order_ref, cycle.sell_status):
            return
        if not rth_open or not (0 < minutes_to_close <= cutoff):
            return

        if not requested:
            cycle.close_before_rth_liquidation_requested = True
            cycle.close_before_rth_cancel_requested = False
            cycle.error_message = (
                f"Close-before-RTH liquidation started {minutes_to_close:.1f} minutes before {close_text}. "
                "Waiting for final SELL-trail cancellation before submitting a DAY market SELL for the remaining app-owned quantity."
            )
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.add_decision_event(
                event_type="RTH_CLOSE_LIQUIDATION_REQUESTED",
                message=cycle.error_message,
                cycle=cycle,
                stage_before=cycle.stage.value,
                stage_after=cycle.stage.value,
                decision_result="cancel_then_liquidate",
                broker_order_id=cycle.sell_order_id,
                perm_id=cycle.sell_perm_id,
                raw={
                    "minutes_to_close": minutes_to_close,
                    "configured_minutes": cutoff,
                    "session_close": close_text,
                    "order_ref": cycle.sell_order_ref,
                },
            )
            self._log("WARN", cycle.error_message, cycle)

        if bool(getattr(cycle, "close_before_rth_cancel_requested", False)):
            return
        if self._broker_mutation_blocked_by_storage_fault(
            "Final SELL-trail cancellation before RTH close",
            cycle,
        ):
            return
        try:
            self.adapter.cancel_order(cycle.sell_order_ref, cycle.sell_order_id)
            cycle.sell_status = "CancelRequested"
            cycle.close_before_rth_cancel_requested = True
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.update_order_status(
                cycle.sell_order_ref,
                "CancelRequested",
                cycle.sell_order_id,
                cycle.sell_perm_id,
            )
            self._log(
                "WARN",
                f"Close-before-RTH liquidation: cancellation requested for final SELL trail {cycle.sell_order_ref}.",
                cycle,
            )
        except BrokerAdapterError as exc:
            self._handle_broker_connection_problem(exc)
            self._log("WARN", f"Close-before-RTH liquidation could not confirm the SELL-trail cancellation request: {exc}", cycle)
        except Exception as exc:
            self._log("WARN", f"Close-before-RTH liquidation could not request SELL-trail cancellation: {exc}", cycle)

    def _what_if_guard_message_for_buy(self, cycle: CycleState, payload: dict[str, Any]) -> Optional[str]:
        if not bool(getattr(cycle, "what_if_check_enabled", False)):
            return None
        order_type = str(payload.get("order_type") or ("MKT" if float(payload.get("trailing_percent") or 0.0) <= 0 else "TRAIL")).upper()
        if order_type == "MKT":
            method = getattr(self.adapter, "what_if_market_order", None)
        else:
            method = getattr(self.adapter, "what_if_trailing_stop", None)
        if not callable(method):
            return f"What-if guard blocked BUY: adapter does not support {order_type} margin what-if checks."
        try:
            if order_type == "MKT":
                result = method(
                    contract=self.contract,
                    action="BUY",
                    quantity=int(payload["quantity"]),
                    order_ref=str(payload["order_ref"]) + "|WHATIF",
                    tif=self.strategy.tif,
                    account=(self.connection.account or cycle.account),
                    outside_rth=False,
                )
            else:
                result = method(
                    contract=self.contract,
                    action="BUY",
                    quantity=int(payload["quantity"]),
                    trailing_percent=float(payload["trailing_percent"]),
                    initial_stop_price=float(payload["initial_stop_price"]),
                    order_ref=str(payload["order_ref"]) + "|WHATIF",
                    tif=self.strategy.tif,
                    account=(self.connection.account or cycle.account),
                    outside_rth=False,
                )
        except Exception as exc:
            return f"What-if guard blocked BUY: IBKR what-if margin check failed: {exc}"
        ok = bool(result.get("ok", False)) if isinstance(result, dict) else bool(result)
        if not ok:
            text = result.get("message", "IBKR what-if margin check did not approve the BUY.") if isinstance(result, dict) else "IBKR what-if margin check did not approve the BUY."
            return f"What-if guard blocked BUY: {text}"
        self.storage.add_decision_event(
            event_type="WHAT_IF_CHECK",
            message="IBKR what-if margin check passed before BUY submission.",
            cycle=cycle,
            decision_result="pass",
            raw=result if isinstance(result, dict) else {"result": bool(result)},
        )
        return None

    def _execute_actions(self, actions: list[StrategyAction], cycle: CycleState) -> None:
        """Execute broker actions returned by the pure strategy engine.

        If a broker submission fails before IBKR confirms an order, the cycle is
        rolled back to the previous waiting stage so the bot does not believe an
        unconfirmed order is active.
        """
        if self._broker_mutation_blocked_by_storage_fault(
            "Strategy broker action",
            cycle,
        ):
            return
        for action in actions:
            if self._broker_mutation_blocked_by_storage_fault(
                f"Strategy action {action.action_type}",
                cycle,
            ):
                return
            try:
                if action.action_type == "PLACE_BUY_TRAIL":
                    self._place_trailing_order(cycle, action, "BUY")
                elif action.action_type == "PLACE_BUY_MARKET":
                    self._place_market_order(cycle, action, "BUY")
                elif action.action_type == "PLACE_PROTECTIVE_SELL_TRAIL":
                    self._place_trailing_order(cycle, action, "SELL", role="PROTECTIVE_SELL")
                elif action.action_type == "PLACE_SELL_TRAIL":
                    self._place_trailing_order(cycle, action, "SELL")
                elif action.action_type == "PLACE_SELL_MARKET":
                    self._place_market_order(cycle, action, "SELL")
                elif action.action_type == "CANCEL_ORDER":
                    try:
                        if self._broker_mutation_blocked_by_storage_fault(
                            "Order cancellation",
                            cycle,
                        ):
                            return
                        self.adapter.cancel_order(action.payload["order_ref"], action.payload.get("order_id"))
                        self.storage.update_order_status(action.payload["order_ref"], "CancelRequested", order_id=action.payload.get("order_id"))
                        if action.payload.get("role") == "protective_sell" and self.active_cycle:
                            self.active_cycle.protective_sell_status = "CancelRequested"
                            self.active_cycle.protective_sell_cancel_requested = True
                            self.active_cycle.touch()
                            self.storage.upsert_cycle(self.active_cycle)
                        elif action.payload.get("role") == "buy_remainder" and self.active_cycle:
                            self.active_cycle.buy_status = "CancelRequested"
                            self.active_cycle.buy_remainder_cancel_requested = True
                            self.active_cycle.touch()
                            self.storage.upsert_cycle(self.active_cycle)
                            decision = dict(action.payload.get("cancel_decision") or {})
                            self.storage.add_decision_event(
                                event_type="BUY_REMAINDER_CANCEL_REQUESTED",
                                message=str(
                                    action.payload.get("reason")
                                    or "BUY remainder cancellation requested."
                                ),
                                cycle=self.active_cycle,
                                stage_before=self.active_cycle.stage.value,
                                stage_after=self.active_cycle.stage.value,
                                decision_result=str(
                                    decision.get("trigger") or "requested"
                                ),
                                broker_order_id=action.payload.get("order_id"),
                                perm_id=self.active_cycle.buy_perm_id,
                                raw={"partial_fill_policy": decision},
                            )
                        self._log("INFO", action.payload.get("reason", "Cancel requested."), cycle)
                    except BrokerAdapterError as exc:
                        if action.payload.get("role") == "buy_remainder" and self.active_cycle:
                            self.active_cycle.buy_remainder_cancel_requested = False
                            self.active_cycle.touch()
                            self.storage.upsert_cycle(self.active_cycle)
                        self._handle_broker_connection_problem(exc)
                        self._log("WARN", f"Cancel request could not be confirmed because TWS connection is unavailable: {exc}", cycle)
                    except Exception as exc:
                        if action.payload.get("role") == "buy_remainder" and self.active_cycle:
                            self.active_cycle.buy_remainder_cancel_requested = False
                            self.active_cycle.touch()
                            self.storage.upsert_cycle(self.active_cycle)
                        self._log("WARN", f"Cancel request could not be confirmed: {exc}", cycle)
            except BrokerAdapterError as exc:
                side = (
                    "BUY" if action.action_type in {"PLACE_BUY_TRAIL", "PLACE_BUY_MARKET"}
                    else "PROTECTIVE_SELL" if action.action_type == "PLACE_PROTECTIVE_SELL_TRAIL"
                    else "SELL" if action.action_type in {"PLACE_SELL_TRAIL", "PLACE_SELL_MARKET"}
                    else "ORDER"
                )
                message = f"{side} order submission failed before confirmation: {exc}"
                if side in {"BUY", "SELL", "PROTECTIVE_SELL"}:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, side, message)
                    self.storage.upsert_cycle(self.active_cycle)
                self._handle_broker_connection_problem(exc)
                self._log("WARN", message, self.active_cycle or cycle)

    def _apply_buy_preflight_block(
        self,
        cycle: CycleState,
        message: str,
        blocker_code: str,
    ) -> None:
        """Persist a local BUY block and audit it at a stable bounded cadence."""
        blocked = StrategyEngine.rollback_preflight_blocked_order(cycle, "BUY", message)
        self.active_cycle = blocked
        self.storage.upsert_cycle(blocked)
        normalized_code = str(blocker_code or "preflight").strip().lower() or "preflight"
        informational_codes = {
            "atr_warmup",
            "rth_closed",
            "session_timing",
            "timing",
        }
        informational = normalized_code in informational_codes
        immediate_level = "INFO" if informational else "WARN"
        self._update_audit_condition(
            key=f"buy_preflight_block|{cycle.id}",
            family="BUY preflight block",
            reason_code=normalized_code.upper(),
            message=message,
            cycle=blocked,
            initial_delay_seconds=(
                self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS
                if informational
                else self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS
            ),
            repeat_interval_seconds=(
                self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS
                if informational
                else self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS
            ),
            summary_level="INFO" if informational else "WARN",
            first_summary_level="INFO" if informational else "WARN",
            immediate_level=immediate_level,
            immediate_message=message,
            context={"blocker_code": normalized_code},
            kind="buy_preflight_block",
        )

    @staticmethod
    def _snapshot_field(snapshot: Optional[dict[str, Any]], *names: str) -> Optional[float]:
        fields = (snapshot or {}).get("fields") or {}
        for name in names:
            value = fields.get(name)
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except Exception:
                pass
        return None

    @staticmethod
    def _positive_float(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except Exception:
            return None
        if number > 0 and isfinite(number):
            return number
        return None

    def _contract_min_tick(self) -> float:
        try:
            value = float(getattr(self.contract, "min_tick", 0.01) or 0.01)
        except Exception:
            value = 0.01
        if value <= 0 or not isfinite(value):
            return 0.01
        return value

    @staticmethod
    def _round_to_increment(price: float, increment: float, direction: str) -> float:
        """Normalize an order price to the contract's minimum price increment.

        Direction is intentionally side-aware:
        - BUY stop prices are rounded up so the stop remains above the reference.
        - SELL stop prices are rounded down so the stop remains below the reference.
        - nearest is used only for display/reference values.
        """
        try:
            p = float(price)
            inc = float(increment)
        except Exception:
            return float(price)
        if p <= 0 or not isfinite(p) or inc <= 0 or not isfinite(inc):
            return p
        units = p / inc
        if direction == "up":
            rounded = ceil(units - 1e-12) * inc
        elif direction == "down":
            rounded = floor(units + 1e-12) * inc
        else:
            rounded = round(units) * inc
        # Use enough decimals for sub-cent increments while avoiding binary
        # artifacts in the value sent to ib_async/TWS.
        decimals = 0
        probe = inc
        while decimals < 8 and abs(round(probe, decimals) - probe) > 1e-12:
            decimals += 1
        decimals = max(decimals, 2)
        return round(max(0.0, rounded), decimals)

    def _normalize_order_price_for_submission(self, price: float, direction: str) -> dict[str, Any]:
        """Normalize one broker-facing price with market-rule support when available."""
        method = getattr(self.adapter, "normalize_order_price", None)
        if callable(method) and self.contract is not None:
            result = method(self.contract, float(price), direction)
            normalized = float(getattr(result, "normalized_price"))
            increment = float(getattr(result, "increment"))
            if normalized <= 0 or not isfinite(normalized):
                raise BrokerAdapterError("IBKR order-price normalization returned an invalid price.")
            if increment <= 0 or not isfinite(increment):
                raise BrokerAdapterError("IBKR order-price normalization returned an invalid increment.")
            return {
                "price": normalized,
                "increment": increment,
                "source": str(getattr(result, "source", "") or "broker"),
                "market_rule_id": getattr(result, "market_rule_id", None),
                "market_rule_exchange": str(getattr(result, "market_rule_exchange", "") or ""),
            }
        increment = self._contract_min_tick()
        return {
            "price": self._round_to_increment(float(price), increment, direction),
            "increment": increment,
            "source": "contract_min_tick",
            "market_rule_id": None,
            "market_rule_exchange": "",
        }

    @staticmethod
    def _order_price_normalization_label(details: dict[str, Any]) -> str:
        increment = float(details.get("increment") or 0.0)
        if str(details.get("source") or "") == "market_rule":
            rule_id = details.get("market_rule_id")
            exchange = str(details.get("market_rule_exchange") or "")
            route = f" for {exchange}" if exchange else ""
            return f"IBKR market rule {rule_id}{route} increment {increment:g}"
        return f"contract tick {increment:g}"

    def _buy_stop_reference_price(self) -> Optional[float]:
        """Return the highest current visible price relevant to a BUY stop.

        A BUY trailing stop should start above the current market.  The app's
        selected display price may be a midpoint/marketPrice, which can be below
        the ask.  Using the max of visible ask/last/market fields avoids sending
        an initial BUY stop that is already at or below the current ask/last.
        """
        snapshot = self.price_snapshot or {}
        fields = snapshot.get("fields") or {}
        candidates = [
            snapshot.get("price"),
            fields.get("ask"),
            fields.get("delayedAsk"),
            fields.get("last"),
            fields.get("delayedLast"),
            fields.get("marketPrice"),
            fields.get("bidAskMidpoint"),
            fields.get("delayedBidAskMidpoint"),
        ]
        cleaned = [value for value in (self._positive_float(item) for item in candidates) if value is not None]
        return max(cleaned) if cleaned else None

    def _sell_stop_reference_price(self) -> Optional[float]:
        """Return the lowest current visible price relevant to a SELL stop."""
        snapshot = self.price_snapshot or {}
        fields = snapshot.get("fields") or {}
        candidates = [
            snapshot.get("price"),
            fields.get("bid"),
            fields.get("delayedBid"),
            fields.get("last"),
            fields.get("delayedLast"),
            fields.get("marketPrice"),
            fields.get("bidAskMidpoint"),
            fields.get("delayedBidAskMidpoint"),
        ]
        cleaned = [value for value in (self._positive_float(item) for item in candidates) if value is not None]
        return min(cleaned) if cleaned else None

    def _normalize_trailing_order_payload(
        self,
        cycle: CycleState,
        payload: dict[str, Any],
        side: str,
        role: str = "",
    ) -> tuple[dict[str, Any], Optional[str]]:
        """Return a payload copy with broker-valid market-rule precision."""
        updated = dict(payload)
        try:
            trail_pct = max(0.0, float(updated.get("trailing_percent") or 0.0))
            stop = float(updated.get("initial_stop_price") or 0.0)
        except Exception:
            return updated, None
        if trail_pct <= 0 or stop <= 0:
            return updated, None
        try:
            message: Optional[str] = None
            if side.upper() == "BUY":
                reference = self._buy_stop_reference_price()
                if reference is not None:
                    stop = max(stop, reference * (1.0 + trail_pct / 100.0))
                stop_details = self._normalize_order_price_for_submission(stop, "up")
                normalized_stop = float(stop_details["price"])
                updated["initial_stop_price"] = normalized_stop
                updated["price_increment"] = float(stop_details["increment"])
                updated["price_increment_source"] = str(stop_details["source"])
                updated["market_rule_id"] = stop_details.get("market_rule_id")
                updated["market_rule_exchange"] = stop_details.get("market_rule_exchange")

                sizing = normalized_stop
                if bool(getattr(cycle, "slippage_buffer_enabled", False)):
                    sizing *= 1.0 + max(
                        0.0,
                        float(getattr(cycle, "slippage_buffer_pct", 0.0) or 0.0),
                    ) / 100.0
                sizing_details = self._normalize_order_price_for_submission(sizing, "up")
                sizing = float(sizing_details["price"])
                updated["sizing_price"] = sizing
                if sizing > 0:
                    updated["quantity"] = max(0, int(floor(float(cycle.budget) / sizing)))
                if float(payload.get("initial_stop_price") or 0.0) != normalized_stop:
                    label = self._order_price_normalization_label(stop_details)
                    message = f"Normalized BUY trailing stop to {label}: {normalized_stop:.8g}."
            else:
                reference = self._sell_stop_reference_price()
                if reference is not None:
                    stop = min(stop, reference * (1.0 - trail_pct / 100.0))
                stop_details = self._normalize_order_price_for_submission(stop, "down")
                normalized_stop = float(stop_details["price"])
                updated["initial_stop_price"] = normalized_stop
                updated["price_increment"] = float(stop_details["increment"])
                updated["price_increment_source"] = str(stop_details["source"])
                updated["market_rule_id"] = stop_details.get("market_rule_id")
                updated["market_rule_exchange"] = stop_details.get("market_rule_exchange")

                minimum_stop: Optional[float]
                try:
                    minimum_stop = float(getattr(cycle, "avg_buy_price", 0.0) or 0.0) * (
                        1.0 + float(getattr(cycle, "rise_trigger_pct", 0.0) or 0.0) / 100.0
                    )
                    if bool(getattr(cycle, "slippage_buffer_enabled", False)):
                        slip = max(
                            0.0,
                            float(getattr(cycle, "slippage_buffer_pct", 0.0) or 0.0),
                        ) / 100.0
                        minimum_stop = minimum_stop / max(1e-12, 1.0 - slip)
                    minimum_stop = float(
                        self._normalize_order_price_for_submission(minimum_stop, "up")["price"]
                    )
                except BrokerAdapterError:
                    raise
                except Exception:
                    minimum_stop = None
                if (
                    minimum_stop is not None
                    and normalized_stop + 1e-9 < minimum_stop
                    and side.upper() == "SELL"
                    and role != "PROTECTIVE_SELL"
                ):
                    return (
                        updated,
                        f"SELL stop {normalized_stop:.8g} no longer protects minimum-profit "
                        f"stop {minimum_stop:.8g}; waiting for a higher price.",
                    )
                if float(payload.get("initial_stop_price") or 0.0) != normalized_stop:
                    label = self._order_price_normalization_label(stop_details)
                    message = f"Normalized SELL trailing stop to {label}: {normalized_stop:.8g}."
            return updated, message
        except BrokerAdapterError as exc:
            return updated, f"Order-price validation blocked {side.upper()} submission: {exc}"
        except Exception as exc:
            return updated, f"Order-price validation blocked {side.upper()} submission: {exc}"

    @staticmethod
    def _trading_blocker(side: str, code: str, message: str, short: str) -> dict[str, str]:
        return {
            "side": str(side).upper(),
            "code": str(code),
            "message": str(message),
            "short": str(short),
        }

    def _atr_warmup_guard_blocker_for_buy(self, cycle: CycleState) -> Optional[dict[str, str]]:
        enabled = bool(getattr(cycle, "atr_block_new_buy_until_ready", False)) and bool(
            getattr(cycle, "atr_adaptive_enabled", False)
        )
        if not enabled:
            return None
        snapshot = self.price_snapshot or {}
        atr_state = dict(snapshot.get("atr") or {})
        ready = bool(snapshot.get("atr_ready") or atr_state.get("ready"))
        if ready:
            return None
        bars = snapshot.get("atr_bars_available")
        if bars is None:
            bars = atr_state.get("bars_available")
        required = snapshot.get("atr_bars_required")
        if required is None:
            required = atr_state.get("bars_required")
        bars_display = bars if bars not in (None, "") else 0
        required_display = required if required not in (None, "") else "?"
        reason = str(atr_state.get("reason") or "ATR has not collected enough RTH-only bars yet")
        message = f"{self.ATR_WARMUP_BLOCK_PREFIX} {reason} ({bars_display}/{required_display} bars)."
        return self._trading_blocker("BUY", "atr_warmup", message, f"ATR {bars_display}/{required_display}")

    @classmethod
    def _cycle_paused_for_atr_warmup(cls, cycle: CycleState) -> bool:
        message = str(getattr(cycle, "error_message", "") or "")
        atr_pause_shape = (
            bool(getattr(cycle, "atr_adaptive_enabled", False))
            and bool(getattr(cycle, "atr_block_new_buy_until_ready", False))
            and getattr(cycle, "drop_trigger_price", None) is None
        )
        return cycle.stage == Stage.WAIT_INITIAL_DROP and (
            message.startswith(cls.ATR_WARMUP_BLOCK_PREFIX) or atr_pause_shape
        )

    @staticmethod
    def _retired_account_position_block_message(message: Any) -> bool:
        lowered = str(message or "").lower()
        return "current broker position is already" in lowered and "expected no open long position" in lowered

    def _reset_stage3_sell_confirmation(self, cycle_id: str = "") -> None:
        if not cycle_id or str(self._stage3_sell_confirmation.get("cycle_id") or "") == str(cycle_id):
            self._stage3_sell_confirmation.clear()

    def _snapshot_age_now(
        self,
        snapshot: dict[str, Any],
        received_at_key: str,
        age_key: str,
    ) -> Optional[float]:
        parsed = self._safe_parse_utc_iso(snapshot.get(received_at_key))
        if parsed is not None:
            return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
        try:
            recorded_age = float(snapshot.get(age_key))
        except (TypeError, ValueError):
            return None
        snapshot_time = self._safe_parse_utc_iso(snapshot.get("timestamp"))
        if snapshot_time is None:
            return max(0.0, recorded_age)
        elapsed = max(0.0, (datetime.now(timezone.utc) - snapshot_time).total_seconds())
        return max(0.0, recorded_age + elapsed)

    def _snapshot_field_age_now(
        self,
        snapshot: dict[str, Any],
        field_name: str,
    ) -> Optional[float]:
        """Return a field's current age from its independent update stamp."""
        received_at = dict(
            snapshot.get("field_update_received_at")
            or snapshot.get("field_change_received_at")
            or {}
        )
        parsed = self._safe_parse_utc_iso(received_at.get(field_name))
        if parsed is not None:
            return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
        ages = dict(
            snapshot.get("field_update_age_seconds")
            or snapshot.get("field_change_age_seconds")
            or {}
        )
        try:
            recorded_age = float(ages.get(field_name))
        except (TypeError, ValueError):
            return None
        snapshot_time = self._safe_parse_utc_iso(snapshot.get("timestamp"))
        if snapshot_time is None:
            return max(0.0, recorded_age)
        elapsed = max(0.0, (datetime.now(timezone.utc) - snapshot_time).total_seconds())
        return max(0.0, recorded_age + elapsed)

    def _stage3_sell_quote_evidence(
        self,
        cycle: CycleState,
        *,
        require_latest_event: bool,
        require_trigger: bool = True,
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Return quote evidence that can safely arm a normal Stage-3 exit.

        The production adapter exposes per-field change identity.  This guard
        deliberately rejects a whole-ticker event when the selected price fell
        back to an unchanged Last, requires a recent valid top-of-book pair,
        applies the configured spread ceiling, and confirms the profit trigger
        on the executable SELL side (bid) rather than on a cached convenience
        price.
        """
        snapshot = dict(self.price_snapshot or {})
        if not bool(snapshot.get("market_data_field_tracking")):
            return None, "Field-level market-data freshness is unavailable."
        if require_latest_event and not bool(snapshot.get("api_data_received_in_latest_read")):
            return None, "No distinct market-data update is available for SELL confirmation."
        # Stage 3 is confirmed on the executable bid, not on the adapter's
        # convenience marketPrice. A fresh bid/ask event remains usable here
        # even when marketPrice temporarily resolves to an unchanged cached
        # Last; the generic selected price stays blocked for ATR and all other
        # strategy paths.
        if snapshot.get("upstream_connected") is not True:
            return None, "IBKR upstream connectivity is not confirmed."

        try:
            sequence = int(snapshot.get("market_data_update_sequence") or 0)
        except Exception:
            sequence = 0
        subscription_id = str(snapshot.get("market_data_subscription_id") or "")
        if sequence <= 0 or not subscription_id:
            return None, "Market-data event identity is unavailable."

        fields = dict(snapshot.get("fields") or {})
        basis = str(snapshot.get("selected_price_basis") or snapshot.get("source") or "")
        mode_value = snapshot.get("subscription_market_data_type")
        if mode_value is None:
            mode_value = snapshot.get("selected_market_data_type")
        try:
            delayed_mode = int(mode_value) in {3, 4}
        except Exception:
            delayed_mode = basis.startswith("delayed_")
        if basis.startswith("delayed_"):
            delayed_mode = True

        # ib_async normalizes delayed price ticks into Ticker.bid/ask/last.
        # Older test doubles and audit readers may still expose delayedBid and
        # delayedAsk explicitly, so use those only when either is actually
        # present; otherwise the standard fields carry both live and delayed
        # values and the market-data type supplies the label.
        explicit_delayed_quote = delayed_mode and (
            fields.get("delayedBid") is not None or fields.get("delayedAsk") is not None
        )
        if explicit_delayed_quote:
            bid_name, ask_name = "delayedBid", "delayedAsk"
            quote_sequence_key = "delayed_quote_update_sequence"
            quote_received_at_key = "delayed_quote_update_received_at"
            quote_age_key = "delayed_quote_update_age_seconds"
        else:
            bid_name, ask_name = "bid", "ask"
            quote_sequence_key = "quote_update_sequence"
            quote_received_at_key = "quote_update_received_at"
            quote_age_key = "quote_update_age_seconds"
        quote_kind = "delayed" if delayed_mode else "live"

        bid = self._positive_float(fields.get(bid_name))
        ask = self._positive_float(fields.get(ask_name))
        if bid is None or ask is None:
            return None, f"The current {quote_kind} bid/ask pair is incomplete."
        if ask < bid:
            return None, f"The current {quote_kind} bid/ask pair is crossed."
        try:
            quote_sequence = int(snapshot.get(quote_sequence_key) or 0)
        except Exception:
            quote_sequence = 0
        quote_age = self._snapshot_age_now(snapshot, quote_received_at_key, quote_age_key)
        max_quote_age = max(0.1, float(getattr(cycle, "max_bid_ask_age_seconds", 3.0) or 3.0))
        if quote_sequence <= 0 or quote_age is None:
            return None, f"The current {quote_kind} bid/ask update time is unavailable."
        if require_latest_event:
            updated_fields = set(snapshot.get("fields_updated_in_event") or [])
            if quote_sequence != sequence or not updated_fields.intersection({bid_name, ask_name}):
                return None, (
                    f"The latest market-data event did not update the current {quote_kind} "
                    "bid or ask, so it cannot count as a SELL confirmation."
                )
        if quote_age > max_quote_age:
            return None, (
                f"The current {quote_kind} bid/ask pair is {quote_age:.1f}s old, above the "
                f"configured {max_quote_age:.1f}s maximum."
            )

        # A recent update to one side must not make an old opposite side look
        # fresh.  The executable bid and the ask used to validate the spread
        # each need their own recent change timestamp.
        bid_age = self._snapshot_field_age_now(snapshot, bid_name)
        ask_age = self._snapshot_field_age_now(snapshot, ask_name)
        if bid_age is None or ask_age is None:
            return None, f"Independent {quote_kind} bid/ask field timestamps are unavailable."
        stale_sides = [
            f"bid {bid_age:.1f}s" if bid_age > max_quote_age else "",
            f"ask {ask_age:.1f}s" if ask_age > max_quote_age else "",
        ]
        stale_sides = [item for item in stale_sides if item]
        if stale_sides:
            return None, (
                f"The current {quote_kind} {' and '.join(stale_sides)} exceed the configured "
                f"{max_quote_age:.1f}s maximum."
            )

        midpoint = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / midpoint) * 100.0 if midpoint > 0 else float("inf")
        max_spread = max(0.0, float(getattr(cycle, "max_spread_pct", 0.0) or 0.0))
        if max_spread > 0 and spread_pct > max_spread:
            return None, (
                f"The current {quote_kind} spread {spread_pct:.2f}% exceeds the configured "
                f"maximum {max_spread:.2f}%."
            )

        trigger = float(
            cycle.rise_trigger_price
            or StrategyEngine.recalculate_rise_trigger_price(cycle)
            or 0.0
        )
        try:
            min_tick = max(0.0, float(getattr(self.contract, "min_tick", 0.0) or 0.0))
        except Exception:
            min_tick = 0.0
        trigger_tolerance = max(1e-9, min_tick)
        if require_trigger and trigger <= 0:
            return None, "The minimum-profit rise trigger is unavailable."
        if require_trigger and bid + trigger_tolerance < trigger:
            return None, (
                f"The executable SELL bid {bid:.4f} does not confirm the rise trigger "
                f"{trigger:.4f}."
            )

        field_ages = dict(
            snapshot.get("field_update_age_seconds")
            or snapshot.get("field_change_age_seconds")
            or {}
        )
        field_received_at = dict(
            snapshot.get("field_update_received_at")
            or snapshot.get("field_change_received_at")
            or {}
        )
        last_age = None
        if basis in {"last", "delayed_last"}:
            last_name = "delayedLast" if basis == "delayed_last" else "last"
            parsed = self._safe_parse_utc_iso(field_received_at.get(last_name))
            if parsed is not None:
                last_age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
            else:
                try:
                    last_age = float(field_ages.get(last_name))
                except (TypeError, ValueError):
                    last_age = None

        evidence = {
            "cycle_id": cycle.id,
            "subscription_id": subscription_id,
            "sequence": sequence,
            "quote_sequence": quote_sequence,
            "quote_kind": quote_kind,
            "quote_age_seconds": quote_age,
            "bid_age_seconds": bid_age,
            "ask_age_seconds": ask_age,
            "bid": bid,
            "ask": ask,
            "midpoint": midpoint,
            "spread_pct": spread_pct,
            "max_spread_pct": max_spread,
            "rise_trigger_price": trigger,
            "reference_price": bid,
            "selected_price": self._positive_float(snapshot.get("price")),
            "selected_price_source": str(snapshot.get("source") or ""),
            "selected_price_basis": basis,
            "selected_price_basis_sequence": snapshot.get("selected_price_basis_update_sequence"),
            "selected_price_basis_age_seconds": snapshot.get("selected_price_basis_age_seconds"),
            "selected_price_basis_updated_in_event": snapshot.get("selected_price_basis_updated_in_event"),
            "selected_price_basis_changed_in_update": snapshot.get("selected_price_basis_changed_in_update"),
            "last_field_age_seconds": last_age,
            "fields_updated_in_event": list(snapshot.get("fields_updated_in_event") or []),
            "fields_changed_in_update": list(snapshot.get("fields_changed_in_update") or []),
            "captured_at": utc_now_iso(),
        }
        return evidence, ""

    @staticmethod
    def _stage3_sell_guard_reason_code(message: str) -> str:
        """Map one Stage-3 quote detail to a stable audit/GUI reason code."""
        text = str(message or "").strip().lower()
        if not text:
            return "OK"
        if "field-level market-data freshness is unavailable" in text:
            return "FIELD_TRACKING_UNAVAILABLE"
        if "no distinct market-data update" in text:
            return "NO_DISTINCT_UPDATE"
        if "upstream connectivity is not confirmed" in text:
            return "UPSTREAM_UNCONFIRMED"
        if "market-data event identity is unavailable" in text:
            return "EVENT_IDENTITY_UNAVAILABLE"
        if "bid/ask pair is incomplete" in text:
            return "INCOMPLETE_QUOTE"
        if "bid/ask pair is crossed" in text:
            return "CROSSED_QUOTE"
        if "bid/ask update time is unavailable" in text:
            return "QUOTE_TIME_UNAVAILABLE"
        if "latest market-data event did not update" in text:
            return "NO_PRICE_FIELD_UPDATE"
        if "bid/ask pair is" in text and "old" in text:
            return "STALE_QUOTE"
        if "field timestamps are unavailable" in text:
            return "FIELD_TIMESTAMPS_UNAVAILABLE"
        stale_field_message = "exceed" in text and "configured" in text
        if stale_field_message and "bid" in text and "ask" in text:
            return "STALE_BID_ASK"
        if stale_field_message and "bid" in text:
            return "STALE_BID"
        if stale_field_message and "ask" in text:
            return "STALE_ASK"
        if "spread" in text and "exceeds the configured" in text:
            return "SPREAD_TOO_WIDE"
        if "rise trigger is unavailable" in text:
            return "TRIGGER_UNAVAILABLE"
        if "does not confirm the rise trigger" in text:
            return "TRIGGER_NOT_REACHED"
        return "UNKNOWN_QUOTE_GUARD"

    def _stage3_sell_quote_context(
        self,
        cycle: CycleState,
        evidence: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build display-only context for Stage-3 quote diagnostics."""
        snapshot = dict(self.price_snapshot or {})
        fields = dict(snapshot.get("fields") or {})
        context = dict(evidence or {})
        basis = str(snapshot.get("selected_price_basis") or snapshot.get("source") or "")
        delayed = basis.startswith("delayed_")
        try:
            selected_mode = int(
                snapshot.get("subscription_market_data_type")
                if snapshot.get("subscription_market_data_type") is not None
                else snapshot.get("selected_market_data_type")
            )
        except Exception:
            selected_mode = 0
        delayed = delayed or selected_mode in {3, 4}
        explicit_delayed = delayed and (
            fields.get("delayedBid") is not None or fields.get("delayedAsk") is not None
        )
        bid_name = "delayedBid" if explicit_delayed else "bid"
        ask_name = "delayedAsk" if explicit_delayed else "ask"
        bid = self._positive_float(context.get("bid")) or self._positive_float(fields.get(bid_name))
        ask = self._positive_float(context.get("ask")) or self._positive_float(fields.get(ask_name))
        trigger = self._positive_float(cycle.rise_trigger_price)
        if trigger is None:
            trigger = self._positive_float(StrategyEngine.recalculate_rise_trigger_price(cycle))
        selected_price = self._positive_float(snapshot.get("strategy_price"))
        if selected_price is None:
            selected_price = self._positive_float(snapshot.get("price"))
        reference = bid or selected_price or self._positive_float(cycle.last_price)
        spread_pct: Optional[float] = None
        if bid is not None and ask is not None and ask >= bid:
            midpoint = (bid + ask) / 2.0
            if midpoint > 0:
                spread_pct = ((ask - bid) / midpoint) * 100.0
        distance_pct: Optional[float] = None
        if reference is not None and trigger is not None and trigger > 0:
            distance_pct = ((reference - trigger) / trigger) * 100.0
        near_trigger = bool(
            distance_pct is not None
            and distance_pct >= -abs(float(self.STAGE3_NEAR_TRIGGER_PCT))
        )
        context.update(
            {
                "bid": bid,
                "ask": ask,
                "bid_age_seconds": self._snapshot_field_age_now(snapshot, bid_name),
                "ask_age_seconds": self._snapshot_field_age_now(snapshot, ask_name),
                "spread_pct": spread_pct,
                "selected_price": selected_price,
                "selected_price_source": snapshot.get("strategy_price_source") or snapshot.get("source"),
                "rise_trigger_price": trigger,
                "trigger_price": trigger,
                "reference_price": reference,
                "trigger_distance_pct": distance_pct,
                "near_trigger": near_trigger,
                "confirmation_pending": bool(
                    self._stage3_sell_confirmation
                    and str(self._stage3_sell_confirmation.get("cycle_id") or "") == cycle.id
                ),
            }
        )
        return context

    def _stage3_sell_quote_evidence_detail(
        self,
        cycle: CycleState,
        *,
        require_latest_event: bool,
        require_trigger: bool = True,
    ) -> tuple[Optional[dict[str, Any]], str, str, dict[str, Any]]:
        """Return evidence, detail, stable reason code, and display context."""
        evidence, message = self._stage3_sell_quote_evidence(
            cycle,
            require_latest_event=require_latest_event,
            require_trigger=require_trigger,
        )
        reason_code = "OK" if evidence is not None else self._stage3_sell_guard_reason_code(message)
        context = self._stage3_sell_quote_context(cycle, evidence)
        return evidence, message, reason_code, context

    @staticmethod
    def _stage3_quote_guard_condition_key(cycle: CycleState) -> str:
        return f"stage3_sell_quote_guard|{cycle.id}"

    def _set_stage3_quote_guard_status(
        self,
        cycle: CycleState,
        *,
        state: str,
        reason_code: str,
        message: str,
        context: Optional[dict[str, Any]] = None,
        severity: str = "normal",
    ) -> None:
        condition = self._audit_condition_snapshot(
            self._stage3_quote_guard_condition_key(cycle)
        )
        context_data = dict(context or {})
        observations = int(condition.get("observations", 0) or 0)
        suppressed = int(condition.get("suppressed_observations", 0) or 0)
        trigger_price = context_data.get("trigger_price")
        if trigger_price is None:
            trigger_price = context_data.get("rise_trigger_price")
        self._stage3_quote_guard_status = {
            "active": cycle.stage == Stage.WAIT_RISE_TRIGGER,
            "cycle_id": cycle.id,
            "state": str(state or "waiting"),
            "severity": str(severity or "normal"),
            "reason_code": str(reason_code or ""),
            "message": str(message or ""),
            "bid": context_data.get("bid"),
            "ask": context_data.get("ask"),
            "spread_pct": context_data.get("spread_pct"),
            "bid_age_seconds": context_data.get("bid_age_seconds"),
            "ask_age_seconds": context_data.get("ask_age_seconds"),
            "reference_price": context_data.get("reference_price"),
            "trigger_price": trigger_price,
            "trigger_distance_pct": context_data.get("trigger_distance_pct"),
            "near_trigger": bool(context_data.get("near_trigger")),
            "confirmation_pending": bool(context_data.get("confirmation_pending")),
            "confirmation_count": int(context_data.get("confirmation_count", 0) or 0),
            "confirmation_required": self.STAGE3_SELL_CONFIRMATIONS_REQUIRED,
            "duration_seconds": float(condition.get("duration_seconds", 0.0) or 0.0),
            "occurrence_count": observations,
            "suppressed_count": suppressed,
            "updated_at": utc_now_iso(),
        }

    def _observe_stage3_quote_guard(
        self,
        cycle: CycleState,
        *,
        reason_code: str,
        message: str,
        context: dict[str, Any],
        confirmation_was_pending: bool = False,
    ) -> None:
        """Update Stage-3 GUI state while coalescing routine quote failures."""
        key = self._stage3_quote_guard_condition_key(cycle)
        normalized_reason = str(reason_code or "UNKNOWN_QUOTE_GUARD").upper()

        if normalized_reason == "TRIGGER_NOT_REACHED":
            # This outcome proves that a complete fresh quote is available; the
            # executable bid is simply below the profit trigger. It is normal
            # Stage-3 waiting, so resolve any prior quote-evidence condition.
            self._clear_audit_condition(
                key,
                cycle=cycle,
                recovery_message="Fresh quote supervision is available again.",
                context=context,
            )
            self._set_stage3_quote_guard_status(
                cycle,
                state="waiting_below_trigger",
                reason_code=normalized_reason,
                message=message,
                context=context,
                severity="normal",
            )
            return

        if normalized_reason in {"NO_DISTINCT_UPDATE", "NO_PRICE_FIELD_UPDATE"}:
            # Cached reads and non-price ticker events are expected between real
            # quote updates. Do not create an audit row and do not clear a
            # persistent stale/incomplete-quote condition merely because this
            # read cannot prove recovery.
            existing = self._audit_conditions.get(key)
            if confirmation_was_pending:
                condition = self._update_audit_condition(
                    key=key,
                    family="Stage-3 SELL quote evidence",
                    reason_code=normalized_reason,
                    message=message,
                    cycle=cycle,
                    initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
                    repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                    summary_level="WARN",
                    immediate_level="WARN",
                    immediate_message=(
                        "Stage-3 SELL confirmation was invalidated before the second "
                        f"qualifying quote: {message}"
                    ),
                    immediate_token=f"confirmation_invalidated|{normalized_reason}",
                    context=context,
                    kind="stage3_sell_quote_guard",
                )
                existing = condition
            if existing is not None:
                existing_context = dict(existing.get("latest_context") or context)
                duration = max(
                    0.0,
                    time.monotonic()
                    - float(existing.get("first_seen_monotonic", time.monotonic())),
                )
                self._set_stage3_quote_guard_status(
                    cycle,
                    state="blocked",
                    reason_code=str(existing.get("latest_reason_code") or normalized_reason),
                    message=str(existing.get("latest_message") or message),
                    context=existing_context,
                    severity=(
                        "warning"
                        if duration >= self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS
                        or bool(existing_context.get("near_trigger"))
                        else "info"
                    ),
                )
            else:
                self._set_stage3_quote_guard_status(
                    cycle,
                    state="waiting_quote_update",
                    reason_code=normalized_reason,
                    message=message,
                    context=context,
                    severity="normal",
                )
            return

        existing = self._audit_conditions.get(key)
        near_trigger = bool(context.get("near_trigger"))
        severe_codes = {
            "FIELD_TRACKING_UNAVAILABLE",
            "UPSTREAM_UNCONFIRMED",
            "EVENT_IDENTITY_UNAVAILABLE",
            "TRIGGER_UNAVAILABLE",
        }
        immediate_level: Optional[str] = None
        immediate_message: Optional[str] = None
        immediate_token: Optional[str] = None
        if confirmation_was_pending:
            immediate_level = "WARN"
            immediate_token = f"confirmation_invalidated|{normalized_reason}"
            immediate_message = (
                "Stage-3 SELL confirmation was invalidated before the second qualifying quote: "
                f"{message}"
            )
        elif near_trigger:
            immediate_level = "WARN"
            immediate_token = f"near_trigger|{normalized_reason}"
            immediate_message = (
                "Stage-3 SELL evidence is invalid while price is near or above the profit trigger: "
                f"{message}"
            )
        elif normalized_reason in severe_codes:
            immediate_level = "WARN"
            immediate_token = f"severe|{normalized_reason}"
            immediate_message = f"Stage-3 SELL evidence is unavailable: {message}"
        elif (
            existing is None
            and self._stage3_routine_guard_entry_cycle_id != cycle.id
        ):
            immediate_level = "INFO"
            immediate_token = "condition_started"
            immediate_message = f"Stage-3 SELL evidence is temporarily unavailable: {message}"
            self._stage3_routine_guard_entry_cycle_id = cycle.id

        condition = self._update_audit_condition(
            key=key,
            family="Stage-3 SELL quote evidence",
            reason_code=normalized_reason,
            message=message,
            cycle=cycle,
            initial_delay_seconds=self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS,
            repeat_interval_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
            summary_level="WARN",
            immediate_level=immediate_level,
            immediate_message=immediate_message,
            immediate_token=immediate_token,
            context=context,
            kind="stage3_sell_quote_guard",
        )
        duration = max(
            0.0,
            time.monotonic()
            - float(condition.get("first_seen_monotonic", time.monotonic())),
        )
        severity = (
            "warning"
            if near_trigger or normalized_reason in severe_codes
            or duration >= self.DIAGNOSTIC_FIRST_SUMMARY_SECONDS
            else "info"
        )
        self._set_stage3_quote_guard_status(
            cycle,
            state="blocked",
            reason_code=normalized_reason,
            message=message,
            context=context,
            severity=severity,
        )

    def _clear_stage3_quote_guard(
        self,
        cycle: Optional[CycleState],
        *,
        message: str = "",
        state: str = "ready",
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        if cycle is None:
            self._stage3_quote_guard_status = {}
            return
        self._clear_audit_condition(
            self._stage3_quote_guard_condition_key(cycle),
            cycle=cycle,
            recovery_message=message or "Fresh Stage-3 quote evidence is available.",
            context=context,
        )
        reason_by_state = {
            "waiting_below_trigger": "TRIGGER_NOT_REACHED",
            "confirmation_pending": "CONFIRMATION_PENDING",
            "confirmed": "CONFIRMED",
            "ready": "OK",
        }
        self._set_stage3_quote_guard_status(
            cycle,
            state=state,
            reason_code=reason_by_state.get(str(state or ""), "OK"),
            message=message or "Fresh complete Stage-3 quote evidence is available.",
            context=context,
            severity="normal",
        )

    def _confirm_stage3_sell_evidence(
        self,
        cycle: CycleState,
        evidence: dict[str, Any],
    ) -> bool:
        previous = dict(self._stage3_sell_confirmation or {})
        same_context = bool(
            previous
            and str(previous.get("cycle_id") or "") == cycle.id
            and str(previous.get("subscription_id") or "") == str(evidence.get("subscription_id") or "")
        )
        current_sequence = int(evidence.get("sequence") or 0)
        previous_sequence = int(previous.get("sequence") or 0) if same_context else 0
        current_quote_sequence = int(evidence.get("quote_sequence") or 0)
        previous_quote_sequence = int(previous.get("quote_sequence") or 0) if same_context else 0
        if same_context and (
            current_sequence <= previous_sequence
            or current_quote_sequence <= previous_quote_sequence
        ):
            return False
        if not same_context:
            self._stage3_sell_confirmation = dict(evidence)
            self.storage.add_decision_event(
                event_type="SELL_TRIGGER_CONFIRMATION_STARTED",
                message=(
                    "The executable bid reached the Stage-3 trigger with a fresh valid quote. "
                    "Waiting for one more distinct consistent market-data update before arming the final SELL."
                ),
                cycle=cycle,
                stage_before=cycle.stage.value,
                stage_after=cycle.stage.value,
                decision_result="waiting_confirmation",
                raw={
                    "required_confirmations": self.STAGE3_SELL_CONFIRMATIONS_REQUIRED,
                    "evidence": dict(evidence),
                },
            )
            return False

        self._stage3_sell_confirmation.clear()
        self.storage.add_decision_event(
            event_type="SELL_TRIGGER_CONFIRMATION_PASSED",
            message="Two distinct fresh quote observations confirmed the Stage-3 final-SELL trigger.",
            cycle=cycle,
            stage_before=cycle.stage.value,
            stage_after=cycle.stage.value,
            decision_result="confirmed",
            raw={
                "required_confirmations": self.STAGE3_SELL_CONFIRMATIONS_REQUIRED,
                "first_evidence": previous,
                "second_evidence": dict(evidence),
            },
        )
        return True

    @staticmethod
    def _stage3_waiting_cycle_with_price(cycle: CycleState, price: float) -> CycleState:
        waiting = copy(cycle)
        waiting.last_price = float(price)
        waiting.rise_trigger_price = StrategyEngine.recalculate_rise_trigger_price(waiting)
        waiting.touch()
        return waiting

    def _stage3_sell_submission_guard_message(
        self,
        cycle: CycleState,
        payload: dict[str, Any],
    ) -> Optional[str]:
        expected = payload.get("stage3_market_data_guard")
        if not isinstance(expected, dict):
            return None
        evidence, message = self._stage3_sell_quote_evidence(cycle, require_latest_event=False)
        if evidence is None:
            return f"Final SELL market-data revalidation failed: {message}"
        for key in (
            "cycle_id",
            "subscription_id",
            "sequence",
            "quote_sequence",
            "quote_kind",
            "selected_price_source",
            "selected_price_basis",
            "selected_price_basis_sequence",
        ):
            if str(evidence.get(key)) != str(expected.get(key)):
                return (
                    "Final SELL market-data revalidation failed: the quote identity changed "
                    f"between confirmation and submission ({key})."
                )
        try:
            expected_bid = float(expected.get("bid") or 0.0)
            current_bid = float(evidence.get("bid") or 0.0)
            expected_ask = float(expected.get("ask") or 0.0)
            current_ask = float(evidence.get("ask") or 0.0)
        except Exception:
            expected_bid = 0.0
            current_bid = 0.0
            expected_ask = 0.0
            current_ask = 0.0
        try:
            min_tick = max(1e-9, float(getattr(self.contract, "min_tick", 0.0) or 0.0))
        except Exception:
            min_tick = 1e-9
        if expected_bid <= 0 or current_bid <= 0 or abs(current_bid - expected_bid) > min_tick:
            return "Final SELL market-data revalidation failed: the executable bid changed before submission."
        if expected_ask <= 0 or current_ask <= 0 or abs(current_ask - expected_ask) > min_tick:
            return "Final SELL market-data revalidation failed: the ask changed before submission."
        try:
            reference_price = float(payload.get("reference_price") or 0.0)
        except Exception:
            reference_price = 0.0
        if reference_price <= 0 or abs(reference_price - current_bid) > min_tick:
            return "Final SELL market-data revalidation failed: the order reference is not the confirmed executable bid."
        return None

    def _rollback_final_sell_market_data_block(
        self,
        cycle: CycleState,
        message: str,
        *,
        order_ref: str = "",
    ) -> None:
        if order_ref:
            try:
                self.storage.mark_order_intent_failed(
                    order_ref,
                    message,
                    raw={"reason": "stage3_market_data_revalidation"},
                )
            except Exception:
                pass
        rolled_back = StrategyEngine.rollback_unsubmitted_order(cycle, "SELL", message)
        self.active_cycle = rolled_back
        self.storage.upsert_cycle(rolled_back)
        self._reset_stage3_sell_confirmation(cycle.id)
        self.storage.add_decision_event(
            event_type="SELL_MARKET_DATA_REVALIDATION_BLOCKED",
            message=message,
            cycle=rolled_back,
            stage_before=cycle.stage.value,
            stage_after=rolled_back.stage.value,
            decision_result="blocked",
            raw={"order_ref": order_ref},
        )
        self._log("WARN", message, rolled_back)

    def _advance_waiting_cycle_from_price(
        self,
        cycle: CycleState,
        last_price: float,
        *,
        is_rth: bool,
        rth_message: str,
    ) -> tuple[CycleState, list[StrategyAction]]:
        """Advance a waiting stage while enforcing ATR warm-up semantics.

        When the ATR-entry block is active, Stage 1 deliberately has no initial
        drop trigger.  Every usable warm-up price becomes only the latest
        reference.  The first tick after readiness establishes a fresh anchor;
        only a later tick may satisfy the ATR-derived initial-drop percentage.
        """
        if self._retired_account_position_block_message(cycle.error_message):
            cycle.error_message = None
        if cycle.stage == Stage.WAIT_INITIAL_DROP:
            atr_blocker = self._atr_warmup_guard_blocker_for_buy(cycle)
            if atr_blocker is not None:
                paused = StrategyEngine.pause_initial_drop_until_ready(
                    cycle,
                    last_price,
                    atr_blocker["message"],
                )
                return paused, []
            if self._cycle_paused_for_atr_warmup(cycle):
                return StrategyEngine.restart_initial_drop_from_price(cycle, last_price), []
        if cycle.stage == Stage.WAIT_RISE_TRIGGER and bool(
            (self.price_snapshot or {}).get("market_data_field_tracking")
        ):
            evidence, guard_message, reason_code, guard_context = self._stage3_sell_quote_evidence_detail(
                cycle,
                require_latest_event=True,
            )
            if evidence is None:
                confirmation_was_pending = bool(
                    self._stage3_sell_confirmation
                    and str(self._stage3_sell_confirmation.get("cycle_id") or "") == cycle.id
                )
                self._reset_stage3_sell_confirmation(cycle.id)
                self._observe_stage3_quote_guard(
                    cycle,
                    reason_code=reason_code,
                    message=guard_message,
                    context=guard_context,
                    confirmation_was_pending=confirmation_was_pending,
                )
                # Keep the cycle's last actionable price rather than persisting
                # a rejected convenience value such as an unchanged cached
                # Last. The GUI still displays the raw snapshot separately.
                retained_price = self._positive_float(cycle.last_price) or float(last_price)
                return self._stage3_waiting_cycle_with_price(cycle, retained_price), []

            reference_price = float(evidence["reference_price"])
            if not self._confirm_stage3_sell_evidence(cycle, evidence):
                confirmation_count = 1 if self._stage3_sell_confirmation else 0
                confirmation_context = dict(guard_context)
                confirmation_context["confirmation_pending"] = True
                confirmation_context["confirmation_count"] = confirmation_count
                self._clear_stage3_quote_guard(
                    cycle,
                    state="confirmation_pending",
                    message=(
                        f"Fresh quote accepted; {confirmation_count} of "
                        f"{self.STAGE3_SELL_CONFIRMATIONS_REQUIRED} confirmations received."
                    ),
                    context=confirmation_context,
                )
                return self._stage3_waiting_cycle_with_price(cycle, reference_price), []

            confirmed_context = dict(guard_context)
            confirmed_context["confirmation_pending"] = False
            confirmed_context["confirmation_count"] = self.STAGE3_SELL_CONFIRMATIONS_REQUIRED
            self._clear_stage3_quote_guard(
                cycle,
                state="confirmed",
                message="Two fresh quote observations confirmed the final-SELL trigger.",
                context=confirmed_context,
            )

            next_cycle, actions = StrategyEngine.on_price_update(
                cycle,
                reference_price,
                is_rth=is_rth,
                rth_message=rth_message,
            )
            for action in actions:
                if action.action_type in {"PLACE_SELL_TRAIL", "PLACE_SELL_MARKET"}:
                    action.payload["stage3_market_data_guard"] = dict(evidence)
            return next_cycle, actions
        return StrategyEngine.on_price_update(
            cycle,
            last_price,
            is_rth=is_rth,
            rth_message=rth_message,
        )

    def _risk_guard_blockers_for_buy(
        self,
        cycle: CycleState,
        payload: Optional[dict[str, Any]] = None,
        *,
        stop_after_first: bool = False,
        database_facts: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, str]]:
        """Return currently active configured BUY blockers.

        The order of this list matches the order-submission fail-closed priority.
        The GUI requests the complete list, while broker submission asks only for
        the first blocker to preserve the existing operator-facing behavior.
        """
        payload = payload or {}
        blockers: list[dict[str, str]] = []
        hard_limits_enabled = bool(getattr(cycle, "hard_risk_limits_enabled", False))
        delayed_live_guard_enabled = bool(getattr(cycle, "block_delayed_data_in_live", False))
        stale_guard_enabled = bool(getattr(cycle, "stale_data_guard_enabled", False))
        session_guard_enabled = bool(getattr(cycle, "session_timing_guard_enabled", False))
        volatility_guard_enabled = bool(getattr(cycle, "volatility_filter_enabled", False))
        atr_warmup_guard_enabled = bool(getattr(cycle, "atr_block_new_buy_until_ready", False)) and bool(
            getattr(cycle, "atr_adaptive_enabled", False)
        )
        if not any(
            [
                hard_limits_enabled,
                delayed_live_guard_enabled,
                stale_guard_enabled,
                session_guard_enabled,
                volatility_guard_enabled,
                atr_warmup_guard_enabled,
            ]
        ):
            return blockers

        def add(code: str, message: str, short: str) -> bool:
            blockers.append(self._trading_blocker("BUY", code, message, short))
            return stop_after_first

        def database_fact(name: str, loader: Any) -> Any:
            if database_facts is None:
                return loader()
            if (
                str(database_facts.get("ticker") or "") != cycle.ticker
                or int(database_facts.get("con_id") or 0) != int(cycle.con_id or 0)
            ):
                raise RuntimeError("cached risk facts are for a different exact contract")
            errors = database_facts.get("errors") or {}
            if name in errors:
                raise RuntimeError(str(errors[name]))
            if name not in database_facts:
                raise RuntimeError(f"cached risk fact {name} has not been refreshed")
            return database_facts[name]

        ticker = cycle.ticker
        if hard_limits_enabled and float(getattr(cycle, "max_daily_loss_ticker", 0.0) or 0.0) > 0:
            pnl = database_fact("daily_net_pnl_ticker", lambda: self.storage.get_daily_net_pnl_for_ticker(ticker, con_id=cycle.con_id))
            if pnl <= -float(cycle.max_daily_loss_ticker):
                message = f"Hard risk limit blocked BUY: {ticker} daily app P/L {pnl:.2f} is at/below -{cycle.max_daily_loss_ticker:.2f}."
                if add("daily_loss_ticker", message, "Ticker loss limit"):
                    return blockers
        if hard_limits_enabled and float(getattr(cycle, "max_daily_loss_total", 0.0) or 0.0) > 0:
            pnl = database_fact("daily_net_pnl_total", self.storage.get_daily_net_pnl_total)
            if pnl <= -float(cycle.max_daily_loss_total):
                message = f"Hard risk limit blocked BUY: total daily app P/L {pnl:.2f} is at/below -{cycle.max_daily_loss_total:.2f}."
                if add("daily_loss_total", message, "Total loss limit"):
                    return blockers
        if hard_limits_enabled and int(getattr(cycle, "max_cycles_per_ticker_day", 0) or 0) > 0:
            count = database_fact("completed_cycle_count", lambda: self.storage.get_completed_cycle_count(ticker, con_id=cycle.con_id))
            if count >= int(cycle.max_cycles_per_ticker_day):
                message = f"Hard risk limit blocked BUY: {ticker} already has {count} completed cycles in total."
                if add("max_cycles", message, "Max cycles"):
                    return blockers
        if hard_limits_enabled and int(getattr(cycle, "max_consecutive_losses", 0) or 0) > 0:
            count = database_fact("consecutive_loss_count", lambda: self.storage.get_consecutive_loss_count(ticker, con_id=cycle.con_id))
            if count >= int(cycle.max_consecutive_losses):
                message = f"Hard risk limit blocked BUY: {ticker} has {count} consecutive losing completed cycles."
                if add("loss_streak", message, "Loss streak"):
                    return blockers

        try:
            price = float(payload.get("sizing_price") or payload.get("initial_stop_price") or cycle.last_price or 0.0)
        except Exception:
            price = 0.0
        if hard_limits_enabled and price > 0 and float(getattr(cycle, "min_trade_price", 0.0) or 0.0) > 0 and price < float(cycle.min_trade_price):
            message = f"Hard risk limit blocked BUY: sizing price {price:.4f} is below minimum trade price {cycle.min_trade_price:.4f}."
            if add("min_trade_price", message, "Minimum price"):
                return blockers

        snapshot = self.price_snapshot or {}
        delayed_blocker = self._delayed_live_data_blocker_for_buy(cycle)
        if delayed_blocker is not None:
            blockers.append(delayed_blocker)
            if stop_after_first:
                return blockers

        atr_blocker = self._atr_warmup_guard_blocker_for_buy(cycle)
        if atr_blocker is not None:
            blockers.append(atr_blocker)
            if stop_after_first:
                return blockers

        for code, short, guard in (
            ("stale_data", "Stale data", self._stale_data_guard_message_for_buy),
            ("session_timing", "Open/close window", self._session_timing_guard_message_for_buy),
            ("volatility", "Volatility", self._volatility_guard_message_for_buy),
        ):
            message = guard(cycle)
            if message:
                if add(code, message, short):
                    return blockers

        max_spread = float(getattr(cycle, "max_spread_pct", 0.0) or 0.0)
        if hard_limits_enabled and max_spread > 0:
            bid = self._snapshot_field(snapshot, "bid", "delayedBid")
            ask = self._snapshot_field(snapshot, "ask", "delayedAsk")
            if bid is None or ask is None or ask < bid:
                message = "Hard risk limit blocked BUY: bid/ask spread cannot be verified."
                if add("spread_unverified", message, "Spread unavailable"):
                    return blockers
            else:
                midpoint = (bid + ask) / 2.0
                spread_pct = ((ask - bid) / midpoint) * 100.0 if midpoint > 0 else 999999.0
                if spread_pct > max_spread:
                    message = f"Hard risk limit blocked BUY: spread {spread_pct:.2f}% exceeds max {max_spread:.2f}%."
                    if add("spread", message, "Spread"):
                        return blockers

        max_gap = float(getattr(cycle, "max_gap_from_prev_close_pct", 0.0) or 0.0)
        if hard_limits_enabled and max_gap > 0:
            close = self._snapshot_field(snapshot, "close", "delayedClose")
            current = self._snapshot_field(snapshot, "marketPrice", "bidAskMidpoint", "delayedBidAskMidpoint", "last", "delayedLast")
            if close is None or current is None:
                message = "Hard risk limit blocked BUY: previous close/current price gap cannot be verified."
                if add("gap_unverified", message, "Gap unavailable"):
                    return blockers
            else:
                gap_pct = abs((current / close) - 1.0) * 100.0 if close > 0 else 999999.0
                if gap_pct > max_gap:
                    message = f"Hard risk limit blocked BUY: current gap from close {gap_pct:.2f}% exceeds max {max_gap:.2f}%."
                    if add("gap", message, "Gap from close"):
                        return blockers
        return blockers

    def _delayed_live_data_blocker_for_buy(
        self,
        cycle: CycleState,
    ) -> Optional[dict[str, str]]:
        if not bool(getattr(cycle, "block_delayed_data_in_live", False)):
            return None
        if str(getattr(self.connection, "trading_mode", "") or "").strip().lower() != "live":
            return None
        snapshot = self.price_snapshot or {}
        selected = snapshot.get("selected_market_data_type")
        actual = snapshot.get("subscription_market_data_type")
        mode = actual if actual is not None else selected
        if mode is None:
            return self._trading_blocker(
                "BUY",
                "live_data_unconfirmed",
                "Hard risk limit blocked BUY: live profile market-data mode is not confirmed as live.",
                "Live data unconfirmed",
            )
        if int(mode) != 1:
            return self._trading_blocker(
                "BUY",
                "non_live_data",
                f"Hard risk limit blocked BUY: live profile is using non-live market data mode {mode}.",
                "Non-live data",
            )
        return None

    def _risk_guard_blocker_for_buy(
        self,
        cycle: CycleState,
        payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Return the first configured BUY blocker in fail-closed priority order."""
        blockers = self._risk_guard_blockers_for_buy(cycle, payload, stop_after_first=True)
        return blockers[0] if blockers else None

    def _risk_guard_message_for_buy(self, cycle: CycleState, payload: dict[str, Any]) -> Optional[str]:
        blocker = self._risk_guard_blocker_for_buy(cycle, payload)
        return str(blocker["message"]) if blocker else None

    def _buy_submission_preflight_message(self, cycle: CycleState, payload: dict[str, Any]) -> Optional[str]:
        connectivity_message = self._order_submission_connectivity_message("BUY")
        if connectivity_message:
            return f"BUY pre-flight blocked order: {connectivity_message}"
        account = str(getattr(self.connection, "account", "") or getattr(cycle, "account", "") or "").strip()
        if str(getattr(self.connection, "trading_mode", "") or "").strip().lower() == "live" and account:
            method = getattr(self.adapter, "managed_accounts", None)
            if callable(method):
                try:
                    accounts = [str(item).strip() for item in (method() or []) if str(item).strip()]
                except Exception as exc:
                    return f"BUY pre-flight blocked order: could not confirm managed accounts before live order submission: {exc}"
                if accounts and account not in accounts:
                    return f"BUY pre-flight blocked order: configured account {account} is not in the IBKR managed-account list."
        if self.contract is None:
            return "BUY pre-flight blocked order: no qualified contract is available."
        expected_con_id = getattr(cycle, "con_id", None)
        actual_con_id = getattr(self.contract, "con_id", None)
        if expected_con_id and actual_con_id and int(expected_con_id) != int(actual_con_id):
            return f"BUY pre-flight blocked order: contract conId changed from {expected_con_id} to {actual_con_id}."
        app_position_blocker = self._app_owned_position_blocker_for_buy(cycle)
        if app_position_blocker is not None:
            return app_position_blocker["message"]
        snapshot = self.price_snapshot or {}
        if str(getattr(self.connection, "trading_mode", "") or "").strip().lower() == "live" and bool(getattr(cycle, "block_delayed_data_in_live", False)):
            mode = snapshot.get("subscription_market_data_type")
            if mode is None:
                mode = snapshot.get("selected_market_data_type")
            if mode is None or int(mode) != 1:
                return f"BUY pre-flight blocked order: live order submission requires confirmed live market data; current mode is {mode}."
        return None

    def _record_order_intent(self, cycle: CycleState, payload: dict[str, Any], side: str, order_type: str, role: str = "") -> None:
        action = "PROTECTIVE_SELL" if role == "PROTECTIVE_SELL" else side.upper()
        self.storage.create_order_intent(
            cycle=cycle,
            action=action,
            order_type=order_type,
            order_ref=str(payload["order_ref"]),
            quantity=int(payload["quantity"]),
            trailing_percent=(float(payload["trailing_percent"]) if payload.get("trailing_percent") not in (None, "") else None),
            initial_stop_price=(float(payload["initial_stop_price"]) if payload.get("initial_stop_price") not in (None, "") else payload.get("reference_price")),
            raw={"payload": dict(payload), "side": side.upper(), "role": role, "order_type": order_type},
        )
        self.storage.add_decision_event(
            event_type="ORDER_INTENT",
            message=f"Durably recorded {action} {order_type} order intent before broker submission.",
            cycle=cycle,
            stage_before=cycle.stage.value,
            stage_after=cycle.stage.value,
            decision_result="intent_recorded",
            raw={"payload": dict(payload), "side": side.upper(), "role": role, "order_type": order_type},
        )

    def _normalize_contract_quantity(
        self,
        cycle: CycleState,
        payload: dict[str, Any],
        side: str,
    ) -> tuple[dict[str, Any], Optional[str]]:
        """Apply IBKR whole-share minimum/step rules before recording intent."""
        if self.contract is None:
            return payload, "Order quantity cannot be validated without a qualified contract."
        try:
            requested = int(payload.get("quantity") or 0)
        except Exception:
            requested = 0
        method = getattr(self.adapter, "normalize_order_quantity", None)
        try:
            normalized = int(method(self.contract, requested)) if callable(method) else requested
        except Exception as exc:
            return payload, f"Order quantity validation failed: {exc}"
        if normalized <= 0:
            return payload, (
                "Calculated quantity is below the selected contract's whole-share minimum or size increment."
            )
        side_value = str(side or "").upper()
        if side_value == "SELL" and normalized != requested:
            return payload, (
                f"SELL quantity {requested} does not conform to the selected contract's size increment; "
                "the app will not leave an untracked remainder."
            )
        updated = dict(payload)
        updated["quantity"] = normalized
        if side_value == "BUY" and normalized != requested:
            cycle.quantity = normalized
            return updated, f"Normalized BUY quantity from {requested} to {normalized} shares for the selected contract size rules."
        return updated, None

    def _place_trailing_order(self, cycle: CycleState, action: StrategyAction, side: str, role: str = "") -> None:
        rollback_side = "PROTECTIVE_SELL" if role == "PROTECTIVE_SELL" else side.upper()
        connectivity_message = self._order_submission_connectivity_message(rollback_side)
        if connectivity_message:
            if rollback_side == "BUY":
                self._apply_buy_preflight_block(cycle, connectivity_message, "connectivity")
            else:
                self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, connectivity_message)
                self.storage.upsert_cycle(self.active_cycle)
                self._log("WARN", connectivity_message, self.active_cycle)
            return
        if self.contract is None:
            self.contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
        if getattr(cycle, "rth_only", True):
            rth_status = self._update_rth_status(self.contract)
            if not bool(rth_status.get("is_open", True)):
                detail = str(rth_status.get("message") or rth_status.get("source") or "regular trading hours are closed")
                message = f"RTH guard blocked {side} order submission: {detail}"
                if side.upper() == "BUY":
                    self._apply_buy_preflight_block(cycle, message, "rth_closed")
                else:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, side, message)
                    self.storage.upsert_cycle(self.active_cycle)
                    self._log("WARN", message, self.active_cycle)
                return
        if side.upper() == "BUY":
            delayed_blocker = self._delayed_live_data_blocker_for_buy(cycle)
            if delayed_blocker is not None:
                self._apply_buy_preflight_block(
                    cycle,
                    str(delayed_blocker["message"]),
                    str(delayed_blocker.get("code") or "non_live_data"),
                )
                return
        payload = dict(action.payload)
        payload, normalization_message = self._normalize_trailing_order_payload(cycle, payload, side, role=role)
        if normalization_message:
            if normalization_message.startswith(("SELL stop", "Order-price validation blocked")):
                if side.upper() == "BUY":
                    self._apply_buy_preflight_block(
                        cycle,
                        normalization_message,
                        "price_normalization",
                    )
                else:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, side, normalization_message)
                    self.storage.upsert_cycle(self.active_cycle)
                    self._log("WARN", normalization_message, self.active_cycle)
                return
            self._log("INFO", normalization_message, cycle)
        payload, quantity_message = self._normalize_contract_quantity(cycle, payload, side)
        if quantity_message:
            if quantity_message.startswith(("Order quantity", "Calculated quantity", "SELL quantity")):
                if rollback_side == "BUY":
                    self._apply_buy_preflight_block(cycle, quantity_message, "quantity")
                else:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, quantity_message)
                    self.storage.upsert_cycle(self.active_cycle)
                    self._log("WARN", quantity_message, self.active_cycle)
                return
            self._log("INFO", quantity_message, cycle)
        if side.upper() == "BUY":
            try:
                cycle.quantity = int(payload.get("quantity") or cycle.quantity)
                cycle.buy_initial_trail_stop_price = float(payload.get("initial_stop_price") or cycle.buy_initial_trail_stop_price or 0.0)
                self.storage.upsert_cycle(cycle)
            except Exception:
                pass
            if int(payload.get("quantity") or 0) <= 0:
                message = "Calculated quantity is zero after contract tick/slippage order-price normalization."
                self._apply_buy_preflight_block(cycle, message, "quantity")
                return
        elif side.upper() == "SELL":
            try:
                if role == "PROTECTIVE_SELL":
                    cycle.protective_sell_initial_stop_price = float(payload.get("initial_stop_price") or cycle.protective_sell_initial_stop_price or 0.0)
                else:
                    cycle.sell_initial_trail_stop_price = float(payload.get("initial_stop_price") or cycle.sell_initial_trail_stop_price or 0.0)
                self.storage.upsert_cycle(cycle)
            except Exception:
                pass
        if side.upper() == "BUY":
            risk_blocker = self._risk_guard_blocker_for_buy(cycle, payload)
            if risk_blocker:
                self._apply_buy_preflight_block(
                    cycle,
                    str(risk_blocker["message"]),
                    str(risk_blocker.get("code") or "risk_guard"),
                )
                return
        if side.upper() == "BUY":
            what_if_message = self._what_if_guard_message_for_buy(cycle, payload)
            if what_if_message:
                self._apply_buy_preflight_block(cycle, what_if_message, "what_if")
                return
        if side.upper() == "BUY":
            preflight_message = self._buy_submission_preflight_message(cycle, payload)
            if preflight_message:
                self._apply_buy_preflight_block(
                    cycle,
                    preflight_message,
                    "submission_preflight",
                )
                return
        connectivity_message = self._order_submission_connectivity_message(rollback_side)
        if connectivity_message:
            if rollback_side == "BUY":
                self._apply_buy_preflight_block(cycle, connectivity_message, "connectivity")
            else:
                self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, connectivity_message)
                self.storage.upsert_cycle(self.active_cycle)
                self._log("WARN", connectivity_message, self.active_cycle)
            return
        if rollback_side == "BUY":
            self._clear_audit_condition(
                f"buy_preflight_block|{cycle.id}",
                cycle=cycle,
                recovery_message="All BUY preflight checks are passing; order submission is proceeding.",
            )
        try:
            self.storage.backup_database("before_order_submit")
        except Exception:
            pass
        order_type = "PROTECTIVE_TRAIL" if role == "PROTECTIVE_SELL" else "TRAIL"
        if rollback_side == "SELL" and isinstance(payload.get("stage3_market_data_guard"), dict):
            guard_message = self._stage3_sell_submission_guard_message(cycle, payload)
            if guard_message:
                self._rollback_final_sell_market_data_block(cycle, guard_message)
                return
        self._record_order_intent(cycle, payload, side, order_type, role=role)
        if self._broker_mutation_blocked_by_storage_fault(
            f"{rollback_side} trailing-order submission",
            cycle,
        ):
            return
        if rollback_side == "SELL" and isinstance(payload.get("stage3_market_data_guard"), dict):
            guard_message = self._stage3_sell_submission_guard_message(cycle, payload)
            if guard_message:
                self._rollback_final_sell_market_data_block(
                    cycle,
                    guard_message,
                    order_ref=str(payload.get("order_ref") or ""),
                )
                return
        try:
            handle = self.adapter.place_trailing_stop(
                contract=self.contract,
                action=side,
                quantity=int(payload["quantity"]),
                trailing_percent=float(payload["trailing_percent"]),
                initial_stop_price=float(payload["initial_stop_price"]),
                order_ref=str(payload["order_ref"]),
                tif=self.strategy.tif,
                account=(self.connection.account or cycle.account),
                outside_rth=False,
            )
        except BrokerAdapterError as exc:
            self.storage.mark_order_intent_failed(str(payload["order_ref"]), str(exc), raw={"payload": dict(payload), "side": side})
            raise
        except Exception as exc:
            self.storage.mark_order_intent_failed(str(payload["order_ref"]), str(exc), raw={"payload": dict(payload), "side": side})
            raise BrokerAdapterError(f"Broker trailing-order submission failed: {exc}") from exc
        updated = StrategyEngine.on_order_submitted(cycle, handle.order_ref, handle.order_id, handle.perm_id, handle.status)
        self.active_cycle = updated
        self.storage.record_order_submission(
            cycle=updated,
            order_ref=handle.order_ref,
            order_id=handle.order_id,
            perm_id=handle.perm_id,
            status=handle.status,
            raw=handle.raw,
        )
        label = "protective SELL" if role == "PROTECTIVE_SELL" else side
        self.storage.add_decision_event(
            event_type="ORDER_SUBMITTED",
            message=f"Submitted {label} TRAIL order.",
            cycle=updated,
            stage_before=cycle.stage.value,
            stage_after=updated.stage.value,
            decision_result="submitted",
            broker_order_id=handle.order_id,
            perm_id=handle.perm_id,
            raw=handle.raw,
        )
        self._log("INFO", f"Submitted {label} TRAIL order {handle.order_ref} qty={payload['quantity']} trail={payload['trailing_percent']}%, outsideRth=False.", updated)

    def _place_market_order(self, cycle: CycleState, action: StrategyAction, side: str) -> None:
        rollback_side = side.upper()
        connectivity_message = self._order_submission_connectivity_message(rollback_side)
        if connectivity_message:
            if rollback_side == "BUY":
                self._apply_buy_preflight_block(cycle, connectivity_message, "connectivity")
            else:
                self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, connectivity_message)
                self.storage.upsert_cycle(self.active_cycle)
                self._log("WARN", connectivity_message, self.active_cycle)
            return
        if self.contract is None:
            self.contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
        if getattr(cycle, "rth_only", True):
            rth_status = self._update_rth_status(self.contract)
            if not bool(rth_status.get("is_open", True)):
                detail = str(rth_status.get("message") or rth_status.get("source") or "regular trading hours are closed")
                message = f"RTH guard blocked {side} market order submission: {detail}"
                if side.upper() == "BUY":
                    self._apply_buy_preflight_block(cycle, message, "rth_closed")
                else:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, side, message)
                    self.storage.upsert_cycle(self.active_cycle)
                    self._log("WARN", message, self.active_cycle)
                return
        if side.upper() == "BUY":
            delayed_blocker = self._delayed_live_data_blocker_for_buy(cycle)
            if delayed_blocker is not None:
                self._apply_buy_preflight_block(
                    cycle,
                    str(delayed_blocker["message"]),
                    str(delayed_blocker.get("code") or "non_live_data"),
                )
                return
        payload = dict(action.payload)
        payload, quantity_message = self._normalize_contract_quantity(cycle, payload, side)
        if quantity_message:
            if quantity_message.startswith(("Order quantity", "Calculated quantity", "SELL quantity")):
                if rollback_side == "BUY":
                    self._apply_buy_preflight_block(cycle, quantity_message, "quantity")
                else:
                    self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, quantity_message)
                    self.storage.upsert_cycle(self.active_cycle)
                    self._log("WARN", quantity_message, self.active_cycle)
                return
            self._log("INFO", quantity_message, cycle)
        if side.upper() == "BUY":
            cycle.quantity = int(payload.get("quantity") or cycle.quantity)
            self.storage.upsert_cycle(cycle)
        if side.upper() == "BUY":
            risk_blocker = self._risk_guard_blocker_for_buy(cycle, payload)
            if risk_blocker:
                self._apply_buy_preflight_block(
                    cycle,
                    str(risk_blocker["message"]),
                    str(risk_blocker.get("code") or "risk_guard"),
                )
                return
            what_if_message = self._what_if_guard_message_for_buy(cycle, payload)
            if what_if_message:
                self._apply_buy_preflight_block(cycle, what_if_message, "what_if")
                return
        if side.upper() == "BUY":
            preflight_message = self._buy_submission_preflight_message(cycle, payload)
            if preflight_message:
                self._apply_buy_preflight_block(
                    cycle,
                    preflight_message,
                    "submission_preflight",
                )
                return
        connectivity_message = self._order_submission_connectivity_message(rollback_side)
        if connectivity_message:
            if rollback_side == "BUY":
                self._apply_buy_preflight_block(cycle, connectivity_message, "connectivity")
            else:
                self.active_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, rollback_side, connectivity_message)
                self.storage.upsert_cycle(self.active_cycle)
                self._log("WARN", connectivity_message, self.active_cycle)
            return
        if rollback_side == "BUY":
            self._clear_audit_condition(
                f"buy_preflight_block|{cycle.id}",
                cycle=cycle,
                recovery_message="All BUY preflight checks are passing; order submission is proceeding.",
            )
        try:
            self.storage.backup_database("before_order_submit")
        except Exception:
            pass
        if rollback_side == "SELL" and isinstance(payload.get("stage3_market_data_guard"), dict):
            guard_message = self._stage3_sell_submission_guard_message(cycle, payload)
            if guard_message:
                self._rollback_final_sell_market_data_block(cycle, guard_message)
                return
        self._record_order_intent(cycle, payload, side, "MKT")
        if self._broker_mutation_blocked_by_storage_fault(
            f"{rollback_side} market-order submission",
            cycle,
        ):
            return
        if rollback_side == "SELL" and isinstance(payload.get("stage3_market_data_guard"), dict):
            guard_message = self._stage3_sell_submission_guard_message(cycle, payload)
            if guard_message:
                self._rollback_final_sell_market_data_block(
                    cycle,
                    guard_message,
                    order_ref=str(payload.get("order_ref") or ""),
                )
                return
        try:
            handle = self.adapter.place_market_order(
                contract=self.contract,
                action=side,
                quantity=int(payload["quantity"]),
                order_ref=str(payload["order_ref"]),
                tif=self.strategy.tif,
                account=(self.connection.account or cycle.account),
                outside_rth=False,
            )
        except BrokerAdapterError as exc:
            self.storage.mark_order_intent_failed(str(payload["order_ref"]), str(exc), raw={"payload": dict(payload), "side": side})
            raise
        except Exception as exc:
            self.storage.mark_order_intent_failed(str(payload["order_ref"]), str(exc), raw={"payload": dict(payload), "side": side})
            raise BrokerAdapterError(f"Broker trailing-order submission failed: {exc}") from exc
        updated = StrategyEngine.on_order_submitted(cycle, handle.order_ref, handle.order_id, handle.perm_id, handle.status)
        self.active_cycle = updated
        self.storage.record_order_submission(
            cycle=updated,
            order_ref=handle.order_ref,
            order_id=handle.order_id,
            perm_id=handle.perm_id,
            status=handle.status,
            raw=handle.raw,
        )
        self.storage.add_decision_event(
            event_type="ORDER_SUBMITTED",
            message=f"Submitted {side} MKT order because trailing percentage is 0.",
            cycle=updated,
            stage_before=cycle.stage.value,
            stage_after=updated.stage.value,
            decision_result="submitted",
            broker_order_id=handle.order_id,
            perm_id=handle.perm_id,
            raw=handle.raw,
        )
        self._log("INFO", f"Submitted {side} MKT order {handle.order_ref} qty={payload['quantity']}; trailing disabled by 0% setting; outsideRth=False.", updated)

    @staticmethod
    def _order_terminal_without_fill(status: str) -> bool:
        return str(status or "").strip() in {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}

    @staticmethod
    def _broker_errors_from_polled(polled: PolledOrderState) -> list[dict[str, Any]]:
        raw = polled.raw if isinstance(polled.raw, dict) else {}
        values = raw.get("broker_errors")
        result = [dict(item) for item in values if isinstance(item, dict)] if isinstance(values, list) else []
        latest = raw.get("broker_error")
        if isinstance(latest, dict) and latest not in result:
            result.append(dict(latest))
        return result

    @staticmethod
    def _broker_error_is_rejection(item: dict[str, Any]) -> bool:
        try:
            code = int(item.get("error_code") or item.get("errorCode"))
        except Exception:
            code = None
        message = str(item.get("message") or item.get("error_string") or "").lower()
        rejection_words = (
            "reject",
            "invalid",
            "not allowed",
            "validation",
            "does not conform",
            "insufficient",
            "failed",
            "error",
        )
        if code == 202 and not any(word in message for word in rejection_words):
            return False
        if code is not None and code != 202:
            return True
        return any(word in message for word in rejection_words)

    @classmethod
    def _polled_order_rejection(cls, polled: PolledOrderState) -> Optional[dict[str, Any]]:
        status = str(polled.status or "").strip()
        errors = cls._broker_errors_from_polled(polled)
        for item in reversed(errors):
            if cls._broker_error_is_rejection(item):
                return item
        if status in {"Inactive", "Rejected"}:
            return {"error_code": None, "message": f"IBKR terminal order status {status}"}
        return None

    def _move_no_fill_order_to_stopped_error(
        self,
        cycle: CycleState,
        polled: PolledOrderState,
        side: str,
    ) -> None:
        rejection = self._polled_order_rejection(polled)
        detail = ""
        if rejection:
            code = self._optional_int(rejection.get("error_code") or rejection.get("errorCode"))
            text = str(rejection.get("message") or rejection.get("error_string") or "").strip()
            if code is not None and text:
                detail = f" IBKR error {code}: {text}."
            elif text:
                detail = f" {text}."
        cycle.stage = Stage.ERROR
        cycle.error_message = (
            f"{side} order is no longer working with no filled quantity "
            f"(status {polled.status}).{detail} Strategy paused for manual review; "
            "no replacement or automatic fresh-cycle retry will be sent."
        )
        if side == "BUY":
            cycle.buy_status = polled.status
        elif side == "PROTECTIVE_SELL":
            cycle.protective_sell_status = polled.status
            cycle.protective_sell_cancel_requested = False
        else:
            cycle.sell_status = polled.status
        cycle.recovery_required = False
        cycle.close_position_market_requested = False
        cycle.close_before_rth_liquidation_requested = False
        cycle.close_before_rth_cancel_requested = False
        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        raw = dict(polled.raw or {})
        raw.update(
            {
                "order_ref": polled.order_ref,
                "order_id": polled.order_id,
                "perm_id": polled.perm_id,
                "terminal_status": polled.status,
                "rejection": rejection,
            }
        )
        self.storage.add_decision_event(
            event_type="ORDER_TERMINAL_WITHOUT_FILL",
            message=cycle.error_message,
            cycle=cycle,
            decision_result="stopped_error",
            broker_order_id=polled.order_id,
            perm_id=polled.perm_id,
            raw=raw,
        )
        self._log("ERROR", cycle.error_message, cycle)

    def _log_native_order_wait_diagnostic(self, cycle: CycleState, status: str) -> None:
        """Throttle a clear explanation when a native trailing order is still waiting.

        The GUI price can be marketPrice/midpoint while IBKR triggerMethod=2 is
        Last.  This log avoids the common confusion where the chart line appears
        to cross the initial stop but TWS has not reported a fill.
        """
        diag = dict((self.price_snapshot or {}).get("native_order_trigger") or {})
        if not diag.get("active"):
            return
        message = str(diag.get("message") or "Native trailing order is waiting for broker fill/status.")
        side = str(diag.get("side") or "ORDER")
        stop = diag.get("displayed_initial_stop")
        raw_value = diag.get("raw_last_value")
        selected = diag.get("selected_price")
        details = f"Status={status or '-'}; selected={selected}; rawLast={raw_value}; displayedInitialStop={stop}. {message}"
        side_key = side.upper().strip()
        if side_key == "BUY":
            diagnostic_order_ref = cycle.buy_order_ref
        elif side_key == "PROTECTIVE_SELL":
            diagnostic_order_ref = cycle.protective_sell_order_ref
        else:
            diagnostic_order_ref = cycle.sell_order_ref
        selected_crossed = bool(diag.get("selected_crossed_displayed_initial_stop"))
        raw_last_crossed = bool(diag.get("raw_last_crossed_displayed_initial_stop"))
        if raw_last_crossed:
            reason_code = "RAW_LAST_CROSSED"
        elif selected_crossed:
            reason_code = "SELECTED_PRICE_CROSSED"
        elif raw_value in (None, ""):
            reason_code = "RAW_LAST_UNAVAILABLE"
        else:
            reason_code = "WAITING_FOR_NATIVE_TRIGGER"
        anomaly = bool(selected_crossed or raw_last_crossed)
        condition_suffix = f"{cycle.id}|{side_key}|{diagnostic_order_ref or ''}"
        wait_key = f"native_trailing_wait|{condition_suffix}"
        anomaly_key = f"native_trailing_anomaly|{condition_suffix}"
        context = {
            "side": side_key,
            "order_ref": diagnostic_order_ref or "",
            "status": status or "",
            "selected_price": selected,
            "raw_last_value": raw_value,
            "displayed_initial_stop": stop,
            "selected_crossed": selected_crossed,
            "raw_last_crossed": raw_last_crossed,
        }
        if anomaly:
            # Keep the expected long-running wait and the unusual trigger-cross
            # evidence as separate conditions. A transient crossing can then
            # recover immediately without leaving the ordinary wait on a WARN
            # cadence for the remainder of the broker order's lifetime.
            self._update_audit_condition(
                key=anomaly_key,
                family=f"Stage {cycle.stage.value} {side} trailing-order trigger anomaly",
                reason_code=reason_code,
                message=details,
                cycle=cycle,
                initial_delay_seconds=self.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS,
                repeat_interval_seconds=self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS,
                summary_level="WARN",
                first_summary_level="WARN",
                immediate_level="WARN" if raw_last_crossed else "INFO",
                immediate_message=(
                    f"Stage {cycle.stage.value} {side} trailing-order diagnostic requires attention: {details}"
                ),
                context=context,
                kind="native_order_anomaly",
            )
            return

        self._clear_audit_condition(
            anomaly_key,
            cycle=cycle,
            recovery_message=(
                "The selected price and raw Last no longer cross the displayed "
                "initial stop while the native order remains working."
            ),
            context=context,
        )
        self._update_audit_condition(
            key=wait_key,
            family=f"Stage {cycle.stage.value} {side} trailing-order wait",
            reason_code=reason_code,
            message=details,
            cycle=cycle,
            initial_delay_seconds=self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS,
            repeat_interval_seconds=self.NATIVE_ORDER_WAIT_SUMMARY_SECONDS,
            summary_level="INFO",
            first_summary_level="INFO",
            context=context,
            kind="native_order_wait",
        )

    def _handle_buy_order_poll(self, cycle: CycleState, polled: PolledOrderState) -> None:
        """Reconcile a BUY until the original broker order is terminal."""
        self.storage.update_order_status(polled.order_ref, polled.status, polled.order_id, polled.perm_id)
        self._update_recovery_probe_from_order_poll(polled)
        existing_buy_totals = self.storage.get_execution_totals(cycle.id, "BUY")
        known_buy_quantity = max(
            int(cycle.buy_filled_qty or 0),
            int(round(float(existing_buy_totals.get("shares", 0.0) or 0.0))),
        )
        if polled.filled <= 0 and known_buy_quantity <= 0:
            if self._order_terminal_without_fill(polled.status):
                if self._polled_order_rejection(polled) is not None:
                    self._move_no_fill_order_to_stopped_error(cycle, polled, "BUY")
                    return
                message = (
                    f"BUY order was cancelled with no filled quantity (status {polled.status}). "
                    "No position was opened. The strategy reset to Stage 1 and requires "
                    "a fresh initial-drop setup before another BUY."
                )
                reset_cycle = StrategyEngine.rollback_unsubmitted_order(cycle, "BUY", message)
                try:
                    latest = float((self.price_snapshot or {}).get("price") or reset_cycle.last_price or 0.0)
                    if latest > 0:
                        reset_cycle.last_price = latest
                        reset_cycle.anchor_price = latest
                        reset_cycle.drop_trigger_price = latest * (
                            1.0 - float(reset_cycle.initial_drop_pct) / 100.0
                        )
                except Exception:
                    pass
                reset_cycle.buy_status = polled.status
                reset_cycle.buy_remainder_cancel_requested = False
                reset_cycle.touch()
                self.active_cycle = reset_cycle
                self.storage.add_decision_event(
                    event_type="ORDER_TERMINAL_WITHOUT_FILL",
                    message=reset_cycle.error_message,
                    cycle=reset_cycle,
                    decision_result="reset_to_stage_1",
                    broker_order_id=polled.order_id,
                    perm_id=polled.perm_id,
                    raw=polled.raw,
                )
                self.storage.upsert_cycle(reset_cycle)
                self._log("WARN", reset_cycle.error_message, reset_cycle)
                return
            cycle.buy_status = polled.status
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log_native_order_wait_diagnostic(cycle, polled.status)
            return

        previous_qty = int(cycle.buy_filled_qty or 0)
        self._record_polled_executions(cycle, polled, "BUY")
        totals = self.storage.get_execution_totals(cycle.id, "BUY")
        persisted_qty = int(round(float(totals.get("shares", 0.0) or 0.0)))
        cumulative_qty = max(int(polled.filled), persisted_qty, previous_qty)
        persisted_avg = float(totals.get("avg_price", 0.0) or 0.0)
        cumulative_avg = (
            persisted_avg
            if persisted_qty >= cumulative_qty and persisted_avg > 0
            else float(polled.avg_fill_price or persisted_avg or cycle.avg_buy_price or 0.0)
        )
        cumulative_commission = max(
            float(polled.commission or 0.0),
            float(totals.get("commission", 0.0) or 0.0),
            float(cycle.buy_commission or 0.0),
        )
        next_cycle, actions = StrategyEngine.on_buy_fill(
            cycle,
            cumulative_qty,
            cumulative_avg,
            polled.status,
            cumulative_commission,
        )
        next_cycle.buy_order_id = polled.order_id or next_cycle.buy_order_id
        next_cycle.buy_perm_id = polled.perm_id or next_cycle.buy_perm_id

        terminal = str(polled.status or "").strip() in {
            "Filled",
            "Cancelled",
            "ApiCancelled",
            "Inactive",
            "Rejected",
        }
        cancel_action: Optional[StrategyAction] = None
        cancel_decision: Optional[dict[str, Any]] = None
        if not terminal:
            cancel_action, cancel_decision = self._buy_partial_remainder_cancel_action(
                next_cycle,
                polled,
                cumulative_qty,
            )
            if cancel_action is not None:
                actions.append(cancel_action)

        self.active_cycle = next_cycle
        self.storage.upsert_cycle(next_cycle)

        first_observed_buy_fill = previous_qty <= 0 and cumulative_qty > 0
        if first_observed_buy_fill:
            self._start_trade_market_data_capture("BUY_FILL", next_cycle, polled)

        if not terminal:
            if cumulative_qty != previous_qty:
                grace = max(0.1, float(self.BUY_PARTIAL_FILL_GRACE_SECONDS))
                elapsed = float((cancel_decision or {}).get("elapsed_seconds") or 0.0)
                remaining_grace = max(0.0, grace - elapsed)
                if cancel_action is None:
                    status_message = (
                        f"The triggered marketable BUY is being allowed up to {grace:.1f}s to finish normally; "
                        f"approximately {remaining_grace:.2f}s of grace remains."
                    )
                    decision_result = "awaiting_terminal_buy"
                else:
                    status_message = str(cancel_action.payload.get("reason") or "BUY remainder cancellation requested.")
                    decision_result = f"cancel_{(cancel_decision or {}).get('trigger') or 'requested'}"
                self.storage.add_decision_event(
                    event_type="BUY_PARTIAL_FILL",
                    message=f"BUY cumulative fill is {cumulative_qty}. {status_message}",
                    cycle=next_cycle,
                    stage_before=cycle.stage.value,
                    stage_after=next_cycle.stage.value,
                    decision_result=decision_result,
                    broker_order_id=polled.order_id,
                    perm_id=polled.perm_id,
                    raw={**dict(polled.raw or {}), "partial_fill_policy": dict(cancel_decision or {})},
                )
                self._log(
                    "INFO" if cancel_action is None else "WARN",
                    f"Partial BUY fill reconciled: {cumulative_qty} filled, {max(0, polled.remaining)} reported remaining. "
                    f"{status_message}",
                    next_cycle,
                )
            if first_observed_buy_fill:
                try:
                    self.storage.backup_database("after_buy_partial_fill")
                except Exception:
                    pass
            self._execute_actions(actions, next_cycle)
            return

        self.storage.add_decision_event(
            event_type="BUY_FILL",
            message="BUY order reached a terminal state and all cumulative fills were reconciled.",
            cycle=next_cycle,
            stage_before=cycle.stage.value,
            stage_after=next_cycle.stage.value,
            decision_result="fill_settled",
            broker_order_id=polled.order_id,
            perm_id=polled.perm_id,
            raw={**dict(polled.raw or {}), "cumulative_buy_quantity": cumulative_qty},
        )
        try:
            self.storage.backup_database("after_buy_fill")
        except Exception:
            pass
        self._log(
            "INFO",
            f"BUY settlement complete: {cumulative_qty} @ {cumulative_avg:.4f}. Moving to minimum-profit stage.",
            next_cycle,
        )
        self._execute_actions(actions, next_cycle)

    def _handle_protective_sell_order_poll(self, cycle: CycleState, polled: PolledOrderState) -> bool:
        """Poll the optional protective SELL order.

        Returns True whenever the caller must not continue with price-trigger
        logic in the same worker tick: full completion, an intermediate partial
        fill that is still working, or a fail-closed terminal quantity mismatch.
        """
        self.storage.update_order_status(polled.order_ref, polled.status, polled.order_id, polled.perm_id)
        self._update_recovery_probe_from_order_poll(polled)
        cycle.protective_sell_status = polled.status
        cycle.protective_sell_order_id = polled.order_id or cycle.protective_sell_order_id
        cycle.protective_sell_perm_id = polled.perm_id or cycle.protective_sell_perm_id
        if bool(getattr(cycle, "close_before_rth_liquidation_requested", False)):
            if polled.filled > 0:
                self._record_polled_executions(cycle, polled, "PROTECTIVE_SELL")
            sold_qty, avg_price, commission = self._close_before_rth_sell_totals(cycle)
            target_qty = max(0, int(cycle.buy_filled_qty or 0))
            if sold_qty > target_qty > 0:
                self._move_close_before_rth_to_error(
                    cycle,
                    f"protective and replacement executions report {sold_qty} SELL shares for "
                    f"an app-owned quantity of {target_qty}",
                )
                return True
            if sold_qty == target_qty and target_qty > 0 and avg_price > 0:
                self._complete_close_before_rth_sell(cycle, polled, sold_qty, avg_price, commission)
                return True
            terminal = str(polled.status or "").strip() in {
                "Filled",
                "Cancelled",
                "ApiCancelled",
                "Inactive",
                "Rejected",
            }
            cycle.protective_sell_filled_qty = int(
                round(
                    float(
                        self.storage.get_execution_totals(cycle.id, "PROTECTIVE_SELL").get(
                            "shares",
                            0.0,
                        )
                        or 0.0
                    )
                )
            )
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            if terminal:
                cycle.protective_sell_cancel_requested = False
                cycle.close_before_rth_cancel_requested = False
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
                if str(polled.status or "").strip() == "Filled" and sold_qty < target_qty:
                    self._move_close_before_rth_to_error(
                        cycle,
                        f"the protective SELL reported Filled but only {sold_qty} of {target_qty} "
                        "app-owned shares were confirmed sold",
                    )
                    return True
                self._submit_close_before_rth_market_sell(cycle)
                return True
            return True
        if polled.filled <= 0:
            if self._order_terminal_without_fill(polled.status):
                if bool(getattr(cycle, "close_position_market_requested", False)):
                    cycle.protective_sell_cancel_requested = False
                    cycle.protective_sell_status = polled.status
                    cycle.touch()
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                    return self._submit_requested_market_close(cycle)
                # If this was a requested protective-to-profit cancel, the
                # strategy may continue and place the final SELL trail on the
                # next price tick. Other terminal protective states pause in ERROR.
                if cycle.protective_sell_cancel_requested and polled.status in {"Cancelled", "ApiCancelled"}:
                    cycle.protective_sell_cancel_requested = False
                    cycle.protective_sell_status = polled.status
                    cycle.touch()
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                    return False
                self._move_no_fill_order_to_stopped_error(cycle, polled, "PROTECTIVE_SELL")
                return True
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            return False
        status = str(polled.status or "").strip()
        terminal = status in {
            "Filled",
            "Cancelled",
            "ApiCancelled",
            "Inactive",
            "Rejected",
        }
        target_qty = max(0, int(cycle.buy_filled_qty or 0))
        filled_qty = max(0, int(polled.filled or 0))
        remaining_qty = max(0, int(polled.remaining or 0))
        terminal_partial = terminal and target_qty > 0 and 0 < filled_qty < target_qty
        quantity_mismatch = (
            target_qty <= 0
            or filled_qty > target_qty
            or (remaining_qty == 0 and filled_qty != target_qty)
            # A terminal order that still reports a remainder while the
            # app-owned quantity is already fully sold cannot be waited on:
            # the status is final, so the partial-fill branch below would
            # re-persist the same facts on every poll and never complete or
            # error. Treat it as an unprovable quantity instead.
            or (terminal and remaining_qty > 0 and filled_qty >= target_qty)
        )
        if terminal_partial or quantity_mismatch:
            # A protective exit may complete the cycle only when the broker's
            # cumulative fill exactly matches the app-owned position and no
            # remainder is still working. Any terminal partial or inconsistent
            # quantity is unsafe to reinterpret as a completed exit.
            stage_before = cycle.stage.value
            self._record_polled_executions(cycle, polled, "PROTECTIVE_SELL")
            cycle.protective_sell_filled_qty = filled_qty
            if float(polled.avg_fill_price or 0.0) > 0:
                cycle.protective_avg_sell_price = float(polled.avg_fill_price)
            cycle.protective_sell_cancel_requested = False
            cycle.stage = Stage.ERROR
            if terminal_partial:
                event_type = "PROTECTIVE_SELL_PARTIAL_TERMINAL"
                cycle.error_message = (
                    f"The protective SELL order ended with status {polled.status} after "
                    f"{filled_qty} of {target_qty} app-owned shares were sold; "
                    f"{max(0, target_qty - filled_qty)} shares remain without a working exit order. "
                    "Trading is paused for manual review."
                )
            else:
                event_type = "PROTECTIVE_SELL_QUANTITY_MISMATCH"
                cycle.error_message = (
                    f"The protective SELL order reported {filled_qty} filled and {remaining_qty} remaining "
                    f"for {target_qty} app-owned shares with status {polled.status}. "
                    "The broker quantity does not prove that the app-owned position is exactly closed, so trading is paused for manual review."
                )
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.add_decision_event(
                event_type=event_type,
                message=cycle.error_message,
                cycle=cycle,
                stage_before=stage_before,
                stage_after=cycle.stage.value,
                decision_result="stopped_error",
                broker_order_id=polled.order_id,
                perm_id=polled.perm_id,
                raw=polled.raw,
            )
            self._log("ERROR", cycle.error_message, cycle)
            return True
        if remaining_qty > 0:
            # An intermediate cumulative fill during a live multi-print market
            # execution. Mirror the final-SELL handler: persist the partial
            # facts and wait for IBKR's terminal status instead of completing
            # the cycle while shares are still working at the broker.
            self._record_polled_executions(cycle, polled, "PROTECTIVE_SELL")
            previous_qty = int(cycle.protective_sell_filled_qty or 0)
            cycle.protective_sell_filled_qty = filled_qty
            if float(polled.avg_fill_price or 0.0) > 0:
                cycle.protective_avg_sell_price = float(polled.avg_fill_price)
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            if filled_qty != previous_qty:
                self._log(
                    "INFO",
                    f"Partial protective SELL fill detected: {filled_qty} filled, "
                    f"{remaining_qty} remaining. Waiting for the protective order "
                    "to reach a terminal status.",
                    cycle,
                )
            return True
        self._record_polled_executions(cycle, polled, "PROTECTIVE_SELL")
        next_cycle = StrategyEngine.on_protective_sell_fill(cycle, polled.filled, polled.avg_fill_price, polled.status, polled.commission)
        self.active_cycle = next_cycle
        self.storage.upsert_cycle(next_cycle)
        self.storage.add_decision_event(event_type="PROTECTIVE_SELL_FILL", message="Protective SELL filled.", cycle=next_cycle, stage_before=cycle.stage.value, stage_after=next_cycle.stage.value, decision_result="cycle_complete", raw=polled.raw)
        self._start_trade_market_data_capture("PROTECTIVE_SELL_FILL", next_cycle, polled)
        try:
            self.storage.backup_database("after_protective_sell_fill")
        except Exception:
            pass
        self._log("WARN", f"Protective SELL filled: {polled.filled} @ {polled.avg_fill_price:.4f}. Net P/L {next_cycle.net_pnl:.2f}.", next_cycle)
        self._maybe_start_next_cycle()
        return True

    def _move_close_before_rth_to_error(self, cycle: CycleState, message: str) -> None:
        """Pause an incomplete automatic close without sending another order."""
        cycle.stage = Stage.ERROR
        cycle.close_position_market_requested = False
        cycle.close_before_rth_liquidation_requested = False
        cycle.close_before_rth_cancel_requested = False
        cycle.recovery_required = False
        cycle.error_message = (
            f"Close-before-RTH liquidation requires manual review: {message} "
            "No outside-RTH replacement order was submitted."
        )
        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        self.storage.add_decision_event(
            event_type="RTH_CLOSE_LIQUIDATION_ERROR",
            message=cycle.error_message,
            cycle=cycle,
            stage_after=cycle.stage.value,
            decision_result="stopped_error",
            broker_order_id=cycle.sell_order_id,
            perm_id=cycle.sell_perm_id,
            raw={"order_ref": cycle.sell_order_ref, "sell_status": cycle.sell_status},
        )
        self._log("ERROR", cycle.error_message, cycle)

    def _close_before_rth_sell_totals(self, cycle: CycleState) -> tuple[int, float, float]:
        normal = self.storage.get_execution_totals(cycle.id, "SELL")
        protective = self.storage.get_execution_totals(cycle.id, "PROTECTIVE_SELL")
        normal_qty = float(normal.get("shares", 0.0) or 0.0)
        protective_qty = float(protective.get("shares", 0.0) or 0.0)
        total_qty = normal_qty + protective_qty
        total_notional = (
            normal_qty * float(normal.get("avg_price", 0.0) or 0.0)
            + protective_qty * float(protective.get("avg_price", 0.0) or 0.0)
        )
        return (
            int(round(total_qty)),
            (total_notional / total_qty) if total_qty > 0 else 0.0,
            float(normal.get("commission", 0.0) or 0.0)
            + float(protective.get("commission", 0.0) or 0.0),
        )

    def _complete_close_before_rth_sell(
        self,
        cycle: CycleState,
        polled: PolledOrderState,
        quantity: int,
        avg_price: float,
        commission: float,
    ) -> None:
        completed = StrategyEngine.on_sell_fill(cycle, quantity, avg_price, "Filled", commission)
        completed.sell_order_id = polled.order_id or completed.sell_order_id
        completed.sell_perm_id = polled.perm_id or completed.sell_perm_id
        self.active_cycle = completed
        self.storage.upsert_cycle(completed)
        self.storage.add_decision_event(
            event_type="SELL_FILL",
            message="Close-before-RTH SELL fills fully closed the app-owned position.",
            cycle=completed,
            stage_before=cycle.stage.value,
            stage_after=completed.stage.value,
            decision_result="cycle_complete",
            broker_order_id=polled.order_id,
            perm_id=polled.perm_id,
            raw={**dict(polled.raw or {}), "cumulative_cycle_sell_quantity": quantity},
        )
        self._start_trade_market_data_capture("SELL_FILL", completed, polled)
        try:
            self.storage.backup_database("after_sell_fill")
        except Exception:
            pass
        self._log(
            "INFO",
            f"Close-before-RTH liquidation completed: {quantity} cumulative shares sold @ {avg_price:.4f}. "
            f"Net P/L {completed.net_pnl:.2f}.",
            completed,
        )
        self._maybe_start_next_cycle()

    def _submit_close_before_rth_market_sell(self, cycle: CycleState) -> bool:
        """Submit an RTH-only DAY market SELL after any prior exit is terminal."""
        stage3_profit_exit = cycle.stage == Stage.WAIT_RISE_TRIGGER
        avg_buy = float(cycle.avg_buy_price or 0.0)
        field_tracking = bool((self.price_snapshot or {}).get("market_data_field_tracking"))
        stage3_quote_evidence: Optional[dict[str, Any]] = None
        stage3_quote_message = ""
        if stage3_profit_exit and field_tracking:
            stage3_quote_evidence, stage3_quote_message = self._stage3_sell_quote_evidence(
                cycle,
                require_latest_event=False,
                require_trigger=False,
            )
            reference_price = (
                float(stage3_quote_evidence.get("bid") or 0.0)
                if stage3_quote_evidence is not None
                else 0.0
            )
        else:
            # Compatibility fallback for deterministic/offline adapters that
            # predate field-level market-data identity. The production adapter
            # always takes the independently fresh bid/ask path above.
            reference_price = float((self.price_snapshot or {}).get("price") or cycle.last_price or 0.0)

        protective_was_cancelled = bool(cycle.protective_sell_order_ref) and str(
            cycle.protective_sell_status or ""
        ) in {"Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        if stage3_profit_exit and field_tracking and stage3_quote_evidence is None:
            detail = stage3_quote_message or "the current Stage-3 bid/ask quote is unavailable"
            if protective_was_cancelled:
                self._move_close_before_rth_to_error(
                    cycle,
                    "the Stage-3 protective SELL became terminal, but a fresh complete executable quote "
                    f"could not be revalidated ({detail})",
                )
            else:
                cycle.close_before_rth_liquidation_requested = False
                cycle.close_before_rth_cancel_requested = False
                cycle.error_message = None
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
            return False
        if stage3_profit_exit and (reference_price <= 0 or avg_buy <= 0 or reference_price <= avg_buy):
            if protective_was_cancelled:
                self._move_close_before_rth_to_error(
                    cycle,
                    "the Stage-3 protective SELL became terminal, but the executable SELL bid is no longer "
                    f"strictly above the average BUY price ({reference_price:.4f} versus {avg_buy:.4f})",
                )
            else:
                cycle.close_before_rth_liquidation_requested = False
                cycle.close_before_rth_cancel_requested = False
                cycle.error_message = None
                cycle.touch()
                self.active_cycle = cycle
                self.storage.upsert_cycle(cycle)
            return False
        if self.contract is None or self.contract.ticker != cycle.ticker:
            try:
                self.contract = self._adapter_qualify_stock(
                    cycle.ticker,
                    cycle.exchange,
                    cycle.currency,
                    cycle.primary_exchange,
                    cycle.con_id,
                )
            except Exception as exc:
                self._move_close_before_rth_to_error(
                    cycle,
                    f"the prior exit order was terminal, but the contract could not be qualified for the market SELL ({exc})",
                )
                return False

        self._update_rth_status(self.contract)
        timing = self._session_minutes_from_rth_status()
        minutes_to_close = timing.get("minutes_to_close") if timing.get("available") else None
        rth_open = bool((self._latest_rth_status or {}).get("is_open"))
        if minutes_to_close is None or not rth_open or float(minutes_to_close) <= 0:
            self._move_close_before_rth_to_error(
                cycle,
                "an open regular session with time remaining before the close could not be confirmed",
            )
            return False

        connectivity_message = self._order_submission_connectivity_message("SELL")
        if connectivity_message:
            self._move_close_before_rth_to_error(
                cycle,
                f"the market SELL could not be submitted ({connectivity_message})",
            )
            return False

        sold_qty, _, _ = self._close_before_rth_sell_totals(cycle)
        target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
        remaining = target_qty - sold_qty
        if target_qty <= 0 or remaining <= 0:
            self._move_close_before_rth_to_error(
                cycle,
                "the remaining app-owned quantity could not be established safely before replacement submission",
            )
            return False

        if stage3_profit_exit and field_tracking:
            refreshed, message = self._stage3_sell_quote_evidence(
                cycle,
                require_latest_event=False,
                require_trigger=False,
            )
            if refreshed is None:
                detail = message or "the current Stage-3 bid/ask quote is unavailable"
                if protective_was_cancelled:
                    self._move_close_before_rth_to_error(
                        cycle,
                        "the Stage-3 protective SELL became terminal, but the executable quote became invalid "
                        f"before the market-order intent was recorded ({detail})",
                    )
                else:
                    cycle.close_before_rth_liquidation_requested = False
                    cycle.close_before_rth_cancel_requested = False
                    cycle.error_message = None
                    cycle.touch()
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                return False
            stage3_quote_evidence = refreshed
            reference_price = float(refreshed.get("bid") or 0.0)
            if reference_price <= 0 or avg_buy <= 0 or reference_price <= avg_buy:
                if protective_was_cancelled:
                    self._move_close_before_rth_to_error(
                        cycle,
                        "the Stage-3 protective SELL became terminal, but the executable SELL bid was no longer "
                        f"strictly above the average BUY price before intent ({reference_price:.4f} versus {avg_buy:.4f})",
                    )
                else:
                    cycle.close_before_rth_liquidation_requested = False
                    cycle.close_before_rth_cancel_requested = False
                    cycle.error_message = None
                    cycle.touch()
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                return False

        order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "RTH_CLOSE_SELL_MARKET")
        stage_before = cycle.stage.value
        cycle.sell_order_ref = order_ref
        cycle.sell_order_id = None
        cycle.sell_perm_id = None
        cycle.sell_status = None
        cycle.sell_initial_trail_stop_price = None
        cycle.close_before_rth_cancel_requested = False
        cycle.error_message = f"Submitting DAY market SELL for {remaining} remaining app-owned shares before the regular-session close."
        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)

        payload = {
            "ticker": cycle.ticker,
            "quantity": int(remaining),
            "order_type": "MKT",
            "trailing_percent": 0.0,
            "initial_stop_price": None,
            "reference_price": reference_price,
            "order_ref": order_ref,
            "tif": "DAY",
            "outside_rth": False,
            "automatic_close_before_rth": True,
        }
        if stage3_quote_evidence is not None:
            payload["stage3_market_data_guard"] = dict(stage3_quote_evidence)
        try:
            self.storage.backup_database("before_order_submit")
        except Exception:
            pass
        self._record_order_intent(cycle, payload, "SELL", "MKT")
        if self._broker_mutation_blocked_by_storage_fault(
            "Close-before-RTH market SELL submission",
            cycle,
        ):
            return False
        if stage3_profit_exit and field_tracking:
            refreshed, message = self._stage3_sell_quote_evidence(
                cycle,
                require_latest_event=False,
                require_trigger=False,
            )
            try:
                min_tick = max(1e-9, float(getattr(self.contract, "min_tick", 0.0) or 0.0))
            except Exception:
                min_tick = 1e-9
            quote_changed = False
            if refreshed is not None and stage3_quote_evidence is not None:
                quote_changed = any(
                    str(refreshed.get(key)) != str(stage3_quote_evidence.get(key))
                    for key in (
                        "cycle_id",
                        "subscription_id",
                        "sequence",
                        "quote_sequence",
                        "quote_kind",
                        "selected_price_source",
                        "selected_price_basis",
                        "selected_price_basis_sequence",
                    )
                )
                try:
                    quote_changed = quote_changed or abs(
                        float(refreshed.get("bid") or 0.0) - reference_price
                    ) > min_tick
                    quote_changed = quote_changed or abs(
                        float(refreshed.get("ask") or 0.0)
                        - float(stage3_quote_evidence.get("ask") or 0.0)
                    ) > min_tick
                except Exception:
                    quote_changed = True
            if (
                refreshed is None
                or quote_changed
                or float(refreshed.get("bid") or 0.0) <= avg_buy
            ):
                detail = message or (
                    "the executable quote identity or bid changed between durable intent and broker submission"
                )
                try:
                    self.storage.mark_order_intent_failed(
                        order_ref,
                        detail,
                        raw={"payload": payload, "side": "SELL", "reason": "stage3_quote_revalidation"},
                    )
                except Exception:
                    pass
                if protective_was_cancelled:
                    self._move_close_before_rth_to_error(
                        cycle,
                        "the Stage-3 protective SELL became terminal, but the executable quote could not be "
                        f"revalidated immediately before market-order submission ({detail})",
                    )
                else:
                    rolled_back = StrategyEngine.rollback_unsubmitted_order(cycle, "SELL", detail)
                    rolled_back.close_before_rth_liquidation_requested = False
                    rolled_back.close_before_rth_cancel_requested = False
                    self.active_cycle = rolled_back
                    self.storage.upsert_cycle(rolled_back)
                return False
        try:
            handle = self.adapter.place_market_order(
                contract=self.contract,
                action="SELL",
                quantity=int(remaining),
                order_ref=order_ref,
                tif="DAY",
                account=(self.connection.account or cycle.account),
                outside_rth=False,
            )
        except BrokerAdapterError as exc:
            self.storage.mark_order_intent_failed(order_ref, str(exc), raw={"payload": payload, "side": "SELL"})
            self._move_close_before_rth_to_error(
                cycle,
                f"IBKR did not confirm the DAY market SELL submission ({exc})",
            )
            try:
                if not self.adapter.is_connected():
                    self._handle_broker_connection_problem(exc)
            except Exception:
                pass
            return False
        except Exception as exc:
            self.storage.mark_order_intent_failed(order_ref, str(exc), raw={"payload": payload, "side": "SELL"})
            self._move_close_before_rth_to_error(cycle, f"the DAY market SELL submission failed ({exc})")
            return False

        updated = StrategyEngine.on_order_submitted(
            cycle,
            handle.order_ref,
            handle.order_id,
            handle.perm_id,
            handle.status,
        )
        self.active_cycle = updated
        self.storage.record_order_submission(
            cycle=updated,
            order_ref=handle.order_ref,
            order_id=handle.order_id,
            perm_id=handle.perm_id,
            status=handle.status,
            raw=handle.raw,
        )
        self.storage.add_decision_event(
            event_type="RTH_CLOSE_MARKET_SUBMITTED",
            message=(
                "Submitted the profitable Stage-3 close-before-RTH DAY market SELL."
                if stage3_profit_exit
                else "Submitted the close-before-RTH DAY market SELL after final SELL-trail cancellation confirmation."
            ),
            cycle=updated,
            stage_before=stage_before,
            stage_after=updated.stage.value,
            decision_result="submitted",
            broker_order_id=handle.order_id,
            perm_id=handle.perm_id,
            raw={
                **dict(handle.raw or {}),
                "configured_minutes": int(cycle.liquidate_before_close_minutes),
                "stage3_profit_condition": stage3_profit_exit,
                "reference_price": reference_price,
                "executable_bid": reference_price if stage3_profit_exit else None,
                "stage3_market_data_guard": dict(stage3_quote_evidence or {}),
                "average_buy_price": avg_buy,
                "commissions_ignored": stage3_profit_exit,
            },
        )
        self._log(
            "WARN",
            f"Close-before-RTH liquidation submitted SELL MKT DAY for {remaining} remaining app-owned shares; outsideRth=False.",
            updated,
        )
        return True

    def _handle_close_before_rth_sell_order_poll(self, cycle: CycleState, polled: PolledOrderState) -> None:
        """Handle original-trail and replacement fills without allowing a double SELL."""
        self.storage.update_order_status(polled.order_ref, polled.status, polled.order_id, polled.perm_id)
        self._update_recovery_probe_from_order_poll(polled)
        previous_qty = int(getattr(cycle, "sell_filled_qty", 0) or 0)
        cycle.sell_status = polled.status
        cycle.sell_order_id = polled.order_id or cycle.sell_order_id
        cycle.sell_perm_id = polled.perm_id or cycle.sell_perm_id
        if polled.filled > 0:
            self._record_polled_executions(cycle, polled, "SELL")

        total_qty, avg_price, commission = self._close_before_rth_sell_totals(cycle)
        target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
        if total_qty > 0:
            cycle.sell_filled_qty = total_qty
            cycle.avg_sell_price = avg_price
            cycle.sell_commission = commission
            cycle.sell_filled_at = cycle.sell_filled_at or utc_now_iso()

        if target_qty <= 0:
            self._move_close_before_rth_to_error(cycle, "the original bought quantity is missing or invalid")
            return
        if total_qty > target_qty:
            self._move_close_before_rth_to_error(
                cycle,
                f"broker executions report {total_qty} SELL shares for an app-owned quantity of {target_qty}; a possible over-sell must be reviewed",
            )
            return
        if total_qty == target_qty and avg_price > 0:
            self._complete_close_before_rth_sell(cycle, polled, total_qty, avg_price, commission)
            return

        cycle.touch()
        self.active_cycle = cycle
        self.storage.upsert_cycle(cycle)
        terminal = str(polled.status or "").strip() in {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}
        replacement = self._is_close_before_rth_market_order_ref(polled.order_ref or cycle.sell_order_ref)

        if replacement:
            if terminal:
                remaining = max(0, target_qty - total_qty)
                self._move_close_before_rth_to_error(
                    cycle,
                    f"the DAY market SELL ended with status {polled.status} after {total_qty} cumulative shares were sold; "
                    f"{remaining} app-owned shares remain unsold",
                )
                return
            if total_qty != previous_qty:
                self._log(
                    "INFO",
                    f"Close-before-RTH market SELL partial fill: {total_qty} cumulative shares sold; "
                    f"{target_qty - total_qty} app-owned shares remain.",
                    cycle,
                )
            return

        if terminal:
            cycle.close_before_rth_cancel_requested = False
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            if str(polled.status or "").strip() == "Filled":
                self._move_close_before_rth_to_error(
                    cycle,
                    f"the original SELL trail reported Filled but only {total_qty} of {target_qty} app-owned shares were confirmed sold",
                )
                return
            self._submit_close_before_rth_market_sell(cycle)
            return

        if total_qty != previous_qty:
            self._log(
                "INFO",
                f"Final SELL trail partially filled during close-before-RTH cancellation: {total_qty} cumulative shares sold; "
                f"{target_qty - total_qty} app-owned shares remain.",
                cycle,
            )

    def _handle_sell_order_poll(self, cycle: CycleState, polled: PolledOrderState) -> None:
        if bool(getattr(cycle, "close_before_rth_liquidation_requested", False)) or self._is_close_before_rth_market_order_ref(
            polled.order_ref or cycle.sell_order_ref
        ):
            self._handle_close_before_rth_sell_order_poll(cycle, polled)
            return
        self.storage.update_order_status(polled.order_ref, polled.status, polled.order_id, polled.perm_id)
        self._update_recovery_probe_from_order_poll(polled)
        if polled.filled <= 0:
            if self._order_terminal_without_fill(polled.status):
                is_forced_close = "FORCED_SELL_MARKET" in str(polled.order_ref or cycle.sell_order_ref or "")
                if bool(getattr(cycle, "close_position_market_requested", False)) and not is_forced_close:
                    cycle.sell_status = polled.status
                    cycle.touch()
                    self.active_cycle = cycle
                    self.storage.upsert_cycle(cycle)
                    self._submit_requested_market_close(cycle)
                    return
                if is_forced_close:
                    cycle.close_position_market_requested = False
                self._move_no_fill_order_to_stopped_error(cycle, polled, "SELL")
                return
            cycle.sell_status = polled.status
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self._log_native_order_wait_diagnostic(cycle, polled.status)
            return
        terminal = str(polled.status or "").strip() in {
            "Filled",
            "Cancelled",
            "ApiCancelled",
            "Inactive",
            "Rejected",
        }
        target_qty = max(0, int(getattr(cycle, "buy_filled_qty", 0) or 0))
        order_filled_qty = max(0, int(polled.filled or 0))
        remaining_qty = max(0, int(polled.remaining or 0))
        previous_filled_qty = max(0, int(getattr(cycle, "sell_filled_qty", 0) or 0))

        # A manual close order may deliberately be sized only for the unsold
        # remainder after an earlier partial SELL. Persist the current order's
        # cumulative facts first, then validate and complete against all SELL
        # executions attributed to this cycle rather than against this one
        # order's quantity alone. This also keeps weighted price/commission and
        # P/L correct across cancel-and-replace exits.
        self._record_polled_executions(cycle, polled, "SELL")
        aggregate_filled_qty, aggregate_avg_price, aggregate_commission = self._close_before_rth_sell_totals(cycle)

        terminal_partial = terminal and target_qty > 0 and 0 < aggregate_filled_qty < target_qty
        quantity_mismatch = (
            target_qty <= 0
            # Selling more than the cycle ever bought is anomalous. A broker
            # order that still reports a working remainder after aggregate
            # cycle fills already reached the app-owned quantity is likewise
            # unsafe because another print could create a short position.
            or aggregate_filled_qty > target_qty
            or (remaining_qty > 0 and aggregate_filled_qty >= target_qty)
            # A zero remainder means this order can no longer close an
            # aggregate quantity shortfall. Do not reinterpret a smaller
            # forced-close order as proof that the complete cycle is closed.
            or (remaining_qty == 0 and aggregate_filled_qty != target_qty)
        )
        fill_price_missing = (
            remaining_qty == 0
            and aggregate_filled_qty == target_qty
            and aggregate_avg_price <= 0
        )
        if terminal_partial or quantity_mismatch or fill_price_missing:
            # The final SELL may complete a cycle only when the broker's
            # persisted cumulative executions exactly match the app-owned
            # position, no remainder is still working, and a fill price is
            # available. A terminal partial, over-fill, missing historical
            # execution, or contradictory remainder therefore pauses trading
            # fail-closed for manual review.
            stage_before = cycle.stage.value
            cycle.sell_status = polled.status
            cycle.sell_filled_qty = aggregate_filled_qty
            if aggregate_avg_price > 0:
                cycle.avg_sell_price = aggregate_avg_price
            cycle.sell_commission = aggregate_commission
            cycle.stage = Stage.ERROR
            if terminal_partial:
                event_type = "SELL_PARTIAL_TERMINAL"
                cycle.error_message = (
                    f"The SELL order ended with status {polled.status} after "
                    f"{aggregate_filled_qty} of {target_qty} app-owned shares were sold; "
                    f"{max(0, target_qty - aggregate_filled_qty)} shares remain without a working exit order. "
                    "Trading is paused for manual review."
                )
            else:
                event_type = "SELL_QUANTITY_MISMATCH"
                cycle.error_message = (
                    f"The SELL order reported {order_filled_qty} filled and {remaining_qty} remaining; "
                    f"persisted cycle executions total {aggregate_filled_qty} of {target_qty} app-owned shares "
                    f"at an average price of {aggregate_avg_price:.4f}, with status {polled.status}. "
                    "The broker facts do not prove that the app-owned position is exactly closed, so trading is paused for manual review."
                )
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            self.storage.add_decision_event(
                event_type=event_type,
                message=cycle.error_message,
                cycle=cycle,
                stage_before=stage_before,
                stage_after=cycle.stage.value,
                decision_result="stopped_error",
                broker_order_id=polled.order_id,
                perm_id=polled.perm_id,
                raw={
                    **dict(polled.raw or {}),
                    "order_cumulative_filled": order_filled_qty,
                    "order_remaining": remaining_qty,
                    "cycle_cumulative_sell_quantity": aggregate_filled_qty,
                    "cycle_cumulative_sell_avg_price": aggregate_avg_price,
                    "cycle_cumulative_sell_commission": aggregate_commission,
                },
            )
            self._log("ERROR", cycle.error_message, cycle)
            return
        if remaining_qty > 0:
            # A non-terminal cumulative fill: persist the partial facts and let
            # the order run to a terminal status before completing the cycle.
            cycle.sell_status = polled.status
            cycle.sell_filled_qty = aggregate_filled_qty
            if aggregate_avg_price > 0:
                cycle.avg_sell_price = aggregate_avg_price
            cycle.sell_commission = aggregate_commission
            cycle.touch()
            self.active_cycle = cycle
            self.storage.upsert_cycle(cycle)
            if aggregate_filled_qty != previous_filled_qty:
                self._log(
                    "INFO",
                    f"Partial SELL fill detected: {order_filled_qty} filled on the current order, "
                    f"{remaining_qty} remaining; {aggregate_filled_qty} of {target_qty} app-owned shares "
                    "are confirmed sold across the cycle.",
                    cycle,
                )
            return
        next_cycle = StrategyEngine.on_sell_fill(
            cycle,
            aggregate_filled_qty,
            aggregate_avg_price,
            polled.status,
            aggregate_commission,
        )
        self.active_cycle = next_cycle
        self.storage.upsert_cycle(next_cycle)
        self.storage.add_decision_event(
            event_type="SELL_FILL",
            message="SELL fills exactly closed the app-owned cycle position.",
            cycle=next_cycle,
            stage_before=cycle.stage.value,
            stage_after=next_cycle.stage.value,
            decision_result="cycle_complete",
            broker_order_id=polled.order_id,
            perm_id=polled.perm_id,
            raw={
                **dict(polled.raw or {}),
                "order_cumulative_filled": order_filled_qty,
                "cycle_cumulative_sell_quantity": aggregate_filled_qty,
                "cycle_cumulative_sell_avg_price": aggregate_avg_price,
                "cycle_cumulative_sell_commission": aggregate_commission,
            },
        )
        self._start_trade_market_data_capture("SELL_FILL", next_cycle, polled)
        try:
            self.storage.backup_database("after_sell_fill")
        except Exception:
            pass
        self._log(
            "INFO",
            f"SELL settlement complete: {aggregate_filled_qty} @ {aggregate_avg_price:.4f}. "
            f"Net P/L {next_cycle.net_pnl:.2f}.",
            next_cycle,
        )
        self._maybe_start_next_cycle()

    def _record_polled_executions(self, cycle: CycleState, polled: PolledOrderState, side: str) -> None:
        """Persist broker executions without double-counting late callbacks.

        A stable residual placeholder represents only the portion of the
        cumulative order-status fill not yet backed by individual execution
        identifiers.  Late execDetails callbacks shrink or remove it.
        """
        side_value = str(side or "").upper()
        if polled.executions:
            for index, execution in enumerate(polled.executions):
                exec_id = str(
                    execution.get("execId")
                    or execution.get("execution_id")
                    or (
                        f"{polled.order_ref}|{side_value}|DETAIL|{index}|{execution.get('shares')}|"
                        f"{execution.get('price')}|{execution.get('time')}"
                    )
                )
                self._executions_recorded.add(exec_id)
                commission_value = execution.get("commission")
                try:
                    commission = (
                        float(commission_value)
                        if commission_value not in (None, "")
                        else None
                    )
                except Exception:
                    commission = None
                if commission is not None and abs(commission) > 0.0:
                    commission = self._commission_in_cycle_currency(
                        cycle,
                        commission,
                        execution.get("currency"),
                        execution_id=exec_id,
                        source="ORDER_POLL_EXECUTION",
                    )
                self.storage.upsert_execution(
                    cycle=cycle,
                    ticker=cycle.ticker,
                    side=side_value,
                    shares=float(execution.get("shares") or 0.0),
                    price=float(execution.get("price") or polled.avg_fill_price or 0.0),
                    avg_price=float(execution.get("avgPrice") or execution.get("avg_price") or polled.avg_fill_price or 0.0),
                    commission=commission,
                    currency=cycle.currency,
                    order_ref=polled.order_ref,
                    order_id=polled.order_id,
                    perm_id=polled.perm_id,
                    execution_id=exec_id,
                    executed_at=str(execution.get("executed_at") or execution.get("time") or utc_now_iso()),
                    raw=execution,
                )

        if polled.filled > 0:
            cumulative_commission = float(polled.commission or 0.0)
            commission_currencies = {
                normalize_contract_currency(value, fallback="")
                for value in list((polled.raw or {}).get("commission_currencies") or [])
                if normalize_contract_currency(value, fallback="")
            }
            mismatched_currencies = sorted(
                value
                for value in commission_currencies
                if value != normalize_contract_currency(cycle.currency, fallback="")
            )
            if cumulative_commission and mismatched_currencies:
                self._commission_in_cycle_currency(
                    cycle,
                    cumulative_commission,
                    mismatched_currencies[0],
                    execution_id=f"{polled.order_ref}|CUMULATIVE",
                    source="ORDER_POLL_CUMULATIVE",
                )
                cumulative_commission = 0.0
            self.storage.reconcile_cumulative_execution_placeholder(
                cycle=cycle,
                side=side_value,
                order_ref=polled.order_ref,
                cumulative_shares=float(polled.filled),
                cumulative_avg_price=float(polled.avg_fill_price or 0.0),
                cumulative_commission=cumulative_commission,
                order_id=polled.order_id,
                perm_id=polled.perm_id,
                currency=cycle.currency,
                executed_at=utc_now_iso(),
                raw={
                    "source": "broker_order_status_cumulative",
                    "status": polled.status,
                    "order_raw": dict(polled.raw or {}),
                },
            )

    def _maybe_start_next_cycle(self) -> None:
        cycle = self.active_cycle
        if cycle is None or cycle.stage != Stage.CYCLE_COMPLETE:
            return
        if cycle.stop_after_current_cycle or not self.strategy.auto_repeat:
            self._log("INFO", f"Cycle complete for {cycle.ticker}. Auto-repeat is stopped.", cycle)
            return
        if bool(getattr(cycle, "hard_risk_limits_enabled", False)) and int(getattr(cycle, "max_cycles_per_ticker_day", 0) or 0) > 0:
            max_cycles = int(cycle.max_cycles_per_ticker_day)
            completed = self.storage.get_completed_cycle_count(cycle.ticker, con_id=cycle.con_id)
            if completed >= max_cycles:
                self._log(
                    "INFO",
                    f"Cycle complete for {cycle.ticker}. Max cycles reached ({completed}/{max_cycles}); auto-repeat is stopped.",
                    cycle,
                )
                return
        if self.contract is None:
            self.contract = self._adapter_qualify_stock(cycle.ticker, cycle.exchange, cycle.currency, cycle.primary_exchange, cycle.con_id)
        self.adapter.set_market_data_type(self.connection.market_data_type)
        self._update_rth_status(self.contract)
        price_snapshot = self.adapter.price_snapshot(self.contract, timeout=8.0)
        self._record_price_snapshot(price_snapshot, self.contract)
        last_price = price_snapshot.price if bool((self.price_snapshot or {}).get("strategy_price_usable")) else None
        realized = self.storage.get_realized_net_profit_for_ticker(cycle.ticker, con_id=cycle.con_id)
        next_number = self.storage.get_next_cycle_number(cycle.ticker)
        repeat_settings = self._settings_for_repeat_cycle(cycle)
        if last_price is None or last_price <= 0:
            next_cycle = StrategyEngine.start_cycle_waiting_for_price(repeat_settings, next_number, self.connection.account, realized)
            log_level = "WARN"
            message = (
                f"Auto-started next cycle {next_cycle.cycle_number} for {next_cycle.ticker}, "
                "but no usable market price is available yet. Waiting for the first price tick."
            )
        else:
            next_cycle = StrategyEngine.start_cycle(repeat_settings, next_number, self.connection.account, last_price, realized)
            log_level = "INFO"
            message = f"Auto-started next cycle {next_cycle.cycle_number} for {next_cycle.ticker}."
        next_cycle.con_id = cycle.con_id
        self.active_cycle = next_cycle
        self.storage.upsert_cycle(next_cycle)
        self._log(log_level, message, next_cycle)

    def _settings_for_repeat_cycle(self, cycle: CycleState) -> StrategySettings:
        """Create next-cycle settings from the completed cycle.

        GUI fields can be edited while a cycle is running. Auto-repeat should
        continue the completed ticker with the cycle's active parameters, not
        accidentally switch ticker because draft settings were changed.
        """
        return StrategySettings(
            ticker=cycle.ticker,
            investment_amount=cycle.investment_amount,
            initial_drop_pct=cycle.initial_drop_pct,
            buy_rebound_trail_pct=cycle.buy_rebound_trail_pct,
            rise_trigger_pct=cycle.rise_trigger_pct,
            sell_trailing_stop_pct=cycle.sell_trailing_stop_pct,
            atr_adaptive_enabled=bool(getattr(cycle, "atr_adaptive_enabled", True)),
            atr_adapt_minimum_profit_enabled=bool(getattr(cycle, "atr_adapt_minimum_profit_enabled", True)),
            atr_block_new_buy_until_ready=bool(getattr(cycle, "atr_block_new_buy_until_ready", True)),
            atr_adapt_protective_sell_enabled=bool(getattr(cycle, "atr_adapt_protective_sell_enabled", False)),
            atr_protective_sell_multiplier=float(getattr(cycle, "atr_protective_sell_multiplier", 3.0)),
            atr_period=int(getattr(cycle, "atr_period", 14)),
            atr_bar_seconds=int(getattr(cycle, "atr_bar_seconds", 60)),
            atr_initial_drop_multiplier=float(getattr(cycle, "atr_initial_drop_multiplier", 1.5)),
            atr_buy_rebound_multiplier=float(getattr(cycle, "atr_buy_rebound_multiplier", 0.75)),
            atr_minimum_profit_multiplier=float(getattr(cycle, "atr_minimum_profit_multiplier", 1.0)),
            atr_sell_trail_multiplier=float(getattr(cycle, "atr_sell_trail_multiplier", 1.0)),
            atr_min_pct=float(getattr(cycle, "atr_min_pct", 0.10)),
            atr_max_pct=float(getattr(cycle, "atr_max_pct", 20.0)),
            protective_sell_enabled=bool(cycle.protective_sell_enabled),
            protective_sell_trailing_stop_pct=cycle.protective_sell_trailing_stop_pct,
            slippage_buffer_enabled=bool(cycle.slippage_buffer_enabled),
            slippage_buffer_pct=cycle.slippage_buffer_pct,
            hard_risk_limits_enabled=bool(cycle.hard_risk_limits_enabled),
            max_daily_loss_ticker=cycle.max_daily_loss_ticker,
            max_daily_loss_total=cycle.max_daily_loss_total,
            max_cycles_per_ticker_day=cycle.max_cycles_per_ticker_day,
            max_consecutive_losses=cycle.max_consecutive_losses,
            max_spread_pct=cycle.max_spread_pct,
            min_trade_price=cycle.min_trade_price,
            max_gap_from_prev_close_pct=cycle.max_gap_from_prev_close_pct,
            block_delayed_data_in_live=bool(cycle.block_delayed_data_in_live),
            what_if_check_enabled=bool(getattr(cycle, "what_if_check_enabled", True)),
            stale_data_guard_enabled=bool(getattr(cycle, "stale_data_guard_enabled", True)),
            max_selected_price_age_seconds=float(getattr(cycle, "max_selected_price_age_seconds", 3.0)),
            max_bid_ask_age_seconds=float(getattr(cycle, "max_bid_ask_age_seconds", 3.0)),
            max_rth_status_age_seconds=float(getattr(cycle, "max_rth_status_age_seconds", 60.0)),
            volatility_filter_enabled=bool(getattr(cycle, "volatility_filter_enabled", False)),
            volatility_window_seconds=int(getattr(cycle, "volatility_window_seconds", 300)),
            max_recent_price_move_pct=float(getattr(cycle, "max_recent_price_move_pct", 5.0)),
            session_timing_guard_enabled=bool(getattr(cycle, "session_timing_guard_enabled", False)),
            no_new_buy_first_minutes=int(getattr(cycle, "no_new_buy_first_minutes", 5)),
            no_new_buy_last_minutes=int(getattr(cycle, "no_new_buy_last_minutes", 15)),
            cancel_buy_before_close_minutes=int(getattr(cycle, "cancel_buy_before_close_minutes", 5)),
            cancel_sell_and_liquidate_before_close_enabled=bool(
                getattr(cycle, "cancel_sell_and_liquidate_before_close_enabled", False)
            ),
            liquidate_before_close_minutes=int(getattr(cycle, "liquidate_before_close_minutes", 5)),
            reinvest_profits=cycle.reinvest_profits,
            auto_repeat=self.strategy.auto_repeat,
            rth_only=bool(getattr(cycle, "rth_only", True)),
            exchange=cycle.exchange,
            primary_exchange=cycle.primary_exchange,
            contract_con_id=cycle.con_id,
            currency=cycle.currency,
            sec_type="STK",
            tif=self.strategy.tif,
        )

    def _log(
        self,
        level: str,
        message: str,
        cycle: Optional[CycleState] = None,
        *,
        raw: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record an event without allowing diagnostics to kill the worker."""
        ticker = cycle.ticker if cycle else None
        cycle_id = cycle.id if cycle else None
        try:
            self.storage.add_event(level, message, ticker=ticker, cycle_id=cycle_id, raw=raw)
        except sqlite3.Error as exc:
            # Event logging uses the same SQLite store as cycle persistence. A
            # write failure therefore activates the same fail-closed recovery
            # state, but _enter_storage_fault itself never calls _log.
            self._enter_storage_fault("persist audit event", exc)
        except Exception as exc:
            self._write_emergency_log(
                "SQLite event logging failed.",
                exc=exc,
                context={
                    "level": level,
                    "message": message,
                    "ticker": ticker,
                    "cycle_id": cycle_id,
                    "raw": raw,
                },
            )
        try:
            self.signals.event_logged.emit(message)
        except Exception as exc:
            self._write_emergency_log(
                "Qt event signal emission failed.",
                exc=exc,
                context={"level": level, "message": message},
            )
