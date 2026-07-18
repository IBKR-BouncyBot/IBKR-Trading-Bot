"""PySide6 operator interface for the portable IBKR trading bot.

The GUI collects connection/strategy input and renders controller snapshots,
current trading blockers, stage projections, history, captures, and reconciliation
data. It does not decide when to trade; the controller and StrategyEngine remain
the source of truth, and chart widgets are explanatory only.

Audit timestamps use UTC to match SQLite and capture data. The top input lock is
an accidental-command/edit guard: it disables editable configuration and all five
workflow buttons while monitoring and read-only views continue.
"""

from __future__ import annotations

import csv
import json
import re
import time
import zipfile
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .controller import TradingController
from .flowchart_model import FlowchartStageCard, build_strategy_flowchart_cards
from .ib_platform import (
    DEFAULT_CONNECTION_PROFILES,
    GATEWAY_PLATFORM,
    TWS_PLATFORM,
    default_port,
    normalize_profile_dict,
    platform_label,
    profile_key_for,
)
from .models import (
    APP_TIMEZONE_LABEL,
    PROFIT_GUARD_EPSILON_PCT,
    ConnectionSettings,
    Stage,
    StopAction,
    StrategySettings,
    minimum_sell_stop_price_for_profit,
    projected_minimum_profit_levels,
    recovery_cycle_signature,
    suggested_broker_timing_defaults,
    suggested_hard_risk_defaults,
)
from .paths import debug_captures_dir
from .timeline_scaling import (
    choose_timestamp_for_display,
    clamp_fraction,
    display_price_bounds,
    downsample_timeline_points,
    evenly_spaced_positions,
    filter_path_points_for_display,
    parse_timeline_timestamp,
    positive_price,
    time_window_from_values,
    timeline_path_time_window,
    true_time_axis_positions,
)

STAGE_LABELS = [
    (Stage.WAIT_INITIAL_DROP.value, "1. Waiting for initial drop"),
    (Stage.BUY_TRAIL_ACTIVE.value, "2. BUY trailing-stop active"),
    (Stage.WAIT_RISE_TRIGGER.value, "3. Waiting for minimum profit"),
    (Stage.SELL_TRAIL_ACTIVE.value, "4. SELL trailing-stop active"),
    (Stage.CYCLE_COMPLETE.value, "5. Cycle complete / repeat"),
]




class NoWheelEditFilter(QObject):
    """Prevent mouse-wheel changes on editable fields.

    Qt spin boxes and combo boxes can change value when the user scrolls while
    the cursor is over the field. In a trading app that is too easy to do by
    accident, so wheel events are swallowed for field widgets. Page-level
    scrolling still works when the cursor is not over an editable field.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Wheel and isinstance(watched, (QAbstractSpinBox, QComboBox)):
            return True
        return super().eventFilter(watched, event)

CURRENCY_SYMBOL = "$"

SEMANTIC_COLORS = {
    "success": "#16a34a",
    "active": "#2563eb",
    "waiting": "#d97706",
    "risk": "#dc2626",
    "inactive": "#6b7280",
    "border": "#d1d5db",
    "surface": "#ffffff",
    "soft": "#f8fafc",
}

def _stage_value(stage: Any) -> str:
    """Return the persisted Stage string for enum or already-normalized values."""
    value = getattr(stage, "value", stage)
    return str(value or "")


STAGE_ORDER = [_stage_value(stage) for stage, _label in STAGE_LABELS]
STAGE_TITLES = {_stage_value(stage): label for stage, label in STAGE_LABELS}
DEFAULT_VIEW_MODE = "Advanced"
VIEW_MODE_HELP = {
    "Simple": "Simple: core status, current stage, chart, next action, orders, positions, risk status, and the Recovery / audit log.",
    "Advanced": "Advanced: default live-supervision view with summaries, guards, previews, history tools, and a full-width Recovery / audit log.",
    "Debug": "Debug: Advanced plus raw API fields, internal diagnostics, detailed audit output, and full troubleshooting panels.",
}

TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "apicancelled", "inactive", "rejected"}


def _blocking_cycle_message(cycle: Optional[dict[str, Any]]) -> str:
    """Return a user-facing guard/timing blocker when a waiting cycle is not actually waiting for price."""
    if not isinstance(cycle, dict):
        return ""
    message = str(cycle.get("error_message") or "").strip()
    if not message:
        return ""
    lowered = message.lower()
    tokens = (
        "blocked",
        "guard",
        "risk limit",
        "max cycles",
        "spread",
        "gap",
        "stale",
        "volatility",
        "near open",
        "near close",
        "rth",
        "regular trading hours",
        "what-if",
        "market data",
    )
    return message if any(token in lowered for token in tokens) else ""


def _is_expected_guard_or_timing_blocker(message: Any) -> bool:
    """Classify configured guard/session pauses as caution, not recovery errors.

    These messages are caused by operator settings or normal session state
    (for example outside RTH or max-cycle limits). They can prevent new BUY
    submission, but they do not by themselves indicate a broker/local-state
    inconsistency requiring emergency manual intervention.
    """
    text = str(message or "").strip().lower()
    if not text:
        return False
    caution_tokens = (
        "blocked",
        "guard",
        "risk limit",
        "max cycles",
        "spread",
        "gap",
        "stale",
        "delayed",
        "frozen",
        "volatility",
        "near open",
        "near close",
        "open/close",
        "rth",
        "regular trading hours",
        "outside regular",
        "outside rth",
        "what-if",
        "market data",
        "user setting",
        "setting",
    )
    error_tokens = (
        "manual review",
        "recovery required",
        "sqlite expects",
        "no matching app-owned",
        "broker position is lower",
        "missing execution",
        "api error",
        "exception",
        "traceback",
    )
    return any(token in text for token in caution_tokens) and not any(token in text for token in error_tokens)




def _is_expected_strategy_wait_message(stage: Any, message: Any) -> bool:
    """Return True for normal live-strategy wait/status text, not recovery errors."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    normal_tokens = (
        "waiting for a higher price",
        "waiting for broker fill",
        "waiting for ibkr",
        "waiting for tws",
        "waiting for minimum",
        "minimum-profit",
        "minimum profit",
        "no longer protects",
        "native trailing",
        "trailing-stop order is working",
        "trailing stop order is working",
        "monitoring",
    )
    error_tokens = (
        "manual review",
        "recovery required",
        "sqlite expects",
        "broker position is lower",
        "exception",
        "traceback",
        "rejected",
    )
    if any(token in text for token in error_tokens):
        return False
    stage_text = str(stage or "")
    active_stages = {
        Stage.WAIT_INITIAL_DROP.value,
        Stage.BUY_TRAIL_ACTIVE.value,
        Stage.WAIT_RISE_TRIGGER.value,
        Stage.SELL_TRAIL_ACTIVE.value,
    }
    return stage_text in active_stages and any(token in text for token in normal_tokens)



def _is_expected_operator_stop_message(message: Any) -> bool:
    """Return True for an intentional local stop that is not a recovery error.

    A Stage 1 controlled stop can leave a stopped cycle with an explanatory
    error_message. That message is audit context, not a broker/local mismatch.
    Recovery should only escalate it if an app-owned broker order or app-bought
    unsold position remains visible.
    """
    text = str(message or "").strip().lower()
    if not text:
        return False
    stop_tokens = (
        "stop selected",
        "strategy stopped locally",
        "stopped locally",
        "no broker order was cancelled or submitted",
        "no broker order was canceled or submitted",
        "no broker command was sent",
        "no broker order was sent",
        "finish current cycle",
        "do not auto-repeat",
        "operator stopped",
    )
    hard_error_tokens = (
        "recovery required",
        "manual review required",
        "sqlite expects",
        "broker position is lower",
        "missing app-owned",
        "no matching app-owned",
        "exception",
        "traceback",
        "rejected",
        "cancel failed",
    )
    return any(token in text for token in stop_tokens) and not any(token in text for token in hard_error_tokens)


def _is_handled_recovery_stop_message(message: Any) -> bool:
    """Return True for recovery states already acknowledged by the operator."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in ("marked manually handled", "manually handled by operator"))


def _is_real_recovery_error_message(message: Any) -> bool:
    """Return True only for messages that imply unresolved broker/local risk."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    risk_tokens = (
        "recovery required",
        "manual review required",
        "broker/local mismatch",
        "sqlite expects",
        "broker position is lower",
        "missing execution",
        "no matching app-owned",
        "rejected",
        "api error",
        "exception",
        "traceback",
    )
    return any(token in text for token in risk_tokens)


def _timestamp_after(reference: Any, candidate: Any, *, tolerance_seconds: float = 0.0) -> bool:
    """Return whether candidate is after reference by more than tolerance seconds.

    Recovery uses this to decide whether a broker probe is older than the local
    cycle/order update it is being compared with.  Missing or unparsable times
    are treated as not-after so stale checks remain conservative but do not
    crash the Recovery tab.
    """
    reference_ts = _parse_timestamp(reference)
    candidate_ts = _parse_timestamp(candidate)
    if reference_ts is None or candidate_ts is None:
        return False
    return candidate_ts > reference_ts + max(0.0, float(tolerance_seconds or 0.0))


RECOVERY_REFRESH_MAX_AGE_SECONDS = 60.0


def _recovery_refresh_status(
    snapshot: Optional[dict[str, Any]],
    *,
    now_timestamp: Any = None,
    max_age_seconds: float = RECOVERY_REFRESH_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Classify whether the latest complete broker probe is safe to act on.

    A current probe must have succeeded, match the active cycle, retain the same
    reconciliation-relevant local signature, and remain recent. Failed attempts
    retain the preceding successful timestamp for operator context.
    """
    payload = snapshot or {}
    broker = payload.get("broker_recovery") if isinstance(payload.get("broker_recovery"), dict) else {}
    cycle = payload.get("active_cycle") if isinstance(payload.get("active_cycle"), dict) else None
    checked_at = broker.get("checked_at")
    last_successful = broker.get("last_successful_checked_at")
    result: dict[str, Any] = {
        "state": "not_refreshed",
        "is_current": False,
        "reason": "No broker refresh has been completed.",
        "checked_at": checked_at,
        "last_successful_checked_at": last_successful,
        "age_seconds": None,
    }
    if not checked_at:
        return result

    checked_ts = _parse_timestamp(checked_at)
    if checked_ts is None:
        result.update(state="failed", reason="The broker refresh timestamp is invalid.")
        return result
    if now_timestamp is None:
        now_ts = time.time()
    elif isinstance(now_timestamp, (int, float)):
        now_ts = float(now_timestamp)
    else:
        parsed_now = _parse_timestamp(now_timestamp)
        now_ts = parsed_now if parsed_now is not None else time.time()
    result["age_seconds"] = max(0.0, now_ts - checked_ts)

    component_errors = [
        str(broker.get(key) or "").strip()
        for key in ("error", "position_error", "recent_executions_error")
        if str(broker.get(key) or "").strip()
    ]
    if component_errors:
        result.update(state="failed", reason=component_errors[0])
        return result
    if not bool(broker.get("connected")):
        result.update(state="failed", reason="The refresh did not have an active IBKR API connection.")
        return result
    if not bool(payload.get("connected")):
        result.update(state="stale", reason="The app disconnected from IBKR/TWS after the refresh.")
        return result
    if broker.get("invalidated_at"):
        result.update(
            state="stale",
            reason=str(broker.get("invalidation_reason") or "Broker connectivity changed after the refresh."),
        )
        return result
    if broker.get("upstream_connected") is False:
        result.update(state="failed", reason="IBKR upstream connectivity was unavailable during the refresh.")
        return result

    probe_cycle_id = str(broker.get("cycle_id") or "")
    local_cycle_id = str((cycle or {}).get("id") or "")
    if probe_cycle_id != local_cycle_id:
        result.update(state="stale", reason="The refresh belongs to a different local cycle state.")
        return result

    stored_signature = broker.get("local_cycle_signature")
    if not isinstance(stored_signature, dict):
        result.update(state="stale", reason="Refresh again to capture the current reconciliation signature.")
        return result
    if stored_signature != recovery_cycle_signature(cycle):
        result.update(state="stale", reason="Local order, fill, stage, or recovery facts changed after the refresh.")
        return result

    order_state_updated_at = broker.get("order_state_updated_at")
    if order_state_updated_at and _timestamp_after(checked_at, order_state_updated_at):
        result.update(state="stale", reason="A broker order update arrived after the full refresh.")
        return result

    if float(result["age_seconds"] or 0.0) > max(0.0, float(max_age_seconds)):
        result.update(
            state="stale",
            reason=f"The broker refresh is older than {int(max_age_seconds)} seconds.",
        )
        return result

    result.update(
        state="current",
        is_current=True,
        reason="Broker orders, position, executions, and local reconciliation facts are current.",
        last_successful_checked_at=last_successful or checked_at,
    )
    return result


def _app_owned_unsold_quantity(cycle: Optional[dict[str, Any]]) -> float:
    """Return the unsold quantity represented by one local cycle snapshot.

    Broker account positions can include shares acquired outside this app. GUI
    stop/recovery actions therefore derive their quantity only from the app's
    persisted BUY and SELL fill fields.
    """
    if not isinstance(cycle, dict):
        return 0.0
    bought = _float_or_none(cycle.get("buy_filled_qty")) or 0.0
    final_sold = _float_or_none(cycle.get("sell_filled_qty")) or 0.0
    protective_sold = _float_or_none(cycle.get("protective_sell_filled_qty")) or 0.0
    return max(0.0, bought - max(final_sold, protective_sold))


def _order_matches_local_identity(order: dict[str, Any], cycle: dict[str, Any], prefix: str) -> bool:
    """Match a broker-probe order to a locally recorded app order."""
    identities = (
        ("order_ref", f"{prefix}_order_ref"),
        ("order_id", f"{prefix}_order_id"),
        ("perm_id", f"{prefix}_perm_id"),
    )
    for broker_key, cycle_key in identities:
        broker_value = order.get(broker_key)
        cycle_value = cycle.get(cycle_key)
        if broker_value in (None, "") or cycle_value in (None, ""):
            continue
        if str(broker_value).strip() == str(cycle_value).strip():
            return True
    return False


def _local_terminal_order_time(cycle: dict[str, Any], order: dict[str, Any]) -> Optional[str]:
    """Return the local terminal timestamp for a matching app-owned order.

    A recovery probe is only superseded when a newer local update came from the
    app's normal broker polling path. A later broker refresh that still reports
    the order remains visible and is treated as a genuine mismatch.
    """
    stage = str(cycle.get("stage") or "")
    open_qty = _app_owned_unsold_quantity(cycle)
    descriptors = (
        ("buy", "buy_status", "buy_filled_qty", "buy_filled_at"),
        ("protective_sell", "protective_sell_status", "protective_sell_filled_qty", "protective_sell_filled_at"),
        ("sell", "sell_status", "sell_filled_qty", "sell_filled_at"),
    )
    for prefix, status_key, filled_key, filled_at_key in descriptors:
        if not _order_matches_local_identity(order, cycle, prefix):
            continue
        status = str(cycle.get(status_key) or "").strip().lower()
        filled = _float_or_none(cycle.get(filled_key)) or 0.0
        terminal = status in TERMINAL_ORDER_STATUSES
        if prefix == "buy" and filled > 0 and stage in {
            Stage.WAIT_RISE_TRIGGER.value,
            Stage.SELL_TRAIL_ACTIVE.value,
            Stage.CYCLE_COMPLETE.value,
        }:
            terminal = True
        if prefix in {"protective_sell", "sell"} and stage == Stage.CYCLE_COMPLETE.value and open_qty <= 0:
            terminal = True
        if not terminal:
            return None
        return str(cycle.get(filled_at_key) or cycle.get("updated_at") or "") or None
    return None


def _reconciled_open_app_orders(snapshot: Optional[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return visible broker orders and stale probe rows superseded locally.

    The latest recovery probe is a point-in-time snapshot. If the app later
    receives a terminal broker update through its normal order polling, that
    older probe row must not make a completed cycle look as though it still has
    a working order. A probe taken after the local terminal update is never
    hidden by this helper.
    """
    payload = snapshot or {}
    broker = payload.get("broker_recovery") if isinstance(payload.get("broker_recovery"), dict) else {}
    cycle = payload.get("active_cycle") if isinstance(payload.get("active_cycle"), dict) else {}
    checked_at = broker.get("checked_at")
    probe_cycle_id = str(broker.get("cycle_id") or "")
    local_cycle_id = str(cycle.get("id") or "")
    same_cycle = not probe_cycle_id or not local_cycle_id or probe_cycle_id == local_cycle_id
    visible: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for raw in broker.get("open_app_orders") or []:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").strip().lower()
        remaining = _float_or_none(raw.get("remaining"))
        if status in TERMINAL_ORDER_STATUSES or (remaining is not None and remaining <= 0):
            continue
        terminal_at = _local_terminal_order_time(cycle, raw) if same_cycle and cycle else None
        if terminal_at and checked_at and _timestamp_after(checked_at, terminal_at):
            superseded.append(raw)
            continue
        visible.append(raw)
    return visible, superseded


def _recovery_action_permissions(
    *,
    has_cycle: bool,
    startup_resume_required: bool,
    startup_resume_only: bool,
    recovery_required: bool,
    action_state: str,
    expected_non_recovery_wait: bool,
    open_order_count: int,
    has_working_local_order: bool,
    open_qty: float,
    terminal_safe_stage: bool,
    broker_refresh_current: bool,
) -> dict[str, bool]:
    """Compute Reconciliation button availability from actual recovery risk.

    Configured trading guards and normal strategy waits are monitoring states,
    not recovery states. They leave refresh/export available but do not expose
    Resume, stop, cancel, sell, leave-working, or mark-handled actions unless an
    independent broker/local mismatch, app-owned order, or unsold app position
    is also present.
    """
    has_open_orders = open_order_count > 0
    broker_or_position_risk = recovery_required or has_open_orders or has_working_local_order or open_qty > 0
    ordinary_wait_only = expected_non_recovery_wait and not broker_or_position_risk
    no_recovery_action_needed = (
        not broker_or_position_risk
        and (action_state in {"success", "inactive", "active"} or ordinary_wait_only or startup_resume_only)
    )
    actionable_recovery = not ordinary_wait_only and (broker_or_position_risk or action_state == "risk")
    resume_supported = (
        has_cycle
        and not startup_resume_required
        and actionable_recovery
        and not no_recovery_action_needed
        and (recovery_required or action_state == "risk" or has_working_local_order or has_open_orders)
    )
    can_stop_cycle = (
        has_cycle
        and not startup_resume_only
        and not terminal_safe_stage
        and actionable_recovery
        and not no_recovery_action_needed
    )
    cancel_supported = (has_working_local_order or has_open_orders) and not no_recovery_action_needed
    market_close_supported = open_qty > 0 and not no_recovery_action_needed
    can_mark_manual = (
        has_cycle
        and not no_recovery_action_needed
        and (recovery_required or action_state == "risk" or has_open_orders or open_qty > 0)
    )
    can_resume = resume_supported and broker_refresh_current
    can_cancel_order = cancel_supported and broker_refresh_current
    can_market_close = market_close_supported and broker_refresh_current
    can_leave_orders = cancel_supported and broker_refresh_current
    return {
        "no_recovery_action_needed": no_recovery_action_needed,
        "actionable_recovery": actionable_recovery,
        "broker_refresh_current": broker_refresh_current,
        "resume_supported": resume_supported,
        "cancel_supported": cancel_supported,
        "market_close_supported": market_close_supported,
        "can_resume": can_resume,
        "can_stop_cycle": can_stop_cycle,
        "can_cancel_order": can_cancel_order,
        "can_market_close": can_market_close,
        "can_mark_manual": can_mark_manual,
        "can_leave_orders": can_leave_orders,
        "ordinary_wait_only": ordinary_wait_only,
    }


def _stage_display_name(stage: Any) -> str:
    value = str(stage or "")
    return STAGE_TITLES.get(value, value or "Idle")


def _stage_index(stage: Any) -> Optional[int]:
    try:
        return STAGE_ORDER.index(str(stage)) + 1
    except Exception:
        return None


def _format_utc_timestamp(value: Any, *, compact: bool = False) -> str:
    """Return a GUI timestamp in the same UTC zone used by capture/log rows.

    Imported historical rows may contain naive or non-ISO text. When parsing fails,
    the raw value is shown rather than inventing a local-time conversion.
    """
    parsed = _parse_timestamp(value)
    if parsed is None:
        return str(value or "-")
    fmt = "%H:%M:%S UTC" if compact else "%Y-%m-%d %H:%M:%S UTC"
    return datetime.fromtimestamp(float(parsed), timezone.utc).strftime(fmt)



def _rth_zone(zone_name: Any) -> tuple[Optional[ZoneInfo], str]:
    """Return a displayable RTH timezone without falling back to local time."""
    raw = str(zone_name or "").strip() or "America/New_York"
    aliases = {
        "US/Eastern": "America/New_York",
        "EST5EDT": "America/New_York",
        "Eastern Standard Time": "America/New_York",
    }
    canonical = aliases.get(raw, raw)
    try:
        return ZoneInfo(canonical), raw
    except Exception:
        try:
            return ZoneInfo("America/New_York"), raw
        except Exception:
            return None, raw


def _parse_rth_endpoint(text_value: str, default_date: str, tz: ZoneInfo) -> Optional[datetime]:
    """Parse IB liquidHours endpoints such as YYYYMMDD:0930 or 0930."""
    text_value = str(text_value or "").strip()
    if not text_value or text_value.upper() == "CLOSED":
        return None
    if ":" in text_value:
        date_part, time_part = text_value.split(":", 1)
    else:
        date_part, time_part = default_date, text_value
    digits = re.sub(r"[^0-9]", "", f"{date_part}{time_part[:4]}")
    if len(digits) < 12:
        return None
    try:
        return datetime.strptime(digits[:12], "%Y%m%d%H%M").replace(tzinfo=tz)
    except Exception:
        return None


def _fallback_us_equity_rth_window(checked_at: Any, tz: ZoneInfo, tz_label: str) -> dict[str, Any]:
    """Return a human-readable regular-hours window when IBKR liquidHours is missing.

    IBKR normally supplies contract-specific liquidHours. During startup,
    imported snapshots, or adapter fallback mode, that field can be absent. The
    GUI then uses the standard US-equity session solely for display text; the
    controller still uses the adapter's RTH result for trading decisions.
    """
    checked_ts = _parse_timestamp(checked_at)
    now_utc = datetime.fromtimestamp(float(checked_ts), timezone.utc) if checked_ts is not None else datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    def next_weekday(day):
        while day.weekday() >= 5:
            day = day + timedelta(days=1)
        return day

    day = next_weekday(now_local.date())
    start_dt = datetime(day.year, day.month, day.day, 9, 30, tzinfo=tz)
    end_dt = datetime(day.year, day.month, day.day, 16, 0, tzinfo=tz)
    if now_local > end_dt:
        day = next_weekday(day + timedelta(days=1))
        start_dt = datetime(day.year, day.month, day.day, 9, 30, tzinfo=tz)
        end_dt = datetime(day.year, day.month, day.day, 16, 0, tzinfo=tz)
    return {
        "start_local": start_dt,
        "end_local": end_dt,
        "start_utc": start_dt.astimezone(timezone.utc),
        "end_utc": end_dt.astimezone(timezone.utc),
        "timezone_label": tz_label,
        "now_utc": now_utc,
        "now_local": now_local,
        "fallback": True,
    }


def _rth_window_from_status(status: dict[str, Any], checked_at: Any) -> Optional[dict[str, Any]]:
    """Extract the most relevant liquid-hours window for operator display.

    IBKR supplies liquidHours in the contract timezone.  The GUI displays both
    that local session and the UTC equivalent so RTH status can be aligned with
    market-capture/audit timestamps, which are stored in UTC.
    """
    liquid = str(status.get("liquid_hours") or status.get("liquidHours") or "").strip()
    tz, tz_label = _rth_zone(status.get("time_zone") or status.get("timeZoneId") or status.get("timezone"))
    if tz is None:
        return None
    checked_ts = _parse_timestamp(checked_at or status.get("checked_at"))
    now_utc = datetime.fromtimestamp(float(checked_ts), timezone.utc) if checked_ts is not None else datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    session_open_ts = _parse_timestamp(status.get("session_open"))
    session_close_ts = _parse_timestamp(status.get("session_close"))
    if session_open_ts is not None and session_close_ts is not None and session_close_ts > session_open_ts:
        start_utc = datetime.fromtimestamp(float(session_open_ts), timezone.utc)
        end_utc = datetime.fromtimestamp(float(session_close_ts), timezone.utc)
        return {
            "start_local": start_utc.astimezone(tz),
            "end_local": end_utc.astimezone(tz),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "timezone_label": tz_label,
            "now_utc": now_utc,
            "now_local": now_local,
            "fallback": str(status.get("source") or "").startswith("fallback"),
        }
    if not liquid:
        return _fallback_us_equity_rth_window(checked_at or status.get("checked_at"), tz, tz_label)
    windows: list[tuple[datetime, datetime]] = []
    for part in liquid.split(";"):
        part = part.strip()
        if not part or "CLOSED" in part.upper() or "-" not in part:
            continue
        start_text, end_text = part.split("-", 1)
        default_date = start_text.split(":", 1)[0] if ":" in start_text else now_local.strftime("%Y%m%d")
        start_dt = _parse_rth_endpoint(start_text, default_date, tz)
        end_dt = _parse_rth_endpoint(end_text, default_date, tz)
        if start_dt is None or end_dt is None:
            continue
        windows.append((start_dt, end_dt))
    if not windows:
        return None
    active = [(start, end) for start, end in windows if start <= now_local <= end]
    if active:
        start_dt, end_dt = active[0]
    else:
        future = [(start, end) for start, end in windows if start > now_local]
        start_dt, end_dt = min(future or windows, key=lambda item: abs((item[0] - now_local).total_seconds()))
    return {
        "start_local": start_dt,
        "end_local": end_dt,
        "start_utc": start_dt.astimezone(timezone.utc),
        "end_utc": end_dt.astimezone(timezone.utc),
        "timezone_label": tz_label,
        "now_utc": now_utc,
        "now_local": now_local,
    }


def _current_time_status_text() -> str:
    """Return synchronized UTC and local system time for the price monitor."""
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    return f"UTC {now_utc:%H:%M:%S} | System {now_local:%H:%M:%S %Z}"


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def _format_rth_status(price_snapshot: Optional[dict[str, Any]], *, short: bool = False) -> str:
    """Convert raw RTH state into a human-readable operating status."""
    snapshot = price_snapshot or {}
    status = snapshot.get("rth_status") if isinstance(snapshot.get("rth_status"), dict) else {}
    open_value = snapshot.get("rth_open")
    message = str(snapshot.get("rth_message") or status.get("message") or "").strip()
    source = str(status.get("source") or "").strip().replace("_", " ")
    checked = status.get("checked_at")
    checked_text = _format_utc_timestamp(checked, compact=True) if checked else ""
    window = _rth_window_from_status(status, checked)
    if open_value is True:
        base = "RTH open"
    elif open_value is False:
        base = "RTH closed"
    else:
        base = "RTH status unavailable"
    if window:
        start_local = window["start_local"]
        end_local = window["end_local"]
        start_utc = window["start_utc"]
        end_utc = window["end_utc"]
        tz_label = window["timezone_label"]
        now_utc = window["now_utc"]
        hours_text = f"{start_local:%H:%M}-{end_local:%H:%M} {tz_label} ({start_utc:%H:%M}-{end_utc:%H:%M} UTC)"
        if window.get("fallback"):
            hours_text += " standard US equity hours"
        if open_value is True:
            remaining = _human_duration((end_utc - now_utc).total_seconds())
            if short:
                return f"RTH open - closes in {remaining}"
            return f"RTH open - Regular hours {hours_text}; market close in {remaining}; checked {checked_text or 'now'}"
        if open_value is False:
            if window["start_local"] > window["now_local"]:
                until_open = _human_duration((start_utc - now_utc).total_seconds())
                if short:
                    return f"RTH closed - opens in {until_open}"
                return f"RTH closed - Regular hours {hours_text}; market opens in {until_open}; checked {checked_text or 'now'}"
            if short:
                return "RTH closed"
            return f"RTH closed - Regular hours {hours_text}; session ended; checked {checked_text or 'now'}"
    if short:
        if checked_text and base != "RTH status unavailable":
            return f"{base} ({checked_text})"
        return base
    details: list[str] = [base]
    if message:
        details.append(re.sub(r"\s+", " ", message).rstrip("."))
    elif source:
        details.append(f"source: {source}")
    if checked_text:
        details.append(f"checked {checked_text}")
    return " - ".join(details)


def _is_empty_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in {"", "-", "None", "none", "null"})


def _empty_display_for_label(label: Any) -> str:
    text = str(label or "").strip().lower()
    if "buy fill" in text or "average buy" in text or "avg buy" in text:
        return "No buy fill yet"
    if "sell fill" in text or "avg sell" in text or "average sell" in text:
        return "No sell fill yet"
    if "sell order" in text or "sell perm" in text:
        return "No sell order active"
    if "protective" in text and ("order" in text or "status" in text or "stop" in text):
        return "No protective order active"
    if "order id" in text or "permid" in text or "perm id" in text or "orderref" in text:
        return "No broker order yet"
    if "ticker" in text or "contract" in text or "conid" in text:
        return "Waiting for ticker confirmation"
    if "rth" in text:
        return "RTH status unavailable"
    if "api" in text or "raw" in text or "bid" in text or "ask" in text or "last" in text or "mark" in text or "close" in text or "source" in text:
        return "Not available from API"
    if "stage" in text:
        return "No active cycle"
    if "trigger" in text or "stop" in text or "anchor" in text or "price" in text:
        return "Not applicable in this stage"
    return "Not applicable"


def _semantic_state_for_text(text: Any) -> str:
    value = str(text or "").lower()
    if any(token in value for token in ("error", "blocked", "manual review", "risk", "failed", "missing")):
        return "risk"
    if any(token in value for token in ("waiting", "pending", "delayed", "stale", "not ready", "closed")):
        return "waiting"
    if any(token in value for token in ("connected", "done", "open", "fresh", "live", "running", "on", "ok", "passed")):
        return "success"
    if any(token in value for token in ("ready", "active", "current", "armed")):
        return "active"
    return "inactive"


def _looks_like_currency_label(label: Any) -> bool:
    """Return True for labels that represent a USD amount or price.

    The app currently trades USD stock contracts. The helper is intentionally
    label-based so order identifiers, quantities, counts, ages, conIds, and
    percentages do not get a currency prefix just because their value is numeric.
    """
    text = str(label or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in ("order id", "permid", "perm id", "orderref", "conid", "count", "qty", "quantity", "age", "reads", "changes", "generic", "mode", "source", "status", "error", "%", "percent")):
        return False
    return any(token in text for token in (
        "price",
        "amount",
        "budget",
        "profit",
        "p/l",
        "pnl",
        "trigger",
        "stop",
        "last",
        "bid",
        "ask",
        "midpoint",
        "mark",
        "close",
        "fill",
    ))


def _format_currency(value: Any, decimals: int = 4) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except Exception:
        return str(value)
    if number != number or number in (float("inf"), float("-inf")):
        return str(value)
    sign = "-" if number < 0 else ""
    return f"{sign}{CURRENCY_SYMBOL}{abs(number):,.{decimals}f}"


def _currency_decimals_for_label(label: Any) -> int:
    text = str(label or "").strip().lower()
    if any(token in text for token in ("amount", "budget", "p/l", "pnl", "gross", "net", "reinvested profit")):
        return 2
    return 4


def _format_field_value(field: Any, value: Any) -> str:
    if _is_empty_value(value):
        return _empty_display_for_label(field)
    if _looks_like_currency_label(field):
        return _format_currency(value, decimals=_currency_decimals_for_label(field))
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _polish_table_widget(
    table: QTableWidget,
    *,
    stretch_last: bool = True,
    horizontal_scroll: Qt.ScrollBarPolicy = Qt.ScrollBarAsNeeded,
    vertical_scroll: Qt.ScrollBarPolicy = Qt.ScrollBarAsNeeded,
    expanding: bool = True,
) -> QTableWidget:
    """Apply consistent table sizing and scrollbar behavior.

    Tables should use available window space first and expose scroll bars only
    when the content no longer fits. This keeps compact tables readable without
    wasting space and avoids clipped rows/columns in larger audit/debug tables.
    """
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setWordWrap(True)
    table.setShowGrid(True)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setMinimumSectionSize(22)
    table.verticalHeader().setDefaultSectionSize(26)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.setVerticalScrollBarPolicy(vertical_scroll)
    table.setHorizontalScrollBarPolicy(horizontal_scroll)
    scroll_per_pixel = getattr(QTableWidget, "ScrollPerPixel", None)
    if scroll_per_pixel is not None:
        table.setHorizontalScrollMode(scroll_per_pixel)
        table.setVerticalScrollMode(scroll_per_pixel)
    table.setTextElideMode(Qt.ElideRight)
    table.horizontalHeader().setHighlightSections(False)
    table.horizontalHeader().setMinimumSectionSize(54)
    table.horizontalHeader().setStretchLastSection(stretch_last)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding if expanding else QSizePolicy.Fixed)
    return table


def _fit_table_height_to_rows(
    table: QTableWidget,
    *,
    min_rows: int = 1,
    max_visible_rows: int = 12,
    min_height: int = 86,
    max_fit_height: int = 420,
    expand_when_overflow: bool = True,
) -> None:
    """Fit small tables to their rows while allowing larger ones to scroll.

    Qt layouts otherwise often leave small 2-column tables with a large empty
    area or clip audit rows behind hidden scrollbars. This function recalculates
    row/header height after content is set.
    """
    try:
        table.resizeRowsToContents()
        row_count = max(0, table.rowCount())
        visible_rows = max(min_rows, min(max(row_count, 1), max_visible_rows))
        header_h = table.horizontalHeader().height() if table.horizontalHeader() else 24
        frame_w = table.frameWidth() * 2 if hasattr(table, "frameWidth") else 2
        row_heights: list[int] = []
        for row_idx in range(min(row_count, visible_rows)):
            row_heights.append(max(22, int(table.rowHeight(row_idx))))
        if not row_heights:
            row_heights = [24] * visible_rows
        while len(row_heights) < visible_rows:
            row_heights.append(max(row_heights[-1], 24))
        target = int(header_h + frame_w + sum(row_heights) + 8)
        target = max(min_height, min(target, max_fit_height))
        table.setMinimumHeight(target)
        if row_count <= max_visible_rows:
            table.setMaximumHeight(target + 4)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        elif not expand_when_overflow:
            # Some audit tables deliberately reserve only a small, predictable
            # number of visible rows so the graph above can use the remaining
            # dialog height. Additional records remain available by scrolling.
            table.setMaximumHeight(target + 4)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            table.setMaximumHeight(16777215)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    except Exception:
        table.setMinimumHeight(min_height)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def _fit_table_height_to_all_rows(
    table: QTableWidget,
    *,
    min_height: int = 120,
    max_height: int = 560,
) -> None:
    """Make a bounded table show every row without a vertical scrollbar.

    This is used for audit summary blocks such as Market capture metadata, where
    the top table is short enough to fit the dialog and a scrollbar makes the
    operator wonder whether important fields are hidden.
    """
    try:
        table.resizeRowsToContents()
        row_count = max(0, table.rowCount())
        header_h = table.horizontalHeader().height() if table.horizontalHeader() else 24
        frame_w = table.frameWidth() * 2 if hasattr(table, "frameWidth") else 2
        row_total = 0
        for row_idx in range(row_count):
            row_total += max(22, int(table.rowHeight(row_idx)))
        target = int(header_h + frame_w + row_total + 12)
        target = max(min_height, min(target, max_height))
        table.setMinimumHeight(target)
        table.setMaximumHeight(target + 4)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    except Exception:
        table.setMinimumHeight(min_height)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def _auto_size_table_columns(
    table: QTableWidget,
    *,
    minimum: int = 64,
    maximum: int = 420,
    extra: int = 14,
    last_maximum: Optional[int] = None,
) -> None:
    """Size table columns to their contents without stretching empty space.

    The GUI uses many audit tables whose columns previously stretched to the
    full viewport, making short values waste space while long values were still
    clipped. This helper makes columns content-sized, caps extremely wide text,
    and leaves horizontal scrollbars available when a table genuinely needs more
    width than the window can provide.
    """
    try:
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        table.resizeColumnsToContents()
        last_col = table.columnCount() - 1
        for col in range(table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            cap = int(last_maximum if last_maximum is not None and col == last_col else maximum)
            width = int(table.columnWidth(col)) + int(extra)
            table.setColumnWidth(col, max(int(minimum), min(cap, width)))
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    except Exception:
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)


def _resize_table_columns_for_available_width(table: QTableWidget, *, min_last_width: int = 160) -> None:
    """Backward-compatible wrapper for content-based table autosizing."""
    _auto_size_table_columns(table, minimum=64, maximum=360, last_maximum=max(360, int(min_last_width)))


def _cap_table_columns_for_horizontal_scroll(table: QTableWidget, *, minimum: int = 70, maximum: int = 220) -> None:
    """Keep wide audit/history tables readable with content-sized columns."""
    _auto_size_table_columns(table, minimum=minimum, maximum=maximum, last_maximum=maximum)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "-"):
        super().__init__()
        self.setObjectName("MetricCard")
        self.title_text = title
        self.title = QLabel(title)
        self.title.setObjectName("MetricTitle")
        self.value = QLabel(value)
        self.value.setObjectName("MetricValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_value(self, value: Any) -> None:
        text = _format_field_value(self.title_text, value)
        if self.value.text() != text:
            self.value.setText(text)


class StageRibbon(QWidget):
    def __init__(self):
        super().__init__()
        self.cards: dict[str, QLabel] = {}
        self._last_stage: Any = object()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.labels: dict[str, str] = {}
        for stage_value, label in STAGE_LABELS:
            card = QLabel(label)
            card.setObjectName("StageInactive")
            card.setAlignment(Qt.AlignCenter)
            card.setWordWrap(True)
            card.setMinimumHeight(76)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.cards[stage_value] = card
            self.labels[stage_value] = label
            layout.addWidget(card)

    def set_stage(self, stage: Optional[str]) -> None:
        if stage == self._last_stage:
            return
        self._last_stage = stage
        active_idx = _stage_index(stage)
        terminal_error = stage in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}
        for idx, (stage_value, _label) in enumerate(STAGE_LABELS, start=1):
            card = self.cards[stage_value]
            label = self.labels.get(stage_value, stage_value)
            if terminal_error:
                state = "BLOCKED"
                fill = "#fef2f2"
                accent = SEMANTIC_COLORS["risk"]
                text_color = "#7f1d1d"
                border_width = 2
            elif active_idx == idx:
                state = "CURRENT"
                fill = "#eff6ff"
                accent = SEMANTIC_COLORS["active"]
                text_color = "#111827"
                border_width = 4
            elif active_idx is not None and idx < active_idx:
                state = "DONE"
                fill = "#ecfdf5"
                accent = SEMANTIC_COLORS["success"]
                text_color = "#064e3b"
                border_width = 2
            else:
                state = "PENDING"
                fill = "#f3f4f6"
                accent = "#9ca3af"
                text_color = "#374151"
                border_width = 1
            card.setObjectName("StageActive" if state == "CURRENT" else "StageInactive")
            card.setText(f"{label}\n{state}")
            card.setStyleSheet(
                f"background-color: {fill}; color: {text_color}; border: {border_width}px solid {accent}; "
                "border-radius: 10px; padding: 10px; font-size: 14px; font-weight: 800;"
            )
            card.style().unpolish(card)
            card.style().polish(card)


class StatusPill(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.title_text = title
        self.setObjectName("StatusPill")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(1)
        self.title = QLabel(title)
        self.title.setObjectName("StatusPillTitle")
        self.value = QLabel("Not available")
        self.value.setObjectName("StatusPillValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._state: Optional[str] = None
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        self.set_state("inactive")

    def set_value(self, value: Any, state: Optional[str] = None) -> None:
        text = str(value) if not _is_empty_value(value) else _empty_display_for_label(self.title_text)
        if self.value.text() != text:
            self.value.setText(text)
        self.set_state(state or _semantic_state_for_text(text))

    def set_state(self, state: str) -> None:
        colors = {
            "success": ("#ecfdf5", "#16a34a", "#064e3b"),
            "active": ("#eff6ff", "#2563eb", "#1e3a8a"),
            "waiting": ("#fffbeb", "#d97706", "#78350f"),
            "risk": ("#fef2f2", "#dc2626", "#7f1d1d"),
            "inactive": ("#f3f4f6", "#9ca3af", "#374151"),
        }
        effective_state = state if state in colors else "inactive"
        if effective_state == self._state:
            return
        self._state = effective_state
        fill, border, text = colors[effective_state]
        self.setStyleSheet(
            f"QFrame#StatusPill {{ background: {fill}; border: 1px solid {border}; border-radius: 8px; }}"
            f"QLabel#StatusPillTitle {{ color: #4b5563; font-size: 10px; font-weight: 700; }}"
            f"QLabel#StatusPillValue {{ color: {text}; font-size: 12px; font-weight: 800; }}"
        )


class LiveStatusBar(QFrame):
    DATA_MODE_LABELS = {0: "Auto", 1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed frozen"}

    def __init__(self):
        super().__init__()
        self.setObjectName("LiveStatusBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        self.pills: dict[str, StatusPill] = {}
        for title in ["Connection", "Profile", "Account", "Ticker", "RTH", "Data", "Trading", "Stage", "Position", "Protection"]:
            pill = StatusPill(title)
            self.pills[title] = pill
            layout.addWidget(pill, 1)
        self.input_lock_btn = QPushButton("\U0001f513")
        self.input_lock_btn.setObjectName("InputLockButton")
        self.input_lock_btn.setCheckable(True)
        self.input_lock_btn.setChecked(False)
        self.input_lock_btn.setFixedWidth(48)
        self.input_lock_btn.setMinimumHeight(40)
        lock_font = self.input_lock_btn.font()
        lock_font.setPointSize(max(lock_font.pointSize() + 4, 16))
        lock_font.setBold(True)
        self.input_lock_btn.setFont(lock_font)
        self.input_lock_btn.setToolTip(
            "Input lock is off. Toggle on to prevent configuration edits and disable the five workflow buttons."
        )
        self._input_lock_state: Optional[bool] = None
        layout.addWidget(self.input_lock_btn, 0)

    def set_input_lock_state(self, locked: bool) -> None:
        locked = bool(locked)
        if locked == self._input_lock_state:
            return
        self._input_lock_state = locked
        self.input_lock_btn.setText("\U0001f512" if locked else "\U0001f513")
        self.input_lock_btn.setToolTip(
            "Input lock is on. Toggle off to edit configuration values or use the five workflow buttons."
            if locked
            else "Input lock is off. Toggle on to prevent configuration edits and disable the five workflow buttons."
        )
        self.input_lock_btn.setProperty("locked", bool(locked))
        self.input_lock_btn.style().unpolish(self.input_lock_btn)
        self.input_lock_btn.style().polish(self.input_lock_btn)

    def update_data(self, snapshot: dict[str, Any]) -> None:
        connection = snapshot.get("connection") or {}
        strategy = snapshot.get("strategy") or {}
        cycle = snapshot.get("active_cycle") or {}
        price_snapshot = snapshot.get("price_snapshot") or {}
        broker_connectivity = snapshot.get("broker_connectivity") or {}
        connected = bool(snapshot.get("connected"))
        local_connected = bool(broker_connectivity.get("local_connected", connected))
        upstream_connected = broker_connectivity.get("upstream_connected")
        if upstream_connected not in {True, False, None}:
            upstream_connected = bool(upstream_connected)
        upstream_recovery_pending = bool(snapshot.get("upstream_recovery_pending"))
        awaiting_fresh_data = bool(broker_connectivity.get("awaiting_fresh_market_data"))
        has_market_data_context = bool(price_snapshot or cycle)
        connectivity_message = str(broker_connectivity.get("message") or snapshot.get("status") or "")
        connectivity_code = broker_connectivity.get("error_code")
        code_text = f"IBKR code {connectivity_code}. " if connectivity_code not in (None, "") else ""

        if not connected or not local_connected:
            connection_text, connection_state = "Disconnected", "risk"
        elif upstream_connected is False:
            connection_text, connection_state = "Gateway only", "risk"
        elif upstream_recovery_pending:
            connection_text, connection_state = "Reconciling", "waiting"
        elif awaiting_fresh_data and has_market_data_context:
            connection_text, connection_state = "Data pending", "waiting"
        elif upstream_connected is True:
            connection_text, connection_state = "Connected", "success"
        else:
            connection_text, connection_state = "Checking link", "waiting"
        connection_tooltip = (
            f"Local API socket: {'connected' if local_connected else 'disconnected'}\n"
            f"Gateway/TWS to IBKR servers: "
            f"{'connected' if upstream_connected is True else 'disconnected' if upstream_connected is False else 'not yet confirmed'}\n"
            f"{code_text}{connectivity_message}"
        ).strip()
        self.pills["Connection"].set_value(connection_text, connection_state)
        for widget in (
            self.pills["Connection"],
            self.pills["Connection"].title,
            self.pills["Connection"].value,
        ):
            if widget.toolTip() != connection_tooltip:
                widget.setToolTip(connection_tooltip)

        platform = platform_label(connection.get("platform") or GATEWAY_PLATFORM)
        mode = str(connection.get("trading_mode") or "live").upper()
        self.pills["Profile"].set_value(f"{platform} {mode}", "risk" if mode == "LIVE" else "success")
        account_candidates = [
            cycle.get("account"),
            connection.get("account"),
            snapshot.get("display_account"),
        ]
        account = ""
        for candidate in account_candidates:
            text = str(candidate or "").strip()
            if text and text not in {"-", "None", "Account not set"}:
                account = text
                break
        if not account:
            broker_accounts = [str(item).strip() for item in (snapshot.get("broker_accounts") or []) if str(item).strip()]
            if len(broker_accounts) == 1:
                account = broker_accounts[0]
            elif len(broker_accounts) > 1:
                account = f"{broker_accounts[0]} +{len(broker_accounts) - 1} accounts"
        account_text = account or "IBKR default"
        self.pills["Account"].set_value(account_text, "success" if upstream_connected is True else "waiting")
        contract = price_snapshot.get("contract") or {}
        ticker = cycle.get("ticker") or contract.get("ticker") or strategy.get("ticker") or "Waiting for ticker confirmation"
        exchange = cycle.get("exchange") or contract.get("exchange") or strategy.get("exchange") or "SMART"
        currency = cycle.get("currency") or contract.get("currency") or strategy.get("currency") or "USD"
        self.pills["Ticker"].set_value(f"{ticker} / {exchange} / {currency}", "success" if ticker and ticker != "Waiting for ticker confirmation" else "waiting")
        rth_open = price_snapshot.get("rth_open")
        rth_text = _format_rth_status(price_snapshot, short=True)
        if rth_open is True:
            self.pills["RTH"].set_value(rth_text, "success")
        elif rth_open is False:
            self.pills["RTH"].set_value(rth_text, "waiting")
        else:
            self.pills["RTH"].set_value(rth_text, "inactive")

        mode_value = price_snapshot.get("subscription_market_data_type")
        if mode_value is None:
            mode_value = price_snapshot.get("selected_market_data_type")
        mode_label = self.DATA_MODE_LABELS.get(mode_value, str(mode_value if mode_value is not None else "No mode"))
        event_tracking = bool(price_snapshot.get("market_data_event_tracking"))
        event_tracking_available = price_snapshot.get("market_data_event_tracking_available")
        age = price_snapshot.get("api_data_age_seconds")
        if age is None and not event_tracking:
            age = price_snapshot.get("age_seconds")
        data_code = str(price_snapshot.get("api_data_state") or "")
        has_price = price_snapshot.get("price") is not None
        if upstream_connected is False or data_code == "upstream_disconnected":
            data_text, data_state = "IBKR link lost", "risk"
        elif event_tracking and event_tracking_available is False:
            data_text, data_state = "Update tracking unavailable", "risk"
        elif bool(price_snapshot.get("api_data_invalidated")) or data_code == "invalidated":
            data_text, data_state = "Waiting for update", "waiting"
        elif not has_price:
            data_text, data_state = "No usable price", "risk"
        elif data_code == "stale" or (
            isinstance(age, (int, float))
            and float(age) > float(strategy.get("max_selected_price_age_seconds") or 3.0)
        ):
            stale_text = f"Stale {float(age):.1f}s" if isinstance(age, (int, float)) else "Stale"
            data_text, data_state = f"{mode_label} / {stale_text}", "waiting"
        elif data_code == "cached_only":
            data_text, data_state = "Cached only", "waiting"
        elif mode_value in {3, 4}:
            age_text = f"{float(age):.1f}s" if isinstance(age, (int, float)) else "recent"
            data_text, data_state = f"{mode_label} / Update {age_text}", "waiting"
        else:
            age_text = f"{float(age):.1f}s" if isinstance(age, (int, float)) else "recent"
            data_text, data_state = f"{mode_label} / Update {age_text}", "success"
        update_age_text = f"{float(age):.1f}s" if isinstance(age, (int, float)) else "unavailable"
        data_tooltip = (
            f"State: {data_code or 'not reported'}\n"
            f"Last actual streaming update age: {update_age_text}\n"
            f"Actual update sequence: {price_snapshot.get('market_data_update_sequence') or '-'}\n"
            f"Subscription ID: {price_snapshot.get('market_data_subscription_id') or '-'}\n"
            f"Update-event tracking available: "
            f"{'yes' if event_tracking_available is True else 'no' if event_tracking_available is False else 'not reported'}\n"
            f"Cached fields present: {'yes' if price_snapshot.get('api_data_present') else 'no'}\n"
            "Cached non-empty fields do not refresh quote freshness or advance the strategy."
        )
        self.pills["Data"].set_value(data_text, data_state)
        for widget in (
            self.pills["Data"],
            self.pills["Data"].title,
            self.pills["Data"].value,
        ):
            if widget.toolTip() != data_tooltip:
                widget.setToolTip(data_tooltip)

        stage = cycle.get("stage")
        trading_status = snapshot.get("trading_status") or {}
        trading_tooltip = ""
        if isinstance(trading_status, dict) and str(trading_status.get("summary") or "").strip():
            trading_text = str(trading_status.get("summary") or "Stopped")
            trading_state = str(trading_status.get("state") or "inactive")
            trading_tooltip = str(trading_status.get("tooltip") or trading_text)
        elif snapshot.get("startup_resume_required"):
            trading_text, trading_state = "Start required", "waiting"
        elif _blocking_cycle_message(cycle):
            trading_text, trading_state = "Guard paused", "waiting"
        elif stage in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            trading_text, trading_state = "Blocked", "risk"
        elif stage in {Stage.WAIT_INITIAL_DROP.value, Stage.BUY_TRAIL_ACTIVE.value, Stage.WAIT_RISE_TRIGGER.value, Stage.SELL_TRAIL_ACTIVE.value}:
            trading_text, trading_state = "Running", "active"
        elif cycle:
            trading_text, trading_state = "Stopped", "waiting"
        else:
            trading_text, trading_state = "Stopped", "waiting"
        self.pills["Trading"].set_value(trading_text, trading_state)
        if self.pills["Trading"].toolTip() != trading_tooltip:
            self.pills["Trading"].setToolTip(trading_tooltip)
        for widget in (self.pills["Trading"].title, self.pills["Trading"].value):
            if widget.toolTip() != trading_tooltip:
                widget.setToolTip(trading_tooltip)
        idx = _stage_index(stage)
        stage_text = f"{idx} of 5" if idx else _stage_display_name(stage)
        self.pills["Stage"].set_value(stage_text, "active" if idx else "inactive")
        try:
            position = int(cycle.get("buy_filled_qty") or 0) - int(cycle.get("sell_filled_qty") or 0) - int(cycle.get("protective_sell_filled_qty") or 0)
        except Exception:
            position = 0
        self.pills["Position"].set_value(f"{position} shares" if position else "No app position", "active" if position else "inactive")
        if not cycle:
            protection_text, protection_state = "N/A", "inactive"
        elif cycle.get("protective_sell_enabled") and cycle.get("protective_sell_order_ref"):
            protection_text, protection_state = "On", "success"
        elif cycle.get("protective_sell_enabled"):
            protection_text, protection_state = "Armed", "active"
        else:
            protection_text, protection_state = "Off", "waiting" if position else "inactive"
        self.pills["Protection"].set_value(protection_text, protection_state)


class CommandStepCard(QFrame):
    def __init__(self, title: str, button: QPushButton):
        super().__init__()
        self.setObjectName("CommandStepCard")
        self.button = button
        self.state = QLabel("NOT READY")
        self.state.setObjectName("CommandState")
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)
        layout.addWidget(self.button)
        layout.addWidget(self.state)
        layout.addWidget(self.detail)
        self._last_state_signature: Optional[tuple[str, bool, str]] = None
        self.set_state("Not ready", False, "")

    def set_state(self, state: str, enabled: bool, detail: str = "") -> None:
        display_state = str(state).upper()
        normalized = str(state).strip().lower()
        signature = (display_state, bool(enabled), str(detail))
        if signature == self._last_state_signature:
            return
        self._last_state_signature = signature
        colors = {
            "done": ("#ecfdf5", "#16a34a", "#064e3b"),
            "ready": ("#eff6ff", "#2563eb", "#1e3a8a"),
            "blocked": ("#fef2f2", "#dc2626", "#7f1d1d"),
            "error": ("#fef2f2", "#dc2626", "#7f1d1d"),
            "locked": ("#f3f4f6", "#6b7280", "#374151"),
            "not ready": ("#f3f4f6", "#9ca3af", "#374151"),
        }
        fill, border, text = colors.get(normalized, colors["not ready"])
        self.setStyleSheet(
            f"QFrame#CommandStepCard {{ background: {fill}; border: 2px solid {border}; border-radius: 10px; }}"
            f"QLabel#CommandState {{ color: {text}; font-size: 12px; font-weight: 900; }}"
        )
        self.state.setText(display_state)
        self.detail.setText(detail)
        self.button.setEnabled(enabled)


class CurrentStagePanel(QGroupBox):
    def __init__(self):
        super().__init__("Current stage")
        layout = QVBoxLayout(self)
        self.stage_label = QLabel("No active strategy cycle")
        self.stage_label.setObjectName("CurrentStageTitle")
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)
        self.happening_label = QLabel("")
        self.next_label = QLabel("")
        self.blocker_label = QLabel("")
        for label in (self.happening_label, self.next_label, self.blocker_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(label)
        self.values_table = QTableWidget(0, 2)
        self.values_table.setHorizontalHeaderLabels(["Relevant live value", "Current display"])
        _polish_table_widget(self.values_table, stretch_last=False, expanding=False)
        layout.addWidget(self.values_table)

    def update_data(self, cycle: Optional[dict[str, Any]], price_snapshot: Optional[dict[str, Any]], strategy: StrategySettings) -> None:
        cycle = cycle or {}
        price_snapshot = price_snapshot or {}
        stage = cycle.get("stage")
        title = _stage_display_name(stage)
        idx = _stage_index(stage)
        stage_text = f"CURRENT STAGE\n{title}" if idx else "CURRENT STAGE\nNo active strategy cycle"
        buy_order_type = "BUY MKT" if float(getattr(strategy, "buy_rebound_trail_pct", 0.0) or 0.0) <= 0 else "BUY trailing-stop"
        sell_order_type = "SELL MKT" if float(getattr(strategy, "sell_trailing_stop_pct", 0.0) or 0.0) <= 0 else "SELL trailing-stop"
        diag = dict(price_snapshot.get("native_order_trigger") or {})
        if stage == Stage.WAIT_INITIAL_DROP.value:
            guard_blocker = _blocking_cycle_message(cycle)
            if guard_blocker:
                happening = "No broker BUY order is working. Trading is paused by a guard/risk/timing condition, not by the initial-drop price condition."
                next_transition = "The bot will not submit a BUY order until this blocker clears or the setting is changed. Market data and the chart continue updating."
                blocker = guard_blocker
            else:
                happening = "Watching the selected app price against the initial drop trigger. No broker order is working yet."
                next_transition = f"Move to Stage 2 when price reaches the drop trigger; then submit a native IBKR {buy_order_type} order."
                blocker = "Waiting for the selected price to reach the configured initial drop."
            rows = [
                ("Current price", price_snapshot.get("price") or cycle.get("last_price")),
                ("Anchor", cycle.get("anchor_price")),
                ("Initial drop trigger", cycle.get("drop_trigger_price")),
                ("Blocking condition", guard_blocker or "None"),
                ("RTH status", _format_rth_status(price_snapshot)),
            ]
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            happening = f"Native IBKR {buy_order_type} order is working."
            next_transition = "Move to Stage 3 when IBKR/TWS reports a BUY fill."
            blocker = "Waiting for broker fill. A displayed chart crossing is not a fill until TWS reports execution."
            rows = [
                ("Current price", price_snapshot.get("price") or cycle.get("last_price")),
                ("Submitted BUY stop", cycle.get("buy_initial_trail_stop_price")),
                ("Native trigger diagnostic", diag.get("message")),
                ("Raw Last trigger value", diag.get("raw_last_value")),
                ("Order status", cycle.get("buy_status")),
                ("Order ID / permId", f"{cycle.get('buy_order_id') or '-'} / {cycle.get('buy_perm_id') or '-'}"),
            ]
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            happening = "Position is open; the app is waiting for the minimum-profit condition."
            next_transition = f"Move to Stage 4 when the required price is reached; then submit {sell_order_type}."
            blocker = "Waiting for price to reach the required minimum-profit trigger."
            rows = [
                ("Current price", price_snapshot.get("price") or cycle.get("last_price")),
                ("Average buy", cycle.get("avg_buy_price")),
                ("Minimum-profit trigger", cycle.get("rise_trigger_price")),
                ("Protective SELL stop", cycle.get("protective_sell_initial_stop_price")),
                ("Protective status", cycle.get("protective_sell_status")),
            ]
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            happening = f"Native IBKR {sell_order_type} order is working."
            next_transition = "Move to Stage 5 when IBKR/TWS reports the final SELL fill."
            blocker = "Waiting for broker SELL fill."
            rows = [
                ("Current price", price_snapshot.get("price") or cycle.get("last_price")),
                ("SELL initial stop", cycle.get("sell_initial_trail_stop_price")),
                ("Native trigger diagnostic", diag.get("message")),
                ("Raw Last trigger value", diag.get("raw_last_value")),
                ("Order status", cycle.get("sell_status")),
                ("Order ID / permId", f"{cycle.get('sell_order_id') or '-'} / {cycle.get('sell_perm_id') or '-'}"),
            ]
        elif stage == Stage.CYCLE_COMPLETE.value:
            happening = "Cycle is complete and recorded in local SQLite."
            next_transition = "Auto-repeat starts a new Stage 1 cycle if enabled."
            blocker = "No blocker; cycle is complete."
            rows = [
                ("Average buy", cycle.get("avg_buy_price")),
                ("Average sell", cycle.get("avg_sell_price")),
                ("Gross P/L", cycle.get("gross_pnl")),
                ("Net P/L", cycle.get("net_pnl")),
                ("Updated", cycle.get("updated_at")),
            ]
        elif stage in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            happening = "Trading is paused for manual review or error handling."
            next_transition = "Use Recovery / Stop strategy / Recovery controls after reconciling local state and broker state."
            blocker = cycle.get("error_message") or "Manual recovery required."
            rows = [
                ("Cycle stage", stage),
                ("Error", cycle.get("error_message")),
                ("Buy order", cycle.get("buy_order_ref")),
                ("Sell order", cycle.get("sell_order_ref") or cycle.get("protective_sell_order_ref")),
            ]
        else:
            happening = "No active cycle is running."
            next_transition = "Confirm ticker and price, then start the strategy."
            blocker = "Waiting for user command."
            rows = [
                ("Current price", price_snapshot.get("price")),
                ("Selected source", price_snapshot.get("source")),
                ("Ticker", getattr(strategy, "ticker", "")),
            ]
        happening_text = f"What is happening: {happening}"
        next_text = f"Next transition: {next_transition}"
        blocker_text = f"Current blocker: {blocker}"
        formatted_rows = tuple((str(label), _format_field_value(label, value)) for label, value in rows)
        signature = (stage_text, happening_text, next_text, blocker_text, formatted_rows)
        if signature == getattr(self, "_last_update_signature", None):
            return
        self._last_update_signature = signature
        self.stage_label.setText(stage_text)
        self.happening_label.setText(happening_text)
        self.next_label.setText(next_text)
        self.blocker_label.setText(blocker_text)
        self.values_table.setUpdatesEnabled(False)
        try:
            self.values_table.setRowCount(len(formatted_rows))
            for r, (label, value) in enumerate(formatted_rows):
                self.values_table.setItem(r, 0, QTableWidgetItem(label))
                self.values_table.setItem(r, 1, QTableWidgetItem(value))
        finally:
            self.values_table.setUpdatesEnabled(True)
        _resize_table_columns_for_available_width(self.values_table)
        _fit_table_height_to_rows(self.values_table, min_rows=3, max_visible_rows=7, min_height=118, max_fit_height=230)


class WhyNotMovingPanel(QGroupBox):
    def __init__(self):
        super().__init__("Why not moving?")
        layout = QVBoxLayout(self)
        self.body = QLabel("No active blocker detected.")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.body)

    def update_data(self, cycle: Optional[dict[str, Any]], price_snapshot: Optional[dict[str, Any]]) -> None:
        cycle = cycle or {}
        price_snapshot = price_snapshot or {}
        stage = cycle.get("stage")
        diag = dict(price_snapshot.get("native_order_trigger") or {})
        fields = price_snapshot.get("fields") or {}
        if stage == Stage.BUY_TRAIL_ACTIVE.value:
            lines = [
                "Broker BUY order is still open.",
                f"Filled quantity is {cycle.get('buy_filled_qty') or 0}.",
                "TWS has not reported a BUY execution.",
                f"Display price source: {price_snapshot.get('source') or 'Not available from API'}.",
                f"Native trigger method: {diag.get('trigger_method_label') or 'Last'}.",
                f"Raw Last value: {_format_field_value('Raw Last trigger value', diag.get('raw_last_value') or fields.get('last') or fields.get('delayedLast'))}.",
            ]
        elif stage == Stage.WAIT_INITIAL_DROP.value:
            guard_blocker = _blocking_cycle_message(cycle)
            if guard_blocker:
                lines = [
                    "Stage 1 is blocked by a guard/risk/timing condition, not by the initial-drop condition.",
                    f"Blocking condition: {guard_blocker}",
                    f"Current price: {_format_field_value('price', price_snapshot.get('price') or cycle.get('last_price'))}.",
                    f"Initial drop trigger: {_format_field_value('trigger', cycle.get('drop_trigger_price'))}.",
                    "The bot will not submit a BUY while this blocker remains active.",
                ]
            else:
                lines = [
                    "No broker order is submitted in Stage 1.",
                    f"Current price: {_format_field_value('price', price_snapshot.get('price') or cycle.get('last_price'))}.",
                    f"Initial drop trigger: {_format_field_value('trigger', cycle.get('drop_trigger_price'))}.",
                    "The app remains in Stage 1 until the selected app price reaches the trigger.",
                ]
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            lines = [
                "The BUY fill has been recorded, but the minimum-profit trigger has not been reached.",
                f"Average buy: {_format_field_value('Average buy fill', cycle.get('avg_buy_price'))}.",
                f"Required price: {_format_field_value('Minimum-profit trigger price', cycle.get('rise_trigger_price'))}.",
                f"Protective order status: {_format_field_value('Protective status', cycle.get('protective_sell_status'))}.",
            ]
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            lines = [
                "Broker SELL order is still open.",
                f"Filled quantity is {cycle.get('sell_filled_qty') or 0}.",
                "TWS has not reported the final SELL execution.",
                f"Native trigger method: {diag.get('trigger_method_label') or 'Last'}.",
            ]
        elif stage in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            lines = [
                "The cycle is paused for error/manual review.",
                f"Reason: {cycle.get('error_message') or 'manual reconciliation required'}.",
                "Use the Recovery tab to compare local and broker-visible state.",
            ]
        else:
            lines = ["No active stage is waiting for a broker or market transition."]
        text = "\n".join(f"- {line}" for line in lines)
        if self.body.text() != text:
            self.body.setText(text)


class CycleTimelineWidget(QWidget):
    """Visual trade-cycle timeline for the history audit dialog.

    The widget prefers completed market-data capture ZIP rows when they exist.
    If no capture file is available, it falls back to persisted cycle/order/
    execution/decision-event timestamps so the dialog still has a visual audit
    path instead of only tables.
    """

    def __init__(self, row: dict[str, Any], details: dict[str, Any], compact: bool = False):
        super().__init__()
        self.row = row or {}
        self.details = details or {}
        self.compact = bool(compact)
        # Default audit timelines should fit inside the dialog without scrollbars.
        # Scroll bars appear only after explicit zooming or on very small screens.
        self.setMinimumHeight(300 if self.compact else 320)
        self._base_canvas_width = 720 if self.compact else 940
        self._zoom_factor = 1.0
        self._hover_pos: Optional[QPointF] = None
        self._drag_start_pos: Optional[QPointF] = None
        self._drag_start_scroll: Optional[tuple[int, int]] = None
        self.setMouseTracking(True)
        self.setMinimumWidth(self._base_canvas_width)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed if self.compact else QSizePolicy.Preferred)
        self.setToolTip("Hover for crosshairs. Ctrl+mouse wheel zooms the audit timeline. Drag while zoomed to pan; use the scroll bars for larger zoom levels.")
        self._path_points_raw = self._build_price_path()
        self._markers = self._build_markers()
        self._transitions = self._build_stage_transitions()
        self._risk_blocks = self._build_risk_blocks()
        visible_points, self._hidden_path_points, self._precomputed_bounds = filter_path_points_for_display(
            self._path_points_raw,
            self._important_prices(),
        )
        # Downsample after filtering so imported outliers are not deliberately
        # preserved as local high/low points. This keeps the plotted cycle
        # focused while still disclosing hidden row counts in the widget text.
        self._path_points = downsample_timeline_points(
            visible_points,
            max_points=260 if self.compact else 360,
        )
        self._path_time_window = timeline_path_time_window(self._path_points)
        self._axis_buckets = [self._path_points, self._markers, self._transitions, self._risk_blocks]
        # When market-capture rows are plotted, their first/last timestamps own
        # the shared X-axis. Older cycle metadata or unrelated diagnostic rows
        # must not compress the blue path while the action graph uses a different
        # apparent scale. Without a usable market path, retain the all-event
        # fallback axis used by imported/legacy histories.
        self._axis_time_window = self._path_time_window or self._compute_axis_time_window()
        self._axis_positions = true_time_axis_positions(
            self._axis_buckets,
            reference_window=self._path_time_window,
        )
        self._untimed_item_count = self._count_untimed_items()
        if self._path_time_window is None:
            self._off_axis_timed_item_count = 0
        else:
            axis_low, axis_high = self._path_time_window
            self._off_axis_timed_item_count = sum(
                1
                for bucket in self._axis_buckets[1:]
                for item in bucket
                if isinstance(item, dict)
                and (item_time := _float_or_none(item.get("time"))) is not None
                and not (axis_low <= item_time <= axis_high)
            )

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(max(self._base_canvas_width, self.minimumWidth()), self.minimumHeight())

    def reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self.setMinimumWidth(self._base_canvas_width)
        self.updateGeometry()
        self.update()

    def _set_zoom_factor(self, value: float) -> None:
        self._zoom_factor = max(1.0, min(6.0, float(value)))
        self.setMinimumWidth(int(self._base_canvas_width * self._zoom_factor))
        self.updateGeometry()
        self.update()

    def _parent_scroll_area(self) -> Optional[QScrollArea]:
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parent()
        return None

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        modifiers = event.modifiers() if hasattr(event, "modifiers") else QApplication.keyboardModifiers()
        delta = event.angleDelta().y() if hasattr(event, "angleDelta") else 0
        if modifiers & Qt.ControlModifier and delta:
            factor = 1.18 if delta > 0 else 1 / 1.18
            old_width = max(1, self.width())
            old_zoom = self._zoom_factor
            self._set_zoom_factor(self._zoom_factor * factor)
            area = self._parent_scroll_area()
            if area is not None and old_zoom > 0:
                # Keep the cursor over approximately the same normalized chart
                # position while zooming. This feels closer to the live market
                # graph and avoids losing the marker under the mouse.
                pos = event.position() if hasattr(event, "position") else event.pos()
                hbar = area.horizontalScrollBar()
                cursor_ratio = max(0.0, min(1.0, float(pos.x()) / float(old_width)))
                new_width = max(1, self.minimumWidth())
                hbar.setValue(int(cursor_ratio * new_width - pos.x()))
            event.accept()
            return
        event.ignore()
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            area = self._parent_scroll_area()
            if area is not None:
                pos = event.position() if hasattr(event, "position") else event.pos()
                self._drag_start_pos = QPointF(pos)
                self._drag_start_scroll = (area.horizontalScrollBar().value(), area.verticalScrollBar().value())
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position() if hasattr(event, "position") else event.pos()
        self._hover_pos = QPointF(pos)
        if self._drag_start_pos is not None and self._drag_start_scroll is not None:
            area = self._parent_scroll_area()
            if area is not None:
                dx = pos.x() - self._drag_start_pos.x()
                dy = pos.y() - self._drag_start_pos.y()
                start_x, start_y = self._drag_start_scroll
                area.horizontalScrollBar().setValue(int(start_x - dx))
                area.verticalScrollBar().setValue(int(start_y - dy))
                self.update()
                event.accept()
                return
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._drag_start_pos is not None:
            self._drag_start_pos = None
            self._drag_start_scroll = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover_pos = None
        self.update()
        super().leaveEvent(event)

    def _cycle(self) -> dict[str, Any]:
        return (self.details.get("cycle") or self.row or {}) if isinstance(self.details, dict) else (self.row or {})

    def _event_time(self, event: dict[str, Any]) -> Optional[float]:
        return _timeline_time(event, "created_at", "executed_at", "updated_at")

    def _first_order(self, action: str, prefer_ref: str = "") -> dict[str, Any]:
        action = action.upper()
        prefer_ref = str(prefer_ref or "").upper()
        for order in self.details.get("orders") or []:
            if action and str(order.get("action") or "").upper() != action:
                continue
            if prefer_ref and prefer_ref not in str(order.get("order_ref") or "").upper():
                continue
            return dict(order)
        for order in self.details.get("orders") or []:
            if action and str(order.get("action") or "").upper() == action:
                return dict(order)
        return {}

    def _first_execution(self, side_token: str) -> dict[str, Any]:
        side_token = side_token.upper()
        for execution in self.details.get("executions") or []:
            side = str(execution.get("side") or "").upper()
            order_ref = str(execution.get("order_ref") or "").upper()
            if side_token in side or side_token in order_ref:
                return dict(execution)
        return {}

    def _first_decision_time(self, *tokens: str) -> Optional[float]:
        wanted = [str(token or "").lower() for token in tokens if str(token or "").strip()]
        if not wanted:
            return None
        events = sorted(
            list(self.details.get("decision_events") or []),
            key=lambda item: (_timeline_time(item, "created_at", "executed_at", "updated_at") is None, _timeline_time(item, "created_at", "executed_at", "updated_at") or 0.0),
        )
        for event in events:
            text = " ".join(
                str(event.get(key) or "")
                for key in ("event_type", "stage_before", "stage_after", "decision_result", "message")
            ).lower()
            if all(token in text for token in wanted):
                return self._event_time(event)
        return None

    def _first_order_time(self, action: str, prefer_ref: str = "") -> Optional[float]:
        order = self._first_order(action, prefer_ref)
        return _timeline_time(order, "created_at", "updated_at") if order else None

    def _action_reference_values(self) -> list[Any]:
        """Return app/capture timestamps used as the audit timeline time basis."""
        cycle = self._cycle()
        values: list[Any] = []
        for key in (
            "created_at",
            "updated_at",
            "buy_filled_at",
            "protective_sell_filled_at",
            "sell_filled_at",
        ):
            values.append(cycle.get(key) or self.row.get(key))
        for collection_name in ("orders", "decision_events", "events"):
            for item in self.details.get(collection_name) or []:
                if not isinstance(item, dict):
                    continue
                values.extend([item.get("created_at"), item.get("updated_at")])
        capture_rows = self.details.get("market_capture_rows") or []
        if capture_rows:
            for item in (capture_rows[0], capture_rows[-1]):
                if isinstance(item, dict):
                    values.extend([item.get("captured_at_utc"), item.get("event_time_utc"), item.get("timestamp"), item.get("time")])
        return [value for value in values if value not in (None, "")]

    def _aligned_action_time(self, preferred: Any, fallback: Any = None) -> Optional[float]:
        return choose_timestamp_for_display(
            preferred,
            fallback,
            reference_values=self._action_reference_values(),
            max_reference_offset_seconds=90 * 60,
        )

    def _event_price(self, event: dict[str, Any]) -> Optional[float]:
        raw = _parse_jsonish(event.get("raw_json") or event.get("raw"))
        if isinstance(raw, dict):
            for key in ("price", "selected_price", "avg_fill_price", "last_price"):
                value = _float_or_none(raw.get(key))
                if value is not None:
                    return value
            for nested_key in ("price_snapshot_at_event", "price_snapshot", "snapshot", "cycle"):
                nested = raw.get(nested_key)
                if isinstance(nested, dict):
                    for key in ("price", "selected_price", "avg_buy_price", "avg_sell_price", "last_price"):
                        value = _float_or_none(nested.get(key))
                        if value is not None:
                            return value
        return None

    def _path_row_price(self, row: dict[str, Any]) -> Optional[float]:
        # IB/TWS snapshots can contain 0.0 placeholders for unavailable fields.
        # A zero placeholder should not compress the visual price scale for an
        # otherwise successful imported/captured trade.
        for key in ("price", "selected_price", "last_price", "marketPrice", "fields.marketPrice", "fields.last", "fields.delayedLast"):
            value = positive_price(row.get(key))
            if value is not None:
                return value
        fields = row.get("fields")
        if isinstance(fields, dict):
            for key in ("marketPrice", "last", "delayedLast", "bidAskMidpoint", "delayedBidAskMidpoint"):
                value = positive_price(fields.get(key))
                if value is not None:
                    return value
        return None

    def _build_price_path(self) -> list[dict[str, Any]]:
        rows = self.details.get("market_capture_rows") or []
        points: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            price = self._path_row_price(row)
            if price is None:
                continue
            t = _timeline_time(row, "captured_at_utc", "event_time_utc", "timestamp", "time")
            points.append({"time": t, "price": price, "label": str(row.get("source") or "price"), "order": idx})
        if points:
            points.sort(key=lambda item: (item.get("time") is None, item.get("time") or item.get("order") or 0))
            return points

        # Fallback path from persisted cycle levels and fills. This is not a
        # tick-by-tick price path, but it keeps the visual timeline useful when
        # no completed RAM capture ZIP has been written yet.
        cycle = self._cycle()
        fallback = [
            ("Anchor", cycle.get("created_at") or self.row.get("created_at"), cycle.get("anchor_price") or self.row.get("anchor_price")),
            ("Drop trigger", cycle.get("updated_at") or self.row.get("updated_at"), cycle.get("drop_trigger_price") or self.row.get("drop_trigger_price")),
            ("BUY fill", cycle.get("buy_filled_at") or self.row.get("buy_filled_at"), cycle.get("avg_buy_price") or self.row.get("avg_buy_price")),
            ("Min profit", cycle.get("updated_at") or self.row.get("updated_at"), cycle.get("rise_trigger_price") or self.row.get("rise_trigger_price")),
            ("SELL fill", cycle.get("sell_filled_at") or cycle.get("protective_sell_filled_at") or self.row.get("sell_filled_at"), cycle.get("avg_sell_price") or cycle.get("protective_avg_sell_price") or self.row.get("avg_sell_price")),
        ]
        for idx, (label, ts, price) in enumerate(fallback):
            value = positive_price(price)
            if value is None:
                continue
            points.append({"time": _parse_timestamp(ts), "price": value, "label": label, "order": idx})
        return points


    def _build_markers(self) -> list[dict[str, Any]]:
        cycle = self._cycle()
        markers: list[dict[str, Any]] = []

        def add(label: str, price: Any, ts: Any, kind: str, note: str = "") -> None:
            value = positive_price(price)
            if value is None:
                return
            hint_map = {"anchor": 0.06, "drop": 0.18, "buy": 0.34, "protective": 0.66, "sell": 0.90}
            parsed_time = ts if isinstance(ts, (int, float)) else _parse_timestamp(ts)
            markers.append({"label": label, "price": value, "time": parsed_time, "kind": kind, "note": note, "position_hint": hint_map.get(kind, 0.5), "time_known": parsed_time is not None})

        buy_order = self._first_order("BUY")
        anchor_time = cycle.get("created_at") or self.row.get("created_at")
        drop_time = (
            self._first_decision_time("buy_trail_active")
            or self._first_decision_time("buy order")
            or self._first_decision_time("drop")
            or buy_order.get("created_at")
            or cycle.get("updated_at")
            or self.row.get("updated_at")
        )
        add("ANCHOR", cycle.get("anchor_price") or self.row.get("anchor_price"), anchor_time, "anchor", "Starting reference price")
        add("DROP", cycle.get("drop_trigger_price") or self.row.get("drop_trigger_price"), drop_time, "drop", "Initial drop trigger")

        buy_exec = self._first_execution("BUY")
        add(
            "BUY",
            buy_exec.get("avg_price") or buy_exec.get("price") or cycle.get("avg_buy_price") or self.row.get("avg_buy_price") or cycle.get("buy_initial_trail_stop_price"),
            self._aligned_action_time(
                cycle.get("buy_filled_at") or self.row.get("buy_filled_at") or self._first_decision_time("buy_fill") or buy_order.get("updated_at") or buy_order.get("created_at"),
                buy_exec.get("executed_at"),
            ),
            "buy",
            "BUY fill or submitted BUY level",
        )
        protective_order = self._first_order("SELL", "PROTECT")
        protective_exec = {}
        for execution in self.details.get("executions") or []:
            ref = str(execution.get("order_ref") or "").upper()
            if "PROTECT" in ref:
                protective_exec = dict(execution)
                break
        add(
            "PROTECTIVE SELL",
            protective_exec.get("avg_price") or protective_exec.get("price") or cycle.get("protective_avg_sell_price") or cycle.get("protective_sell_initial_stop_price"),
            self._aligned_action_time(
                cycle.get("protective_sell_filled_at") or self.row.get("protective_sell_filled_at") or protective_order.get("updated_at") or protective_order.get("created_at"),
                protective_exec.get("executed_at"),
            ),
            "protective",
            "Protective SELL marker",
        )
        sell_exec = {}
        for execution in self.details.get("executions") or []:
            side = str(execution.get("side") or "").upper()
            ref = str(execution.get("order_ref") or "").upper()
            if "SELL" in side or "SELL" in ref:
                if "PROTECT" not in ref:
                    sell_exec = dict(execution)
                    break
                if not sell_exec:
                    sell_exec = dict(execution)
        sell_order = self._first_order("SELL", "SELL_TRAIL")
        add(
            "FINAL SELL",
            sell_exec.get("avg_price") or sell_exec.get("price") or cycle.get("avg_sell_price") or self.row.get("avg_sell_price") or cycle.get("sell_initial_trail_stop_price"),
            self._aligned_action_time(
                cycle.get("sell_filled_at") or self.row.get("sell_filled_at") or self._first_decision_time("sell_fill") or sell_order.get("updated_at") or sell_order.get("created_at"),
                sell_exec.get("executed_at"),
            ),
            "sell",
            "Final profit/protective exit marker",
        )
        return markers

    def _build_stage_transitions(self) -> list[dict[str, Any]]:
        transitions: list[dict[str, Any]] = []
        for idx, event in enumerate(self.details.get("decision_events") or []):
            before = str(event.get("stage_before") or "").strip()
            after = str(event.get("stage_after") or "").strip()
            if not before and not after:
                continue
            label = f"{_stage_display_name(before)} -> {_stage_display_name(after)}" if before and after else _stage_display_name(after or before)
            transitions.append({
                "time": self._event_time(event),
                "price": self._event_price(event),
                "label": label,
                "event_type": str(event.get("event_type") or ""),
                "message": str(event.get("message") or ""),
                "order": idx,
            })
        return transitions

    def _build_risk_blocks(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        combined = list(self.details.get("decision_events") or []) + list(self.details.get("events") or [])
        for idx, event in enumerate(combined):
            if not _is_audit_risk_block_event(event):
                continue
            blocks.append({
                "time": self._event_time(event),
                "price": self._event_price(event),
                "label": str(event.get("event_type") or event.get("level") or "Guard"),
                "message": str(event.get("message") or ""),
                "order": idx,
            })
        return blocks

    def _all_prices(self) -> list[float]:
        prices: list[float] = []
        for bucket in (self._path_points, self._markers, self._transitions, self._risk_blocks):
            for item in bucket:
                value = positive_price(item.get("price"))
                if value is not None:
                    prices.append(value)
        return prices

    def _important_prices(self) -> list[float]:
        # Use only the cycle's anchor/drop/BUY/SELL markers for the Y-axis scale.
        # Decision-event and guard rows can carry stale/raw diagnostic prices from
        # imported logs; they should be plotted when in range, not allowed to
        # flatten the trade-cycle view.
        prices: list[float] = []
        for item in self._markers:
            value = positive_price(item.get("price"))
            if value is not None:
                prices.append(value)
        return prices

    def _marker_position(self, marker: dict[str, Any]) -> float:
        kind = str(marker.get("kind") or "").lower()
        semantic = {"anchor": 0.06, "drop": 0.18, "buy": 0.34, "protective": 0.66, "sell": 0.90}
        hint = _float_or_none(marker.get("position_hint"))
        return clamp_fraction(hint if hint is not None else semantic.get(kind, 0.50))

    def _path_position_span(self) -> tuple[float, float]:
        start, end = 0.08, 0.96
        return clamp_fraction(start), clamp_fraction(end)

    def _compute_axis_time_window(self) -> Optional[tuple[float, float]]:
        times: list[float] = []
        for bucket in self._axis_buckets:
            for item in bucket:
                value = _float_or_none(item.get("time")) if isinstance(item, dict) else None
                if value is not None:
                    times.append(value)
        if len(times) < 2:
            return None
        low = min(times)
        high = max(times)
        if high <= low:
            return None
        return low, high

    def _count_untimed_items(self) -> int:
        count = 0
        for bucket in self._axis_buckets:
            for item in bucket:
                if isinstance(item, dict) and positive_price(item.get("price")) is not None and _float_or_none(item.get("time")) is None:
                    count += 1
        return count

    def _axis_position_for(self, bucket: int, idx: int, fallback_position: float) -> float:
        return clamp_fraction(self._axis_positions.get((bucket, idx), fallback_position))

    def _axis_time_for_position(self, position: float) -> Optional[float]:
        if self._axis_time_window is None:
            return None
        low, high = self._axis_time_window
        return low + (high - low) * clamp_fraction((position - 0.08) / max(1e-9, 0.96 - 0.08))

    @staticmethod
    def _format_axis_time(value: Any) -> str:
        number = _float_or_none(value)
        if number is None:
            return "time unavailable"
        try:
            return _format_utc_timestamp(number)
        except Exception:
            return str(value)

    def zoom_factor(self) -> float:
        return float(self._zoom_factor)

    def set_zoom(self, zoom: float) -> None:
        self._set_zoom_factor(zoom)

    def _path_index_position(self, index: int, count: int) -> float:
        start, end = self._path_position_span()
        if count <= 1:
            return (start + end) / 2.0
        return start + (end - start) * (float(index) / float(max(1, count - 1)))

    def _position_for_timed_item(self, item: dict[str, Any], fallback_position: float) -> float:
        """Return a readable fallback position for imported items without time.

        The primary audit timeline uses one real timestamp axis through
        ``true_time_axis_positions``. Path/price alignment is retained only as a
        fallback for imported records that do not carry usable timestamps.
        """
        fallback = clamp_fraction(fallback_position)
        if len(self._path_points) < 2:
            return fallback
        item_time = _float_or_none(item.get("time"))
        if item_time is not None:
            timed_path: list[tuple[int, float]] = []
            for idx, point in enumerate(self._path_points):
                point_time = _float_or_none(point.get("time"))
                if point_time is not None:
                    timed_path.append((idx, float(point_time)))
            if len(timed_path) >= 2:
                low = min(t for _idx, t in timed_path)
                high = max(t for _idx, t in timed_path)
                span = max(1.0, high - low)
                tolerance = max(60.0, span * 0.02)
                if low - tolerance <= item_time <= high + tolerance:
                    nearest_idx, _nearest_time = min(timed_path, key=lambda row: abs(row[1] - item_time))
                    return clamp_fraction(self._path_index_position(nearest_idx, len(self._path_points)))

        # Imported historical rows may lack matching event times. If a BUY/SELL
        # marker price appears in the visible path, align it by price and use
        # the semantic stage position only as a tie-breaker.
        marker_price = positive_price(item.get("price"))
        if marker_price is not None:
            best: Optional[tuple[float, int]] = None
            for idx, point in enumerate(self._path_points):
                path_price = positive_price(point.get("price"))
                if path_price is None:
                    continue
                relative = abs(path_price - marker_price) / max(abs(marker_price), 0.01)
                position = self._path_index_position(idx, len(self._path_points))
                score = relative + abs(position - fallback) * 0.001
                if best is None or score < best[0]:
                    best = (score, idx)
            if best is not None and best[0] <= 0.0145:
                return clamp_fraction(self._path_index_position(best[1], len(self._path_points)))
        return fallback

    def _stage_transition_position(self, transition: dict[str, Any], index: int, count: int) -> float:
        after = str(transition.get("label") or transition.get("stage_after") or transition.get("event_type") or "").lower()
        mapping = (
            ("wait initial drop", 0.08),
            ("buy trail active", 0.24),
            ("wait rise trigger", 0.45),
            ("sell trail active", 0.68),
            ("cycle complete", 0.92),
            ("manual review", 0.86),
            ("error", 0.86),
            ("stopped", 0.86),
        )
        for token, position in mapping:
            if token in after:
                return position
        return evenly_spaced_positions(max(1, count), start=0.10, end=0.92)[min(index, max(0, count - 1))]

    def _risk_block_position(self, index: int, count: int) -> float:
        start, end = self._path_position_span()
        return evenly_spaced_positions(max(1, count), start=start, end=end)[min(index, max(0, count - 1))]

    @staticmethod
    def _x_for_position(left: float, right: float, position: float) -> float:
        return left + (right - left) * clamp_fraction(position)

    @staticmethod
    def _marker_color(kind: str) -> QColor:
        if kind == "buy":
            return QColor("#16a34a")
        if kind == "sell":
            return QColor("#059669")
        if kind == "protective":
            return QColor("#dc2626")
        if kind == "drop":
            return QColor("#d97706")
        return QColor("#2563eb")

    def _draw_small_label(self, painter: QPainter, x: float, y: float, text: str, color: QColor) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = min(190, max(46, metrics.horizontalAdvance(text) + 10))
        rect = QRectF(x - width / 2.0, y, width, 32)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(color, 1))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor("#111827"))
        painter.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignCenter | Qt.TextWordWrap, text)
        painter.restore()

    def _draw_marker_label(self, painter: QPainter, plot: QRectF, x: float, y: float, text: str, index: int, used_rects: Optional[list[QRectF]] = None) -> None:
        """Draw marker text without overlapping the Y axis or adjacent labels."""
        used_rects = used_rects if used_rects is not None else []
        label_width = 136.0 if not self.compact else 118.0
        label_height = 54.0 if not self.compact else 46.0
        above = y > (plot.top() + plot.bottom()) / 2.0
        label_y = y - label_height - 8 - (index % 2) * 10 if above else y + 14 + (index % 2) * 10
        label_x = x - label_width / 2.0
        label_x = max(plot.left() + 6, min(label_x, plot.right() - label_width - 6))

        def clamped(top: float) -> float:
            return max(plot.top() + 2, min(top, plot.bottom() - label_height - 2))

        rect = QRectF(label_x, clamped(label_y), label_width, label_height)
        directions = [-1, 1] if above else [1, -1]
        for attempt in range(12):
            padded = rect.adjusted(-6, -4, 6, 4)
            if not any(padded.intersects(existing) for existing in used_rects):
                break
            direction = directions[attempt % 2]
            distance = 12.0 * (1 + attempt // 2)
            rect.moveTop(clamped(label_y + direction * distance))
        used_rects.append(rect.adjusted(-6, -4, 6, 4))
        painter.drawText(
            rect,
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            text,
        )

    def _draw_hover_overlay(self, painter: QPainter, plot: QRectF, targets: list[tuple[float, float, str]], min_v: float, max_v: float) -> None:
        if self._hover_pos is None:
            return
        hover_x = float(self._hover_pos.x())
        hover_y = float(self._hover_pos.y())
        if not plot.contains(QPointF(hover_x, hover_y)):
            return
        visible_targets = [target for target in targets if (plot.left() - 24) <= target[0] <= (plot.right() + 24)]
        nearest = min(visible_targets, key=lambda item: abs(item[0] - hover_x) + abs(item[1] - hover_y) * 0.25, default=None)
        price_at_cursor = max_v - ((hover_y - plot.top()) / max(1.0, plot.height())) * (max_v - min_v)
        time_at_cursor = self._axis_time_for_position((hover_x - plot.left()) / max(1.0, plot.width()))
        cursor_lines = []
        if time_at_cursor is not None:
            cursor_lines.append(self._format_axis_time(time_at_cursor))
        cursor_lines.append(f"Cursor {_format_currency(price_at_cursor)}")
        text = "\n".join(cursor_lines)
        if nearest is not None:
            text = f"{nearest[2]}\n" + "\n".join(cursor_lines)
        painter.save()
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.DashLine))
        painter.drawLine(QPointF(hover_x, plot.top()), QPointF(hover_x, plot.bottom()))
        painter.drawLine(QPointF(plot.left(), hover_y), QPointF(plot.right(), hover_y))
        if nearest is not None:
            painter.setPen(QPen(QColor("#111827"), 1))
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(QPointF(nearest[0], nearest[1]), 4, 4)
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        lines = text.splitlines()[:4]
        label_width = min(260, max(142, max(metrics.horizontalAdvance(line) for line in lines) + 16))
        label_height = 18 + 15 * len(lines)
        label_x = hover_x + 12 if hover_x < plot.center().x() else hover_x - label_width - 12
        label_y = hover_y + 12 if hover_y < plot.center().y() else hover_y - label_height - 12
        label_x = max(plot.left() + 4, min(label_x, plot.right() - label_width - 4))
        label_y = max(plot.top() + 4, min(label_y, plot.bottom() - label_height - 4))
        label_rect = QRectF(label_x, label_y, label_width, label_height)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(label_rect, 6, 6)
        painter.setPen(QColor("#111827"))
        painter.drawText(label_rect.adjusted(8, 4, -8, -4), Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))
        painter.restore()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d1d5db"), 1))
        painter.drawRoundedRect(rect, 10, 10)

        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPointSize(10)
        painter.setFont(title_font)
        painter.setPen(QColor("#111827"))
        painter.drawText(rect.adjusted(12, 8, -12, -8), Qt.AlignTop | Qt.AlignLeft, "Visual buy/sell timeline per cycle")

        capture_files = self.details.get("market_capture_files") or []
        capture_rows = self.details.get("market_capture_rows") or []
        source = f"Source: {len(capture_rows):,} market-capture rows from {len(capture_files)} ZIP file(s)." if capture_rows else "Source: persisted cycle, order, execution and decision-event rows; no completed market-capture ZIP found."
        if self._hidden_path_points:
            source += f" Hidden from plotted market path: {self._hidden_path_points:,} off-scale/imported row(s)."
        if self._axis_time_window:
            if self._path_time_window:
                source += " Separate graphs: the plotted market-data window defines one shared horizontal timestamp scale for both graphs."
            else:
                source += " Separate graphs: market data path and app actions share the same horizontal timestamp scale."
        else:
            source += " Separate graphs: market data path and app actions use labelled fallback positions where timestamps are unavailable."
        if self._untimed_item_count:
            source += f" Untimed priced action item(s): {self._untimed_item_count}."
        if self._off_axis_timed_item_count:
            source += f" Timed app item(s) outside the plotted market-data window are pinned to the nearest edge: {self._off_axis_timed_item_count}."
        painter.setFont(QFont())
        painter.setPen(QColor("#5b6270"))
        painter.drawText(rect.adjusted(12, 28, -12, -8), Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap, source)

        path_prices = [point.get("price") for point in self._path_points]
        action_price_items: list[Any] = []
        for bucket in (self._markers, self._transitions, self._risk_blocks):
            for item in bucket:
                action_price_items.append(item.get("price"))
        path_bounds = display_price_bounds(path_prices, ()) if path_prices else None
        action_bounds = display_price_bounds(action_price_items, self._important_prices()) if action_price_items else None
        if path_bounds is None and action_bounds is None:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(rect, Qt.AlignCenter, "No positive price markers are available for this cycle.")
            return

        def axis_width_for(bounds_list: list[Optional[tuple[float, float]]]) -> int:
            metrics = painter.fontMetrics()
            labels: list[str] = []
            for bounds in bounds_list:
                if bounds is None:
                    continue
                labels.append(_format_currency(bounds[0]))
                labels.append(_format_currency(bounds[1]))
            if not labels:
                labels = ["Price"]
            return max(78, min(148, max(metrics.horizontalAdvance(label) for label in labels) + 22))

        axis_width = axis_width_for([path_bounds, action_bounds])
        plot_left = rect.left() + axis_width + 14
        plot_right = rect.right() - 30
        plot_width = max(180.0, plot_right - plot_left)
        top = rect.top() + (82 if not self.compact else 78)
        bottom_reserved = 52 if not self.compact else 44
        gap = 24 if not self.compact else 20
        available = max(176.0, rect.bottom() - top - bottom_reserved)
        price_height = max(80.0, available * 0.47)
        action_height = max(86.0, available - price_height - gap)
        if top + price_height + gap + action_height > rect.bottom() - 34:
            total = max(158.0, rect.bottom() - top - 34 - gap)
            price_height = max(72.0, total * 0.46)
            action_height = max(76.0, total - price_height)
        market_plot = QRectF(plot_left, top, plot_width, price_height)
        action_plot = QRectF(plot_left, market_plot.bottom() + gap, plot_width, action_height)

        def draw_plot_frame(plot: QRectF, bounds: Optional[tuple[float, float]], title: str, empty: str) -> None:
            painter.save()
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.setPen(QPen(QColor("#d1d5db"), 1))
            painter.drawRect(plot)
            label_font = QFont(painter.font())
            label_font.setBold(True)
            painter.setFont(label_font)
            painter.setPen(QColor("#111827"))
            painter.drawText(QRectF(plot.left(), plot.top() - 22, plot.width(), 20), Qt.AlignLeft | Qt.AlignVCenter, title)
            painter.setFont(QFont())
            if bounds is None:
                painter.setPen(QColor("#6b7280"))
                painter.drawText(plot, Qt.AlignCenter | Qt.TextWordWrap, empty)
                painter.restore()
                return
            min_v, max_v = bounds
            painter.setPen(QPen(QColor("#e5e7eb"), 1))
            for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                y = plot.bottom() - plot.height() * frac
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#6b7280"))
            painter.drawText(QRectF(rect.left() + 8, plot.top() - 4, axis_width, 20), Qt.AlignRight | Qt.AlignVCenter, _format_currency(max_v))
            painter.drawText(QRectF(rect.left() + 8, plot.bottom() - 16, axis_width, 20), Qt.AlignRight | Qt.AlignVCenter, _format_currency(min_v))
            painter.restore()

        def draw_time_ticks(plot: QRectF) -> None:
            if self._axis_time_window is None:
                return
            start_t, end_t = self._axis_time_window
            mid_t = start_t + (end_t - start_t) / 2.0
            painter.save()
            painter.setPen(QPen(QColor("#d1d5db"), 1))
            for pos, ts in ((0.08, start_t), (0.52, mid_t), (0.96, end_t)):
                x_tick = self._x_for_position(plot.left(), plot.right(), pos)
                painter.drawLine(QPointF(x_tick, plot.bottom()), QPointF(x_tick, plot.bottom() + 7))
                label_rect = QRectF(x_tick - 82, plot.bottom() + 10, 164, 34)
                painter.setPen(QColor("#6b7280"))
                painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self._format_axis_time(ts))
                painter.setPen(QPen(QColor("#d1d5db"), 1))
            painter.restore()

        def y_for(plot: QRectF, bounds: Optional[tuple[float, float]], price: Any) -> float:
            value = positive_price(price)
            if bounds is None or value is None:
                return plot.center().y()
            min_v, max_v = bounds
            fraction = clamp_fraction((float(value) - min_v) / max(1e-9, max_v - min_v))
            return plot.bottom() - plot.height() * fraction

        def x_for(plot: QRectF, bucket: int, idx: int, count: int, item: Optional[dict[str, Any]] = None) -> float:
            if bucket == 0:
                position = self._axis_position_for(bucket, idx, self._path_index_position(idx, count))
            elif bucket == 1 and item is not None:
                position = self._axis_position_for(bucket, idx, self._position_for_timed_item(item, self._marker_position(item)))
            elif bucket == 2 and item is not None:
                position = self._axis_position_for(bucket, idx, self._position_for_timed_item(item, self._stage_transition_position(item, idx, count)))
            elif bucket == 3 and item is not None:
                position = self._axis_position_for(bucket, idx, self._position_for_timed_item(item, self._risk_block_position(idx, count)))
            else:
                position = float(idx) / max(1.0, float(max(1, count - 1)))
            return self._x_for_position(plot.left(), plot.right(), position)

        draw_plot_frame(market_plot, path_bounds, "Market data graph - captured selected prices", "No captured market-data path is available for this cycle.")
        draw_plot_frame(action_plot, action_bounds, "App actions graph - orders, fills, stages and guards", "No priced app action markers are available; stage/guard events still appear on the time axis when present.")

        if self._axis_time_window is not None:
            painter.save()
            painter.setPen(QPen(QColor("#e2e8f0"), 1, Qt.DashLine))
            for position in (0.08, 0.52, 0.96):
                market_x = self._x_for_position(market_plot.left(), market_plot.right(), position)
                action_x = self._x_for_position(action_plot.left(), action_plot.right(), position)
                painter.drawLine(QPointF(market_x, market_plot.top()), QPointF(market_x, market_plot.bottom()))
                painter.drawLine(QPointF(action_x, action_plot.top()), QPointF(action_x, action_plot.bottom()))
            painter.restore()

        market_hover_targets: list[tuple[float, float, str]] = []
        action_hover_targets: list[tuple[float, float, str]] = []

        if path_bounds is not None and len(self._path_points) >= 2:
            painter.setPen(QPen(QColor("#2563eb"), 2))
            previous: Optional[QPointF] = None
            for idx, point in enumerate(self._path_points):
                x = x_for(market_plot, 0, idx, len(self._path_points), point)
                y = y_for(market_plot, path_bounds, point.get("price"))
                current = QPointF(x, y)
                market_hover_targets.append((x, y, f"Market data\n{self._format_axis_time(point.get('time'))}\n{_format_currency(point.get('price'))}"))
                if previous is not None:
                    painter.drawLine(previous, current)
                previous = current
        elif path_bounds is not None and len(self._path_points) == 1:
            point = self._path_points[0]
            x = x_for(market_plot, 0, 0, 1, point)
            y = y_for(market_plot, path_bounds, point.get("price"))
            painter.setPen(QPen(QColor("#2563eb"), 2))
            painter.drawEllipse(QPointF(x, y), 4, 4)
            market_hover_targets.append((x, y, f"Market data\n{self._format_axis_time(point.get('time'))}\n{_format_currency(point.get('price'))}"))

        for idx, transition in enumerate(self._transitions[:18]):
            x = x_for(action_plot, 2, idx, max(1, len(self._transitions)), transition)
            painter.setPen(QPen(QColor("#93c5fd"), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, action_plot.top()), QPointF(x, action_plot.bottom()))
            y = y_for(action_plot, action_bounds, transition.get("price"))
            action_hover_targets.append((x, y, f"Stage transition\n{self._format_axis_time(transition.get('time'))}\n{_compact_text(transition.get('label') or transition.get('event_type'), 56)}"))
            if not self.compact:
                label = _compact_text(transition.get("event_type") or transition.get("label"), 28)
                self._draw_small_label(painter, x, action_plot.bottom() + 8 + (idx % 2) * 31, label, QColor("#2563eb"))

        for idx, block in enumerate(self._risk_blocks[:12]):
            x = x_for(action_plot, 3, idx, max(1, len(self._risk_blocks)), block)
            y = y_for(action_plot, action_bounds, block.get("price"))
            painter.setBrush(QBrush(QColor("#fef2f2")))
            painter.setPen(QPen(QColor("#dc2626"), 2))
            painter.drawRect(QRectF(x - 5, y - 5, 10, 10))
            action_hover_targets.append((x, y, f"Risk/guard\n{self._format_axis_time(block.get('time'))}\n{_compact_text(block.get('label') or 'Guard', 56)}"))
            if not self.compact:
                label_y = max(action_plot.top() + 4, min(y - 40, action_plot.bottom() - 18))
                self._draw_small_label(painter, x, label_y, _compact_text(block.get("label") or "Guard", 24), QColor("#dc2626"))

        marker_label_rects: list[QRectF] = []
        for idx, marker in enumerate(self._markers):
            x = x_for(action_plot, 1, idx, max(1, len(self._markers)), marker)
            y = y_for(action_plot, action_bounds, marker.get("price"))
            color = self._marker_color(str(marker.get("kind") or ""))
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            radius = 7 if self.compact else 8
            painter.drawEllipse(QPointF(x, y), radius, radius)
            label = f"{marker.get('label')}\n{_format_currency(marker.get('price'))}"
            painter.setPen(QColor("#111827"))
            action_hover_targets.append((x, y, f"{marker.get('label')}\n{self._format_axis_time(marker.get('time'))}\n{_format_currency(marker.get('price'))}"))
            self._draw_marker_label(painter, action_plot, x, y, label, idx, marker_label_rects)

        draw_time_ticks(action_plot)

        if path_bounds is not None:
            self._draw_hover_overlay(painter, market_plot, market_hover_targets, path_bounds[0], path_bounds[1])
        if action_bounds is not None:
            self._draw_hover_overlay(painter, action_plot, action_hover_targets, action_bounds[0], action_bounds[1])

        if not self.compact:
            legend = [
                f"Market rows drawn: {len(self._path_points)}",
                f"Action markers: {len(self._markers)}",
                f"Stage transitions: {len(self._transitions)}",
                f"Risk/guard blocks: {len(self._risk_blocks)}",
            ]
            if self._hidden_path_points:
                legend.append(f"Hidden off-scale rows: {self._hidden_path_points}")
            painter.setPen(QColor("#374151"))
            painter.drawText(rect.adjusted(12, rect.height() - 24, -12, -6), Qt.AlignLeft | Qt.AlignBottom, " | ".join(legend))



def _format_price(value: Any) -> str:
    return _format_currency(value)


def _pct_progress(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _parse_timestamp(value: Any) -> Optional[float]:
    return parse_timeline_timestamp(value)


def _timeline_time(row: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in row:
            parsed = _parse_timestamp(row.get(key))
            if parsed is not None:
                return parsed
    return None


def _is_audit_risk_block_event(event: dict[str, Any]) -> bool:
    """Return True only for explicit guard/risk blockers in audit timelines.

    Successful imported cycles can contain transient diagnostic text with words
    such as "error" or "failed" even though no guard blocked the trade. The
    timeline should show red risk markers only for actual blocking/guard events.
    """
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("event_type") or event.get("level") or "").strip().lower()
    result = str(event.get("decision_result") or "").strip().lower()
    level = str(event.get("level") or "").strip().lower()
    message = str(event.get("message") or "").strip().lower()
    structured = " ".join(part for part in (event_type, result, level) if part)

    explicit_tokens = (
        "block",
        "blocked",
        "risk_guard",
        "risk guard",
        "guard_block",
        "guard block",
        "stale_data",
        "stale data",
        "wide_spread",
        "gap_from_close",
        "max_cycles",
        "rth_block",
        "near_open_close",
        "high_volatility",
        "what_if_failed",
        "rejected",
        "reject",
    )
    if any(token in structured for token in explicit_tokens):
        return True
    if result in {"blocked", "rejected", "reject", "failed", "error"}:
        return True

    message_block_phrases = (
        "blocked by",
        "guard blocked",
        "risk guard",
        "trade guard",
        "cannot start because",
        "preventing new buy",
        "prevents new buy",
        "new buy blocked",
    )
    return any(phrase in message for phrase in message_block_phrases)


def _compact_text(value: Any, max_len: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)].rstrip() + "…"


def _draw_text_box(painter: QPainter, rect: QRectF, text: str, color: QColor, fill: QColor) -> None:
    painter.save()
    painter.setPen(QPen(color, 1))
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, 6, 6)
    painter.drawText(rect.adjusted(6, 2, -6, -2), Qt.AlignVCenter | Qt.AlignLeft, text)
    painter.restore()


class ProfitGuardWidget(QWidget):
    """Input-side strategy map drawn from the editable strategy controls."""

    def __init__(self):
        super().__init__()
        self.initial_drop_pct = 2.0
        self.buy_rebound_pct = 1.0
        self.minimum_profit_pct = 3.0
        self.sell_trail_pct = 1.0
        self.reference_anchor = 100.0
        self.reference_label = "normalized baseline"
        self.protective_sell_enabled = False
        self.protective_sell_trail_pct = 3.0
        self.slippage_buffer_enabled = False
        self.slippage_buffer_pct = 0.50
        self.hard_risk_limits_enabled = False
        self.max_daily_loss_ticker = 0.0
        self.max_daily_loss_total = 0.0
        self.max_cycles_per_ticker_day = 0
        self.max_consecutive_losses = 0
        self.max_spread_pct = 1.0
        self.min_trade_price = 0.0
        self.max_gap_pct = 10.0
        self.block_delayed_live = True
        self.what_if_enabled = True
        self.stale_data_guard_enabled = True
        self.max_price_age_seconds = 3.0
        self.volatility_filter_enabled = True
        self.max_recent_move_pct = 5.0
        self.session_timing_guard_enabled = True
        self.no_new_buy_first_minutes = 5
        self.no_new_buy_last_minutes = 15
        self.cancel_buy_before_close_minutes = 5
        self.setMinimumHeight(560)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(
        self,
        initial_drop_pct: float,
        buy_rebound_pct: float,
        minimum_profit_pct: float,
        sell_trail_pct: float,
        reference_anchor: Optional[float] = None,
        reference_label: str = "",
        protective_sell_enabled: bool = False,
        protective_sell_trail_pct: float = 3.0,
        slippage_buffer_enabled: bool = False,
        slippage_buffer_pct: float = 0.50,
        hard_risk_limits_enabled: bool = False,
        max_daily_loss_ticker: float = 0.0,
        max_daily_loss_total: float = 0.0,
        max_cycles_per_ticker_day: int = 0,
        max_consecutive_losses: int = 0,
        max_spread_pct: float = 1.0,
        min_trade_price: float = 0.0,
        max_gap_pct: float = 10.0,
        block_delayed_live: bool = True,
        what_if_enabled: bool = True,
        stale_data_guard_enabled: bool = True,
        max_price_age_seconds: float = 3.0,
        volatility_filter_enabled: bool = True,
        max_recent_move_pct: float = 5.0,
        session_timing_guard_enabled: bool = True,
        no_new_buy_first_minutes: int = 5,
        no_new_buy_last_minutes: int = 15,
        cancel_buy_before_close_minutes: int = 5,
    ) -> None:
        self.initial_drop_pct = float(initial_drop_pct)
        self.buy_rebound_pct = float(buy_rebound_pct)
        self.minimum_profit_pct = float(minimum_profit_pct)
        self.sell_trail_pct = float(sell_trail_pct)
        self.protective_sell_enabled = bool(protective_sell_enabled)
        self.protective_sell_trail_pct = float(protective_sell_trail_pct)
        self.slippage_buffer_enabled = bool(slippage_buffer_enabled)
        self.slippage_buffer_pct = float(slippage_buffer_pct)
        self.hard_risk_limits_enabled = bool(hard_risk_limits_enabled)
        self.max_daily_loss_ticker = float(max_daily_loss_ticker)
        self.max_daily_loss_total = float(max_daily_loss_total)
        self.max_cycles_per_ticker_day = int(max_cycles_per_ticker_day)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.max_spread_pct = float(max_spread_pct)
        self.min_trade_price = float(min_trade_price)
        self.max_gap_pct = float(max_gap_pct)
        self.block_delayed_live = bool(block_delayed_live)
        self.what_if_enabled = bool(what_if_enabled)
        self.stale_data_guard_enabled = bool(stale_data_guard_enabled)
        self.max_price_age_seconds = float(max_price_age_seconds)
        self.volatility_filter_enabled = bool(volatility_filter_enabled)
        self.max_recent_move_pct = float(max_recent_move_pct)
        self.session_timing_guard_enabled = bool(session_timing_guard_enabled)
        self.no_new_buy_first_minutes = int(no_new_buy_first_minutes)
        self.no_new_buy_last_minutes = int(no_new_buy_last_minutes)
        self.cancel_buy_before_close_minutes = int(cancel_buy_before_close_minutes)
        if reference_anchor is not None:
            try:
                ref = float(reference_anchor)
                if ref > 0:
                    self.reference_anchor = ref
                    self.reference_label = reference_label or "current reference"
            except Exception:
                pass
        signature = (
            self.initial_drop_pct,
            self.buy_rebound_pct,
            self.minimum_profit_pct,
            self.sell_trail_pct,
            self.reference_anchor,
            self.reference_label,
            self.protective_sell_enabled,
            self.protective_sell_trail_pct,
            self.slippage_buffer_enabled,
            self.slippage_buffer_pct,
            self.hard_risk_limits_enabled,
            self.max_daily_loss_ticker,
            self.max_daily_loss_total,
            self.max_cycles_per_ticker_day,
            self.max_consecutive_losses,
            self.max_spread_pct,
            self.min_trade_price,
            self.max_gap_pct,
            self.block_delayed_live,
            self.what_if_enabled,
            self.stale_data_guard_enabled,
            self.max_price_age_seconds,
            self.volatility_filter_enabled,
            self.max_recent_move_pct,
            self.session_timing_guard_enabled,
            self.no_new_buy_first_minutes,
            self.no_new_buy_last_minutes,
            self.cancel_buy_before_close_minutes,
        )
        if signature == getattr(self, "_last_values_signature", None):
            return
        self._last_values_signature = signature
        self.update()

    @staticmethod
    def _pct_vs(value: float, reference: float) -> str:
        if reference <= 0:
            return "-"
        return f"{((value / reference) - 1.0) * 100.0:+.2f}%"

    @staticmethod
    def _onoff(value: bool) -> str:
        return "ON" if value else "OFF"

    def _draw_arrow(self, painter: QPainter, x1: float, y: float, x2: float, color: QColor) -> None:
        painter.save()
        painter.setPen(QPen(color, 1.6))
        painter.drawLine(QPointF(x1, y), QPointF(x2, y))
        painter.drawLine(QPointF(x2, y), QPointF(x2 - 7, y - 5))
        painter.drawLine(QPointF(x2, y), QPointF(x2 - 7, y + 5))
        painter.restore()

    def _draw_block(self, painter: QPainter, rect: QRectF, title: str, value: str, small: str, color: QColor) -> None:
        painter.save()
        painter.setPen(QPen(color, 1.5))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(rect, 8, 8)

        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPointSize(8)
        painter.setFont(title_font)
        painter.setPen(QColor("#111827"))
        painter.drawText(rect.adjusted(7, 6, -7, -50), Qt.AlignCenter | Qt.TextWordWrap, title)

        value_font = QFont(painter.font())
        value_font.setBold(True)
        value_font.setPointSize(10)
        painter.setFont(value_font)
        painter.setPen(color)
        painter.drawText(rect.adjusted(7, 30, -7, -28), Qt.AlignCenter | Qt.TextWordWrap, value)

        small_font = QFont(painter.font())
        small_font.setBold(False)
        small_font.setPointSize(7)
        painter.setFont(small_font)
        painter.setPen(QColor("#4b5563"))
        painter.drawText(rect.adjusted(7, 58, -7, -5), Qt.AlignCenter | Qt.TextWordWrap, small)
        painter.restore()

    def _draw_lane_label(self, painter: QPainter, x: float, y: float, text: str) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor("#374151"))
        painter.drawText(QRectF(x, y, 88, 28), Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()

    def _draw_hover_overlay(self, painter: QPainter, plot: QRectF, targets: list[tuple[float, float, str]], min_v: float, max_v: float) -> None:
        if self._hover_pos is None:
            return
        hover_x = float(self._hover_pos.x())
        hover_y = float(self._hover_pos.y())
        if not plot.contains(QPointF(hover_x, hover_y)):
            return
        visible_targets = [target for target in targets if (plot.left() - 24) <= target[0] <= (plot.right() + 24)]
        nearest = min(visible_targets, key=lambda item: abs(item[0] - hover_x) + abs(item[1] - hover_y) * 0.25, default=None)
        price_at_cursor = max_v - ((hover_y - plot.top()) / max(1.0, plot.height())) * (max_v - min_v)
        text = f"Cursor {_format_currency(price_at_cursor)}"
        if nearest is not None:
            text = f"{nearest[2]}\nCursor {_format_currency(price_at_cursor)}"
        painter.save()
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.DashLine))
        painter.drawLine(QPointF(hover_x, plot.top()), QPointF(hover_x, plot.bottom()))
        painter.drawLine(QPointF(plot.left(), hover_y), QPointF(plot.right(), hover_y))
        if nearest is not None:
            painter.setPen(QPen(QColor("#111827"), 1))
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(QPointF(nearest[0], nearest[1]), 4, 4)
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        lines = text.splitlines()[:4]
        label_width = min(260, max(142, max(metrics.horizontalAdvance(line) for line in lines) + 16))
        label_height = 18 + 15 * len(lines)
        label_x = hover_x + 12 if hover_x < plot.center().x() else hover_x - label_width - 12
        label_y = hover_y + 12 if hover_y < plot.center().y() else hover_y - label_height - 12
        label_x = max(plot.left() + 4, min(label_x, plot.right() - label_width - 4))
        label_y = max(plot.top() + 4, min(label_y, plot.bottom() - label_height - 4))
        label_rect = QRectF(label_x, label_y, label_width, label_height)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(label_rect, 6, 6)
        painter.setPen(QColor("#111827"))
        painter.drawText(label_rect.adjusted(8, 4, -8, -4), Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))
        painter.restore()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(6, 6, -6, -6)
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#c7cbd1"), 1))
        painter.drawRoundedRect(QRectF(rect), 8, 8)

        levels = projected_minimum_profit_levels(
            self.initial_drop_pct,
            self.buy_rebound_pct,
            self.minimum_profit_pct,
            self.sell_trail_pct,
            anchor=max(0.0001, float(self.reference_anchor or 100.0)),
            slippage_buffer_enabled=self.slippage_buffer_enabled,
            slippage_buffer_pct=self.slippage_buffer_pct,
        )
        anchor = levels["anchor"]
        drop = levels["drop_trigger"]
        projected_buy = levels["projected_buy_stop"]
        sizing_price = levels.get("buy_sizing_price", projected_buy)
        minimum_sell_stop = levels["minimum_sell_stop"]
        required_last = levels["required_last_price"]
        protective_stop = projected_buy * (1.0 - max(0.0, self.protective_sell_trail_pct) / 100.0)

        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.setPen(QColor("#111827"))
        painter.drawText(rect.adjusted(12, 8, -12, -8), Qt.AlignLeft | Qt.AlignTop, "Strategy input map")

        small_font = QFont(painter.font())
        small_font.setBold(False)
        small_font.setPointSize(8)
        painter.setFont(small_font)
        painter.setPen(QColor("#5b6270"))
        painter.drawText(
            rect.adjusted(12, 34, -12, -6),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            f"Uses current input values. Baseline: {_format_price(anchor)} ({self.reference_label}).",
        )

        lane_label_w = 86.0
        gap = 10.0
        block_count = 4
        available_w = max(620.0, rect.width() - 28 - lane_label_w)
        block_w = max(135.0, (available_w - (block_count - 1) * gap) / block_count)
        block_h = 90.0
        start_x = rect.left() + 14 + lane_label_w
        entry_y = rect.top() + 72
        exit_y = entry_y + 116
        guard_y = exit_y + 116
        safety_y = guard_y + 116

        self._draw_lane_label(painter, rect.left() + 16, entry_y + 24, "Entry")
        self._draw_lane_label(painter, rect.left() + 16, exit_y + 24, "Exit")
        self._draw_lane_label(painter, rect.left() + 16, guard_y + 24, "Risk")
        self._draw_lane_label(painter, rect.left() + 16, safety_y + 24, "Safety")

        entry_blocks = [
            ("Anchor", _format_price(anchor), "0.00%", QColor("#374151")),
            ("Drop trigger", _format_price(drop), f"-{self.initial_drop_pct:.2f}%", QColor("#c2410c")),
            (("BUY market ref" if self.buy_rebound_pct <= 0 else "BUY trailing-stop"), _format_price(projected_buy), ("trailing OFF" if self.buy_rebound_pct <= 0 else f"rebound {self.buy_rebound_pct:.2f}%"), QColor("#7c3aed")),
            ("Sizing price", _format_price(sizing_price), (f"slip +{self.slippage_buffer_pct:.2f}%" if self.slippage_buffer_enabled else "no slippage buffer"), QColor("#475569")),
        ]
        exit_blocks = [
            ("Buy reference", _format_price(projected_buy), self._pct_vs(projected_buy, anchor), QColor("#7c3aed")),
            ("Protective SELL", _format_price(protective_stop) if self.protective_sell_enabled else "OFF", (f"trail {self.protective_sell_trail_pct:.2f}%" if self.protective_sell_enabled else "optional"), QColor("#dc2626")),
            ("Min SELL stop", _format_price(minimum_sell_stop), f"profit {self.minimum_profit_pct:.2f}%", QColor("#047857")),
            (("SELL market trigger" if self.sell_trail_pct <= 0 else "Place final SELL"), _format_price(required_last), ("trailing OFF" if self.sell_trail_pct <= 0 else f"trail {self.sell_trail_pct:.2f}%"), QColor("#1d4ed8")),
        ]
        guard_blocks = [
            ("Hard risk", self._onoff(self.hard_risk_limits_enabled), f"ticker ${self.max_daily_loss_ticker:,.0f} / total ${self.max_daily_loss_total:,.0f}", QColor("#b45309")),
            ("Cycle limits", ("OFF" if self.max_cycles_per_ticker_day <= 0 else f"max {self.max_cycles_per_ticker_day} total"), f"loss streak {self.max_consecutive_losses}", QColor("#b45309")),
            ("Liquidity", f"spread {self.max_spread_pct:.2f}%", f"min {_format_currency(self.min_trade_price, 2)} / gap {self.max_gap_pct:.2f}%", QColor("#b45309")),
            ("Live data", self._onoff(self.block_delayed_live), "blocks delayed/frozen live orders", QColor("#b45309")),
        ]
        safety_blocks = [
            ("What-if", self._onoff(self.what_if_enabled), "IBKR margin pre-check", QColor("#2563eb")),
            ("Fresh data", self._onoff(self.stale_data_guard_enabled), f"price age <= {self.max_price_age_seconds:.1f}s", QColor("#2563eb")),
            ("Volatility", self._onoff(self.volatility_filter_enabled), f"recent move <= {self.max_recent_move_pct:.2f}%", QColor("#2563eb")),
            ("Open/close", self._onoff(self.session_timing_guard_enabled), f"first {self.no_new_buy_first_minutes}m / last {self.no_new_buy_last_minutes}m", QColor("#2563eb")),
        ]

        for lane_y, blocks in [(entry_y, entry_blocks), (exit_y, exit_blocks), (guard_y, guard_blocks), (safety_y, safety_blocks)]:
            previous_right: Optional[float] = None
            for idx, (title, value, small, color) in enumerate(blocks):
                x = start_x + idx * (block_w + gap)
                block_rect = QRectF(x, lane_y, block_w, block_h)
                self._draw_block(painter, block_rect, title, value, small, color)
                if previous_right is not None and lane_y != guard_y:
                    self._draw_arrow(painter, previous_right + 4, lane_y + block_h / 2, x - 4, QColor("#9ca3af"))
                previous_right = x + block_w

        painter.setPen(QColor("#5b6270"))
        painter.drawText(
            QRectF(rect.left() + 14, rect.bottom() - 24, rect.width() - 28, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Trailing values above 0 use native TWS/IB Gateway trailing-stop orders; 0 uses market orders at the configured trigger.",
        )


class StrategyGraphWidget(QWidget):
    """Live price and strategy-level chart drawn without extra dependencies."""

    def __init__(self):
        super().__init__()
        self._cycle: Optional[dict[str, Any]] = None
        self._price_snapshot: Optional[dict[str, Any]] = None
        self._strategy: Optional[StrategySettings] = None
        self._history: deque[tuple[float, float]] = deque()
        self._history_max_points = 21600
        self._history_max_age_seconds = 6 * 60 * 60
        self._cycle_key: Optional[str] = None
        self._last_plot_rect: Optional[QRectF] = None
        self._last_plot_time_range: Optional[tuple[float, float]] = None
        self._hover_sample: Optional[tuple[float, float]] = None
        self._hover_point: Optional[tuple[float, float]] = None
        self.setMouseTracking(True)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def update_data(
        self,
        cycle: Optional[dict[str, Any]],
        price_snapshot: Optional[dict[str, Any]],
        strategy: StrategySettings,
        *,
        repaint: bool = True,
    ) -> None:
        key = str((cycle or {}).get("id") or f"IDLE:{strategy.normalized_ticker()}")
        if key != self._cycle_key:
            self._history.clear()
            self._hover_point = None
            self._cycle_key = key
        self._cycle = cycle or None
        self._price_snapshot = price_snapshot or None
        self._strategy = strategy

        price = _float_or_none((price_snapshot or {}).get("price"))
        if price is None and cycle:
            price = _float_or_none(cycle.get("last_price"))
        if price is not None and price > 0:
            now = time.time()
            if not self._history or now - self._history[-1][0] >= 1.0 or abs(price - self._history[-1][1]) > 1e-9:
                self._history.append((now, price))
                self._prune_history(now)
        if repaint:
            self.update()

    def _prune_history(self, now: Optional[float] = None) -> None:
        """Keep a rolling graph buffer so the app can run indefinitely.

        The strategy itself always uses the latest API snapshot. This buffer is
        only for the visible graph; it is deliberately bounded by both time and
        number of points to avoid memory growth during multi-day runs.
        """
        if now is None:
            now = time.time()
        max_age = float(self._history_max_age_seconds)
        while self._history and now - self._history[0][0] > max_age:
            self._history.popleft()
        max_points = int(self._history_max_points)
        while len(self._history) > max_points:
            self._history.popleft()

    def _levels(self) -> list[tuple[str, float, QColor, str]]:
        cycle = self._cycle or {}
        stage = str(cycle.get("stage") or "")
        price_snapshot = self._price_snapshot or {}
        levels: list[tuple[str, float, QColor, str]] = []

        def add(name: str, value: Any, color: str, tag: str) -> None:
            f = _float_or_none(value)
            if f is not None and f > 0:
                levels.append((name, f, QColor(color), tag))

        history_prices = [p for _t, p in self._history if p > 0]
        native_diag = dict(price_snapshot.get("native_order_trigger") or {})
        raw_last = native_diag.get("raw_last_value")

        current_price = _float_or_none(price_snapshot.get("price"))
        if current_price is None and history_prices:
            current_price = history_prices[-1]

        if not stage or stage in {Stage.STOPPED.value, Stage.IDLE.value}:
            add("Current price", current_price, "#111827", "last usable app price")
            return levels
        if stage == Stage.WAIT_INITIAL_DROP.value:
            add("Anchor", cycle.get("anchor_price"), "#374151", "Stage 1 reference")
            add("Initial drop trigger", cycle.get("drop_trigger_price"), "#d97706", "Stage 1 trigger")
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            add("BUY initial stop", cycle.get("buy_initial_trail_stop_price"), "#2563eb", "submitted stop")
            pct = _float_or_none(cycle.get("buy_rebound_trail_pct"))
            if pct is None and self._strategy is not None:
                pct = float(self._strategy.buy_rebound_trail_pct)
            if history_prices and pct is not None:
                low = min(history_prices)
                add("Estimated current BUY stop", low * (1.0 + pct / 100.0), "#1d4ed8", "app-observed trail estimate")
            add("Raw Last trigger diagnostic", raw_last, "#7c3aed", "broker trigger data")
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            add("Average buy", cycle.get("avg_buy_price"), "#111827", "executed buy")
            add("Minimum-profit trigger", cycle.get("rise_trigger_price"), "#2563eb", "Stage 3 condition")
            add("Protective SELL stop", cycle.get("protective_sell_initial_stop_price"), "#dc2626", "protective order")
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            add("SELL initial stop", cycle.get("sell_initial_trail_stop_price"), "#16a34a", "submitted stop")
            pct = _float_or_none(cycle.get("sell_trailing_stop_pct"))
            if pct is None and self._strategy is not None:
                pct = float(self._strategy.sell_trailing_stop_pct)
            if history_prices and pct is not None:
                high = max(history_prices)
                add("Estimated current SELL stop", high * (1.0 - pct / 100.0), "#059669", "app-observed trail estimate")
            add("Raw Last trigger diagnostic", raw_last, "#7c3aed", "broker trigger data")
        elif stage == Stage.CYCLE_COMPLETE.value:
            add("Average buy", cycle.get("avg_buy_price"), "#2563eb", "completed BUY marker")
            add("Average sell", cycle.get("avg_sell_price"), "#16a34a", "completed SELL marker")
            add("Protective SELL fill", cycle.get("protective_avg_sell_price"), "#dc2626", "protective exit marker")
        else:
            # No active trading stage: keep the market graph alive but avoid
            # drawing projected levels from stale inputs or previous cycles.
            # Operators should not see anchor/drop/BUY/SELL levels until a real
            # cycle is started.
            add("Current price", current_price, "#111827", "last usable app price")
        return levels

    def _draw_hover_overlay(self, painter: QPainter, plot: QRectF, targets: list[tuple[float, float, str]], min_v: float, max_v: float) -> None:
        if self._hover_pos is None:
            return
        hover_x = float(self._hover_pos.x())
        hover_y = float(self._hover_pos.y())
        if not plot.contains(QPointF(hover_x, hover_y)):
            return
        visible_targets = [target for target in targets if (plot.left() - 24) <= target[0] <= (plot.right() + 24)]
        nearest = min(visible_targets, key=lambda item: abs(item[0] - hover_x) + abs(item[1] - hover_y) * 0.25, default=None)
        price_at_cursor = max_v - ((hover_y - plot.top()) / max(1.0, plot.height())) * (max_v - min_v)
        text = f"Cursor {_format_currency(price_at_cursor)}"
        if nearest is not None:
            text = f"{nearest[2]}\nCursor {_format_currency(price_at_cursor)}"
        painter.save()
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.DashLine))
        painter.drawLine(QPointF(hover_x, plot.top()), QPointF(hover_x, plot.bottom()))
        painter.drawLine(QPointF(plot.left(), hover_y), QPointF(plot.right(), hover_y))
        if nearest is not None:
            painter.setPen(QPen(QColor("#111827"), 1))
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(QPointF(nearest[0], nearest[1]), 4, 4)
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        lines = text.splitlines()[:4]
        label_width = min(260, max(142, max(metrics.horizontalAdvance(line) for line in lines) + 16))
        label_height = 18 + 15 * len(lines)
        label_x = hover_x + 12 if hover_x < plot.center().x() else hover_x - label_width - 12
        label_y = hover_y + 12 if hover_y < plot.center().y() else hover_y - label_height - 12
        label_x = max(plot.left() + 4, min(label_x, plot.right() - label_width - 4))
        label_y = max(plot.top() + 4, min(label_y, plot.bottom() - label_height - 4))
        label_rect = QRectF(label_x, label_y, label_width, label_height)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(label_rect, 6, 6)
        painter.setPen(QColor("#111827"))
        painter.drawText(label_rect.adjusted(8, 4, -8, -4), Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))
        painter.restore()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#c7cbd1"), 1))
        painter.drawRoundedRect(QRectF(rect), 8, 8)

        cycle = self._cycle or {}
        title = "Market and strategy graph"
        ticker = cycle.get("ticker") or (self._strategy.normalized_ticker() if self._strategy else "")
        if ticker:
            title += f" - {ticker}"
        stage = cycle.get("stage") or "Idle / price only"
        source = (self._price_snapshot or {}).get("source") or "-"

        painter.setPen(QColor("#111827"))
        title_font = QFont(painter.font())
        title_font.setBold(True)
        title_font.setPointSize(max(9, title_font.pointSize()))
        painter.setFont(title_font)
        painter.drawText(rect.adjusted(12, 8, -12, -8), Qt.AlignLeft | Qt.AlignTop, title)
        painter.setFont(QFont())
        painter.setPen(QColor("#5b6270"))
        painter.drawText(rect.adjusted(12, 33, -12, -8), Qt.AlignLeft | Qt.AlignTop, f"Stage: {stage} | Price source: {source} | Price line: last usable app price")

        legend_width = 285
        plot = QRectF(rect.left() + 72, rect.top() + 64, rect.width() - legend_width - 104, rect.height() - 116)
        legend = QRectF(plot.right() + 18, plot.top(), legend_width, plot.height())
        self._last_plot_rect = QRectF(plot)
        if self._history:
            self._last_plot_time_range = (float(self._history[0][0]), float(self._history[-1][0]))
        else:
            self._last_plot_time_range = None
        if plot.width() <= 30 or plot.height() <= 30:
            painter.setPen(QColor("#5b6270"))
            painter.drawText(QRectF(rect), Qt.AlignCenter, "Window too small for graph.")
            return

        levels = self._levels()
        values = [p for _t, p in self._history]
        values.extend(v for _name, v, _color, _tag in levels)
        if not values:
            painter.setPen(QColor("#5b6270"))
            painter.drawText(QRectF(rect), Qt.AlignCenter, "No price data yet. Confirm a ticker or start a cycle.")
            return
        min_v = min(values)
        max_v = max(values)
        if abs(max_v - min_v) < 1e-9:
            pad = max(0.01, max_v * 0.01)
        else:
            pad = (max_v - min_v) * 0.16
        min_v -= pad
        max_v += pad

        def map_y(value: float) -> float:
            return plot.bottom() - ((value - min_v) / max(1e-9, (max_v - min_v))) * plot.height()

        painter.setPen(QPen(QColor("#d7dae0"), 1))
        for i in range(6):
            y = plot.top() + plot.height() * i / 5
            value = max_v - (max_v - min_v) * i / 5
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#5b6270"))
            painter.drawText(QRectF(rect.left() + 4, y - 9, 66, 18), Qt.AlignRight | Qt.AlignVCenter, _format_currency(value, decimals=2))
            painter.setPen(QPen(QColor("#d7dae0"), 1))
        painter.setPen(QPen(QColor("#9ca3af"), 1))
        painter.drawRect(plot)

        if self._history:
            points: list[QPointF] = []
            if len(self._history) == 1:
                y = map_y(self._history[0][1])
                points = [QPointF(plot.left(), y), QPointF(plot.right(), y)]
            else:
                first_t = self._history[0][0]
                last_t = self._history[-1][0]
                span = max(1.0, last_t - first_t)
                for t, price in self._history:
                    x = plot.left() + ((t - first_t) / span) * plot.width()
                    points.append(QPointF(x, map_y(price)))
            painter.setPen(QPen(QColor("#111827"), 2))
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)
            current = points[-1]
            painter.setBrush(QColor("#111827"))
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawEllipse(current, 4, 4)
            latest_price = self._history[-1][1]
            _draw_text_box(
                painter,
                QRectF(min(current.x() + 8, plot.right() - 122), max(plot.top() + 4, current.y() - 13), 118, 24),
                f"Current {_format_currency(latest_price)}",
                QColor("#111827"),
                QColor("#ffffff"),
            )

            if self._hover_sample is not None:
                hover_t, hover_price = self._hover_sample
                first_t = self._history[0][0]
                last_t = self._history[-1][0]
                if first_t <= hover_t <= last_t:
                    span = max(1.0, last_t - first_t)
                    hx = plot.left() + ((hover_t - first_t) / span) * plot.width()
                    hy = map_y(hover_price)
                    painter.setPen(QPen(QColor("#2563eb"), 1, Qt.DotLine))
                    painter.drawLine(QPointF(hx, plot.top()), QPointF(hx, plot.bottom()))
                    painter.setBrush(QColor("#2563eb"))
                    painter.setPen(QPen(QColor("#ffffff"), 1))
                    painter.drawEllipse(QPointF(hx, hy), 5, 5)
                    hover_time = _format_utc_timestamp(hover_t, compact=True)
                    label = f"{hover_time}  {_format_currency(float(hover_price), decimals=4)}"
                    label_rect = QRectF(
                        min(max(plot.left() + 4, hx + 8), plot.right() - 178),
                        max(plot.top() + 4, hy - 30),
                        174,
                        26,
                    )
                    _draw_text_box(painter, label_rect, label, QColor("#2563eb"), QColor("#ffffff"))

        for _name, value, color, _tag in levels:
            y = map_y(value)
            painter.setPen(QPen(color, 1, Qt.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        if self._hover_point and self._history:
            hover_t, hover_price = self._hover_point
            first_t = float(self._history[0][0])
            last_t = float(self._history[-1][0])
            span = max(1.0, last_t - first_t)
            hover_x = plot.left() + ((float(hover_t) - first_t) / span) * plot.width()
            hover_y = map_y(float(hover_price))
            if plot.left() <= hover_x <= plot.right() and plot.top() <= hover_y <= plot.bottom():
                painter.setPen(QPen(QColor("#2563eb"), 1, Qt.DashLine))
                painter.drawLine(QPointF(hover_x, plot.top()), QPointF(hover_x, plot.bottom()))
                painter.drawLine(QPointF(plot.left(), hover_y), QPointF(plot.right(), hover_y))
                painter.setBrush(QColor("#2563eb"))
                painter.setPen(QPen(QColor("#ffffff"), 1))
                painter.drawEllipse(QPointF(hover_x, hover_y), 4, 4)
                time_text = _format_utc_timestamp(hover_t, compact=True)
                box_w = 180.0
                box_h = 40.0
                box_x = min(max(plot.left() + 6, hover_x + 10), plot.right() - box_w - 6)
                box_y = min(max(plot.top() + 6, hover_y - box_h - 10), plot.bottom() - box_h - 6)
                _draw_text_box(
                    painter,
                    QRectF(box_x, box_y, box_w, box_h),
                    f"{time_text}  {_format_currency(float(hover_price), decimals=4)}",
                    QColor("#1d4ed8"),
                    QColor("#eff6ff"),
                )

        painter.setPen(QPen(QColor("#c7cbd1"), 1))
        painter.setBrush(QColor("#f9fafb"))
        painter.drawRoundedRect(legend, 6, 6)
        painter.setPen(QColor("#111827"))
        painter.drawText(QRectF(legend.left() + 10, legend.top() + 8, legend.width() - 20, 18), Qt.AlignLeft | Qt.AlignVCenter, "Level list")

        row_y = legend.top() + 34
        row_h = 24
        if not levels:
            painter.setPen(QColor("#6b7280"))
            empty_text = "Only the current price is shown until a strategy cycle is running." if stage in {"", Stage.STOPPED.value, Stage.IDLE.value} else "No level data for this state."
            painter.drawText(QRectF(legend.left() + 12, row_y, legend.width() - 24, 44), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, empty_text)
        for name, value, color, tag in sorted(levels, key=lambda item: item[1], reverse=True)[:10]:
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(legend.left() + 10, row_y + 10), QPointF(legend.left() + 28, row_y + 10))
            painter.setPen(QColor("#111827"))
            painter.drawText(QRectF(legend.left() + 36, row_y, legend.width() - 116, 18), Qt.AlignLeft | Qt.AlignVCenter, name)
            painter.setPen(QColor("#5b6270"))
            painter.drawText(QRectF(legend.right() - 92, row_y, 82, 18), Qt.AlignRight | Qt.AlignVCenter, _format_currency(value))
            painter.setPen(QColor("#6b7280"))
            painter.drawText(QRectF(legend.left() + 36, row_y + 13, legend.width() - 46, 12), Qt.AlignLeft | Qt.AlignVCenter, tag)
            row_y += row_h
            if row_y > legend.bottom() - 28:
                break

        painter.setPen(QColor("#5b6270"))
        note = "Native trailing-stop orders are managed by TWS; market-order mode is used when a trailing field is 0. Active trail lines are estimates from prices observed by this app."
        if self._history:
            hours = self._history_max_age_seconds / 3600.0
            note = f"Rolling graph buffer: {len(self._history):,}/{self._history_max_points:,} points, max {hours:.0f}h | " + note
        painter.drawText(QRectF(rect.left() + 12, rect.bottom() - 32, rect.width() - 24, 24), Qt.AlignLeft | Qt.AlignVCenter, note)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        plot = self._last_plot_rect
        if plot is None or not self._history or not plot.contains(event.position()):
            self._hover_sample = None
            self._hover_point = None
            QToolTip.hideText()
            self.update()
            return super().mouseMoveEvent(event)
        first_t = float(self._history[0][0])
        last_t = float(self._history[-1][0])
        span = max(1.0, last_t - first_t)
        target_t = first_t + ((event.position().x() - plot.left()) / max(1.0, plot.width())) * span
        nearest_t, nearest_price = min(self._history, key=lambda row: abs(float(row[0]) - target_t))
        self._hover_sample = (float(nearest_t), float(nearest_price))
        self._hover_point = self._hover_sample
        time_text = _format_utc_timestamp(nearest_t)
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{time_text}\nPrice: {_format_currency(float(nearest_price), decimals=4)}",
            self,
        )
        self.update()
        return super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover_sample = None
        self._hover_point = None
        QToolTip.hideText()
        self.update()
        return super().leaveEvent(event)


class StrategyFlowchartWidget(QWidget):
    """Non-zoomable rendering of the implemented five-stage strategy flow.

    The widget draws to the available tab width so the right-side detail boxes
    remain visible. Text wraps inside the boxes rather than requiring zoom.
    """

    MIN_CANVAS_WIDTH = 760
    CANVAS_HEIGHT = 1580
    CARD_HEIGHT = 272.0
    CARD_GAP = 12.0

    def __init__(self):
        super().__init__()
        self._strategy = StrategySettings()
        self._cycle: Optional[dict[str, Any]] = None
        self._price_snapshot: Optional[dict[str, Any]] = None
        self._cards: list[FlowchartStageCard] = build_strategy_flowchart_cards(self._strategy)
        self._view_mode = "Full strategy"
        self._compact_mode = False
        self._refresh_canvas_size()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(1120, self._canvas_height())

    def _canvas_height(self) -> int:
        try:
            count = len(self._filtered_cards())
        except Exception:
            count = len(self._cards) or 5
        count = max(1, count)
        height = 140.0 + count * self.CARD_HEIGHT + max(0, count - 1) * self.CARD_GAP + 46.0
        return int(max(640.0, height))

    def _refresh_canvas_size(self) -> None:
        height = self._canvas_height()
        self.setMinimumSize(self.MIN_CANVAS_WIDTH, height)
        self.resize(max(self.width(), self.MIN_CANVAS_WIDTH), height)

    def update_data(self, cycle: Optional[dict[str, Any]], price_snapshot: Optional[dict[str, Any]], strategy: StrategySettings) -> bool:
        cards = build_strategy_flowchart_cards(strategy, cycle, price_snapshot)
        if cards == self._cards:
            self._cycle = cycle or None
            self._price_snapshot = price_snapshot or None
            self._strategy = strategy
            return False
        self._cycle = cycle or None
        self._price_snapshot = price_snapshot or None
        self._strategy = strategy
        self._cards = cards
        self._refresh_canvas_size()
        self.updateGeometry()
        self.update()
        return True

    def set_view_mode(self, mode: str) -> None:
        mode = str(mode or "Full strategy")
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._refresh_canvas_size()
        self.updateGeometry()
        self.update()

    def set_compact_mode(self, compact: bool) -> None:
        compact = bool(compact)
        if compact == self._compact_mode:
            return
        self._compact_mode = compact
        self._refresh_canvas_size()
        self.updateGeometry()
        self.update()

    def _active_stage_index(self) -> Optional[int]:
        cycle_stage = (self._cycle or {}).get("stage")
        try:
            return STAGE_ORDER.index(str(cycle_stage))
        except Exception:
            for idx, card in enumerate(self._cards):
                if card.active:
                    return idx
        return None

    def _filtered_cards(self) -> list[FlowchartStageCard]:
        cards = list(self._cards)
        mode = str(self._view_mode or "Full strategy").strip()
        if mode == "Full strategy":
            return cards
        if mode == "Entry path only":
            return [card for card in cards if card.stage in {Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE}]
        if mode == "Exit path only":
            return [card for card in cards if card.stage in {Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE, Stage.CYCLE_COMPLETE}]
        active_idx = self._active_stage_index()
        if mode == "Current cycle only":
            if active_idx is None:
                return cards
            start = max(0, active_idx - 1)
            end = min(len(cards), active_idx + 2)
            return cards[start:end]
        if mode == "Recovery path":
            if active_idx is None:
                return [card for card in cards if card.stage in {Stage.BUY_TRAIL_ACTIVE, Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE}]
            start = max(0, active_idx - 1)
            end = min(len(cards), active_idx + 2)
            return cards[start:end]
        return cards

    def _status_for_card(self, card: FlowchartStageCard) -> str:
        if card.active:
            return "Current"
        stage_text = str((self._cycle or {}).get("stage") or "")
        if stage_text in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            return "Blocked"
        if stage_text == Stage.CYCLE_COMPLETE.value:
            return "Done"
        active_idx = self._active_stage_index()
        try:
            card_idx = STAGE_ORDER.index(card.stage.value)
        except Exception:
            return "Pending"
        if active_idx is not None and card_idx < active_idx:
            return "Done"
        return "Pending"

    def _draw_round_box(self, painter: QPainter, rect: QRectF, fill: QColor, border: QColor, width: int = 1, radius: int = 12) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(border, width))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        painter.restore()

    def _draw_text(self, painter: QPainter, rect: QRectF, text: str, color: QColor, point_size: int = 9, bold: bool = False, align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignTop) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setPointSize(point_size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, align | Qt.TextWordWrap, text)
        painter.restore()

    def _draw_arrow(self, painter: QPainter, x: float, y1: float, y2: float, color: QColor) -> None:
        painter.save()
        painter.setPen(QPen(color, 2))
        painter.drawLine(QPointF(x, y1), QPointF(x, y2))
        painter.drawLine(QPointF(x, y2), QPointF(x - 8, y2 - 10))
        painter.drawLine(QPointF(x, y2), QPointF(x + 8, y2 - 10))
        painter.restore()

    def _draw_hover_overlay(self, painter: QPainter, plot: QRectF, targets: list[tuple[float, float, str]], min_v: float, max_v: float) -> None:
        if self._hover_pos is None:
            return
        hover_x = float(self._hover_pos.x())
        hover_y = float(self._hover_pos.y())
        if not plot.contains(QPointF(hover_x, hover_y)):
            return
        visible_targets = [target for target in targets if (plot.left() - 24) <= target[0] <= (plot.right() + 24)]
        nearest = min(visible_targets, key=lambda item: abs(item[0] - hover_x) + abs(item[1] - hover_y) * 0.25, default=None)
        price_at_cursor = max_v - ((hover_y - plot.top()) / max(1.0, plot.height())) * (max_v - min_v)
        text = f"Cursor {_format_currency(price_at_cursor)}"
        if nearest is not None:
            text = f"{nearest[2]}\nCursor {_format_currency(price_at_cursor)}"
        painter.save()
        painter.setPen(QPen(QColor("#64748b"), 1, Qt.DashLine))
        painter.drawLine(QPointF(hover_x, plot.top()), QPointF(hover_x, plot.bottom()))
        painter.drawLine(QPointF(plot.left(), hover_y), QPointF(plot.right(), hover_y))
        if nearest is not None:
            painter.setPen(QPen(QColor("#111827"), 1))
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawEllipse(QPointF(nearest[0], nearest[1]), 4, 4)
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        lines = text.splitlines()[:4]
        label_width = min(260, max(142, max(metrics.horizontalAdvance(line) for line in lines) + 16))
        label_height = 18 + 15 * len(lines)
        label_x = hover_x + 12 if hover_x < plot.center().x() else hover_x - label_width - 12
        label_y = hover_y + 12 if hover_y < plot.center().y() else hover_y - label_height - 12
        label_x = max(plot.left() + 4, min(label_x, plot.right() - label_width - 4))
        label_y = max(plot.top() + 4, min(label_y, plot.bottom() - label_height - 4))
        label_rect = QRectF(label_x, label_y, label_width, label_height)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(label_rect, 6, 6)
        painter.setPen(QColor("#111827"))
        painter.drawText(label_rect.adjusted(8, 4, -8, -4), Qt.AlignLeft | Qt.AlignTop, "\n".join(lines))
        painter.restore()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        canvas_w = max(float(self.MIN_CANVAS_WIDTH), float(self.width()))
        canvas_h = max(float(self._canvas_height()), float(self.height()))
        painter.fillRect(QRectF(0, 0, canvas_w, canvas_h), QColor("#f6f7f9"))

        cycle = self._cycle or {}
        ticker = cycle.get("ticker") or self._strategy.normalized_ticker() or "-"
        stage = cycle.get("stage") or "Idle / price only"
        price = (self._price_snapshot or {}).get("price")
        margin_x = 16.0
        header = QRectF(margin_x, 18, canvas_w - 2 * margin_x, 102)
        self._draw_round_box(painter, header, QColor("#ffffff"), QColor("#c7cbd1"), 1)
        self._draw_text(painter, QRectF(header.left() + 22, header.top() + 14, header.width() - 44, 30), "Strategy flowchart - code-aligned current parameters", QColor("#111827"), 16, True)
        self._draw_text(
            painter,
            QRectF(header.left() + 22, header.top() + 52, header.width() - 44, 44),
            f"Ticker: {ticker} | Current stage: {stage} | Selected API price: {_format_field_value('price', price)} | RTH-only: {'on' if self._strategy.rth_only else 'off'} | Protective SELL: {'on' if self._strategy.protective_sell_enabled else 'off'} | Slippage buffer: {'on' if self._strategy.slippage_buffer_enabled else 'off'} | Hard risk limits: {'on' if self._strategy.hard_risk_limits_enabled else 'off'}",
            QColor("#4b5563"),
            9,
            False,
        )

        x = margin_x
        y = 140.0
        card_w = canvas_w - 2 * margin_x
        card_h = self.CARD_HEIGHT
        gap = self.CARD_GAP
        stage_colors = {
            Stage.WAIT_INITIAL_DROP: (QColor("#fff7ed"), QColor("#c2410c")),
            Stage.BUY_TRAIL_ACTIVE: (QColor("#f5f3ff"), QColor("#7c3aed")),
            Stage.WAIT_RISE_TRIGGER: (QColor("#eff6ff"), QColor("#1d4ed8")),
            Stage.SELL_TRAIL_ACTIVE: (QColor("#ecfdf5"), QColor("#047857")),
            Stage.CYCLE_COMPLETE: (QColor("#f8fafc"), QColor("#334155")),
        }
        cards = self._filtered_cards()
        if not cards:
            cards = list(self._cards)
        for idx, card in enumerate(cards):
            top = y + idx * (card_h + gap)
            fill, accent = stage_colors.get(card.stage, (QColor("#ffffff"), QColor("#374151")))
            stage_status = self._status_for_card(card)
            if stage_status == "Done":
                fill, accent = QColor("#ecfdf5"), QColor("#16a34a")
            elif stage_status == "Blocked":
                fill, accent = QColor("#fef2f2"), QColor("#dc2626")
            elif stage_status == "Pending":
                fill, accent = QColor("#f3f4f6"), QColor("#6b7280")
            else:
                fill, accent = QColor("#eff6ff"), QColor("#2563eb")
            border = accent if card.active or stage_status in {"Current", "Blocked"} else QColor("#c7cbd1")
            border_width = 4 if card.active or stage_status == "Current" else 1
            rect = QRectF(x, top, card_w, card_h)
            self._draw_round_box(painter, rect, QColor("#ffffff"), border, border_width)
            painter.fillRect(QRectF(rect.left(), rect.top(), 14, rect.height()), accent)
            painter.fillRect(QRectF(rect.left() + 14, rect.top(), rect.width() - 14, 38), fill)

            title = card.title + (f"  -  {stage_status.upper()}" if stage_status != "Pending" else "")
            self._draw_text(painter, QRectF(rect.left() + 30, rect.top() + 8, rect.width() - 150, 30), title, QColor("#111827"), 12, True)
            badge = QRectF(rect.right() - 96, rect.top() + 8, 64, 24)
            self._draw_round_box(painter, badge, fill, accent, 1, 8)
            stage_number = _stage_index(card.stage.value) or (idx + 1)
            self._draw_text(painter, badge.adjusted(0, 3, 0, 0), f"{stage_number}/5", accent, 9, True, Qt.AlignCenter)

            inner_left = rect.left() + 20
            inner_top = rect.top() + 50
            inner_width = rect.width() - 40
            box_gap = 8
            order_w = max(112.0, inner_width * 0.17)
            trigger_w = max(188.0, inner_width * 0.28)
            details_w = inner_width - order_w - trigger_w - 2 * box_gap
            if details_w < 260.0:
                shortage = 260.0 - details_w
                order_w = max(105.0, order_w - shortage * 0.35)
                trigger_w = max(170.0, trigger_w - shortage * 0.65)
                details_w = inner_width - order_w - trigger_w - 2 * box_gap
            box_h = 198.0
            order_box = QRectF(inner_left, inner_top, order_w, box_h)
            trigger_box = QRectF(order_box.right() + box_gap, inner_top, trigger_w, box_h)
            details_box = QRectF(trigger_box.right() + box_gap, inner_top, max(180.0, details_w), box_h)
            for box_rect in (order_box, trigger_box, details_box):
                self._draw_round_box(painter, box_rect, QColor("#f9fafb"), QColor("#d1d5db"), 1, 8)

            self._draw_text(painter, order_box.adjusted(10, 8, -10, -8), f"Stage status: {stage_status}\n\nBroker order type used\n{card.order_summary}", accent, 9, True)
            self._draw_text(painter, trigger_box.adjusted(10, 8, -10, -8), "Calculated trigger values\n" + card.trigger_summary, QColor("#111827"), 9, True)
            detail_lines = ["Input values used and live guard status:"] + [f"- {line}" for line in card.details]
            detail_text = "\n".join(detail_lines)
            self._draw_text(painter, details_box.adjusted(10, 8, -10, -8), detail_text, QColor("#374151"), 9, False)

            if idx < len(cards) - 1:
                self._draw_arrow(painter, rect.center().x(), rect.bottom() + 4, rect.bottom() + gap - 4, QColor("#9ca3af"))


class FlowchartPanel(QWidget):
    """Non-zoomable strategy flowchart tab with current/previous-cycle data selector."""

    def __init__(self):
        super().__init__()
        self._current_cycle: Optional[dict[str, Any]] = None
        self._current_price_snapshot: Optional[dict[str, Any]] = None
        self._current_strategy = StrategySettings()
        self._history_rows: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Flowchart data"))
        self.history_combo = QComboBox()
        self.history_combo.addItem("Current strategy / active cycle", None)
        selector_row.addWidget(self.history_combo, 1)
        selector_row.addWidget(QLabel("View"))
        self.view_combo = QComboBox()
        self.view_combo.addItems([
            "Full strategy",
            "Current cycle only",
            "Entry path only",
            "Exit path only",
            "Recovery path",
        ])
        selector_row.addWidget(self.view_combo)
        root.addLayout(selector_row)

        self.explanation_label = QLabel(
            "Live explanation view: highlights the current stage, the condition being waited for, the next order/action, enabled protections, and active guards."
        )
        self.explanation_label.setObjectName("Muted")
        self.explanation_label.setWordWrap(True)
        root.addWidget(self.explanation_label)

        self.scroll = QScrollArea()
        # Keep the custom-painted flowchart at its calculated canvas height.
        # Widget-resizable mode can compress custom child canvases during tab
        # switches, making lower Full strategy steps look missing.
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.flowchart = StrategyFlowchartWidget()
        self.scroll.setWidget(self.flowchart)
        root.addWidget(self.scroll, 1)
        self.history_combo.currentIndexChanged.connect(self._redraw)
        self.view_combo.currentIndexChanged.connect(self._redraw)
        QTimer.singleShot(0, self._sync_flowchart_canvas)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_flowchart_canvas)

    def _sync_flowchart_canvas(self) -> None:
        if not hasattr(self, "flowchart") or not hasattr(self, "scroll"):
            return
        try:
            viewport_width = int(self.scroll.viewport().width())
        except Exception:
            viewport_width = int(self.width())
        target_width = max(StrategyFlowchartWidget.MIN_CANVAS_WIDTH, viewport_width - 4)
        target_height = self.flowchart._canvas_height()
        self.flowchart.setMinimumSize(target_width, target_height)
        self.flowchart.resize(target_width, target_height)
        self.flowchart.updateGeometry()
        widget = self.scroll.widget()
        if widget is not None:
            widget.updateGeometry()
        self.scroll.viewport().update()
        self.scroll.updateGeometry()

    def set_compact_mode(self, compact: bool) -> None:
        compact = bool(compact)
        if hasattr(self, "history_combo"):
            # Selecting current or completed-cycle flowchart data is an
            # operational navigation control, not a diagnostic. Keep it
            # available in Simple, Advanced, and Debug modes. Explicitly
            # restore visibility in case the widget was hidden by an older
            # view-mode state before this release.
            self.history_combo.setVisible(True)
        if hasattr(self, "explanation_label"):
            self.explanation_label.setVisible(not compact)
        if hasattr(self, "flowchart"):
            # Simple mode hides the explanatory paragraph but must not reduce
            # "Full strategy" to only three cards. The view selector remains
            # the sole filter for which stages are rendered.
            self.flowchart.set_compact_mode(False)
            QTimer.singleShot(0, self._sync_flowchart_canvas)

    def set_history_rows(self, rows: list[dict[str, Any]]) -> None:
        """Populate the flowchart selector with completed cycles from history."""
        rows = list(rows or [])
        if rows == self._history_rows:
            return
        current_key = self.history_combo.currentData()
        self._history_rows = rows
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        self.history_combo.addItem("Current strategy / active cycle", None)
        for idx, row in enumerate(self._history_rows):
            ticker = row.get("ticker") or "-"
            cycle = row.get("cycle_number") or "-"
            buy = row.get("buy_filled_at") or "-"
            net = row.get("net_pnl")
            net_text = _format_currency(net, 2) if net is not None else "-"
            self.history_combo.addItem(f"Previous trade: {ticker} cycle {cycle} | buy {buy} | net {net_text}", idx)
        if current_key is not None:
            for i in range(self.history_combo.count()):
                if self.history_combo.itemData(i) == current_key:
                    self.history_combo.setCurrentIndex(i)
                    break
        self.history_combo.blockSignals(False)
        self._redraw()

    def update_data(self, cycle: Optional[dict[str, Any]], price_snapshot: Optional[dict[str, Any]], strategy: StrategySettings) -> None:
        self._current_cycle = cycle or None
        self._current_price_snapshot = price_snapshot or None
        self._current_strategy = strategy
        self._redraw()

    def _strategy_from_history_row(self, row: dict[str, Any]) -> StrategySettings:
        data = asdict(self._current_strategy)
        # History rows use enriched/export-oriented column names such as
        # configured_min_profit_pct. Keep the direct names as fallbacks so the
        # selector also works with raw cycle dictionaries in tests.
        mapping = {
            "ticker": ("ticker",),
            "investment_amount": ("investment_amount", "budget"),
            "initial_drop_pct": ("initial_drop_pct", "configured_initial_drop_pct"),
            "buy_rebound_trail_pct": ("buy_rebound_trail_pct", "configured_buy_rebound_pct"),
            "rise_trigger_pct": ("rise_trigger_pct", "configured_min_profit_pct"),
            "sell_trailing_stop_pct": ("sell_trailing_stop_pct", "configured_sell_trail_pct"),
            "protective_sell_enabled": ("protective_sell_enabled",),
            "protective_sell_trailing_stop_pct": (
                "protective_sell_trailing_stop_pct",
                "configured_protective_sell_trail_pct",
            ),
            "slippage_buffer_enabled": ("slippage_buffer_enabled",),
            "slippage_buffer_pct": ("slippage_buffer_pct", "configured_slippage_buffer_pct"),
            "hard_risk_limits_enabled": ("hard_risk_limits_enabled",),
            "max_daily_loss_ticker": ("max_daily_loss_ticker",),
            "max_daily_loss_total": ("max_daily_loss_total",),
            "max_cycles_per_ticker_day": ("max_cycles_per_ticker_day",),
            "max_consecutive_losses": ("max_consecutive_losses",),
            "max_spread_pct": ("max_spread_pct",),
            "min_trade_price": ("min_trade_price",),
            "max_gap_from_prev_close_pct": ("max_gap_from_prev_close_pct",),
            "block_delayed_data_in_live": ("block_delayed_data_in_live",),
        }
        for dest, sources in mapping.items():
            for src in sources:
                if row.get(src) is not None:
                    data[dest] = row.get(src)
                    break
        return StrategySettings(**data)

    def _redraw(self) -> None:
        if not hasattr(self, "flowchart"):
            return
        if hasattr(self, "view_combo"):
            self.flowchart.set_view_mode(self.view_combo.currentText())
        selected = self.history_combo.currentData() if hasattr(self, "history_combo") else None
        if selected is not None:
            try:
                row = self._history_rows[int(selected)]
            except Exception:
                row = {}
            strategy = self._strategy_from_history_row(row) if row else self._current_strategy
            historical_cycle = dict(row or {})
            historical_cycle.setdefault("stage", Stage.CYCLE_COMPLETE.value)
            if self.flowchart.update_data(historical_cycle or None, None, strategy):
                QTimer.singleShot(0, self._sync_flowchart_canvas)
            return
        if self.flowchart.update_data(self._current_cycle, self._current_price_snapshot, self._current_strategy):
            QTimer.singleShot(0, self._sync_flowchart_canvas)


class PricePanel(QGroupBox):
    DATA_MODE_LABELS = {0: "Auto best", 1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed frozen"}

    def __init__(self):
        super().__init__("Price data monitor - IBKR API feed")
        self._last_price_snapshot: dict[str, Any] = {}
        root = QVBoxLayout(self)
        top = QHBoxLayout()

        self.big_price = QLabel("-")
        self.big_price.setObjectName("BigPrice")
        self.big_price.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.big_price, 1)

        right = QVBoxLayout()
        self.price_status = QLabel("No price request yet")
        self.price_status.setObjectName("PriceStatus")
        self.price_source = QLabel("Source: -")
        self.price_source.setObjectName("Muted")
        self.price_refresh = QLabel("Subscription update: -")
        self.price_refresh.setObjectName("Muted")
        self.api_indicator_dot = QLabel("●")
        self.api_indicator_dot.setObjectName("ApiIndicatorBad")
        self.api_indicator_dot.setAlignment(Qt.AlignCenter)
        self.api_indicator_dot.setMinimumWidth(24)
        self.api_indicator_text = QLabel("API data: not received")
        self.api_indicator_text.setObjectName("ApiIndicatorText")
        self.api_indicator_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        indicator_row = QHBoxLayout()
        indicator_row.setContentsMargins(0, 0, 0, 0)
        indicator_row.addWidget(self.api_indicator_dot)
        indicator_row.addWidget(self.api_indicator_text, 1)
        right.addWidget(self.price_status)
        right.addWidget(self.price_source)
        right.addWidget(self.price_refresh)
        right.addLayout(indicator_row)
        top.addLayout(right, 2)
        root.addLayout(top)

        self.progress_label = QLabel("Strategy progress: -")
        self.progress_label.setObjectName("Muted")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        root.addWidget(self.progress_label)
        root.addWidget(self.progress_bar)

        summary_grid = QGridLayout()
        self.price_summary_cards: dict[str, MetricCard] = {}
        for idx, title in enumerate([
            "Selected price",
            "Source",
            "Freshness",
            "Data mode",
            "Bid / Ask / Spread",
            "RTH status",
            "Current time",
        ]):
            card = MetricCard(title)
            self.price_summary_cards[title] = card
            summary_grid.addWidget(card, idx // 3, idx % 3)
        root.addLayout(summary_grid)

        raw_row = QHBoxLayout()
        self.raw_api_toggle = QPushButton("Show raw API fields")
        self.raw_api_toggle.setCheckable(True)
        self.raw_api_toggle.setChecked(False)
        self.raw_api_toggle.toggled.connect(self._set_raw_table_visible)
        raw_note = QLabel("Raw IBKR fields remain available for debugging but are collapsed during normal supervision.")
        raw_note.setObjectName("Muted")
        raw_note.setWordWrap(True)
        raw_row.addWidget(self.raw_api_toggle)
        raw_row.addWidget(raw_note, 1)
        root.addLayout(raw_row)

        self.fields_table = QTableWidget(0, 10)
        self.fields_table.setHorizontalHeaderLabels(["Field", "Value", "Field", "Value", "Field", "Value", "Field", "Value", "Field", "Value"])
        _polish_table_widget(self.fields_table, stretch_last=False, expanding=True)
        self.fields_table.setMinimumHeight(240)
        self.fields_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.fields_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header = self.fields_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(10):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        root.addWidget(self.fields_table, 1)
        self.fields_table.setVisible(False)

    def _set_raw_table_visible(self, visible: bool) -> None:
        if hasattr(self, "fields_table"):
            self.fields_table.setVisible(bool(visible))
            if visible:
                self._update_field_table(getattr(self, "_last_price_snapshot", {}) or {})
        if hasattr(self, "raw_api_toggle"):
            self.raw_api_toggle.setText("Hide raw API fields" if visible else "Show raw API fields")

    def set_debug_mode(self, enabled: bool) -> None:
        if hasattr(self, "raw_api_toggle"):
            self.raw_api_toggle.setChecked(bool(enabled))
            self._set_raw_table_visible(bool(enabled))

    def update_data(self, cycle: Optional[dict[str, Any]], price_snapshot: Optional[dict[str, Any]]) -> None:
        price_snapshot = price_snapshot or {}
        self._last_price_snapshot = price_snapshot
        price = price_snapshot.get("price")
        price_text = _format_price(price)
        if self.big_price.text() != price_text:
            self.big_price.setText(price_text)

        requested = self.DATA_MODE_LABELS.get(price_snapshot.get("requested_market_data_type"), str(price_snapshot.get("requested_market_data_type", "-")))
        selected_value = price_snapshot.get("selected_market_data_type")
        selected = self.DATA_MODE_LABELS.get(selected_value, str(selected_value if selected_value is not None else "not selected"))
        actual_value = price_snapshot.get("subscription_market_data_type")
        actual = self.DATA_MODE_LABELS.get(actual_value, str(actual_value if actual_value is not None else "not reported"))
        status = price_snapshot.get("status") or ("OK" if price is not None else "No usable price")
        error = price_snapshot.get("error") or ""
        if error:
            status = f"{status}: {error}"
        status_text = f"{status} | requested {requested} | selected {selected} | actual {actual}"
        if self.price_status.text() != status_text:
            self.price_status.setText(status_text)
        source_text = f"Source: {price_snapshot.get('source') or '-'} | Updated: {_format_utc_timestamp(price_snapshot.get('timestamp'))}"
        if self.price_source.text() != source_text:
            self.price_source.setText(source_text)
        age = price_snapshot.get("age_seconds")
        next_refresh = price_snapshot.get("next_refresh_seconds")
        age_text = f"{float(age):.1f}s" if isinstance(age, (int, float)) else "-"
        if isinstance(next_refresh, (int, float)) and float(next_refresh) > 0:
            next_text = f"next cached-handle check in {float(next_refresh):.1f}s"
        else:
            next_text = "cached handle checked each worker tick; only actual update events count as fresh"
        refresh_text = f"Snapshot age: {age_text} | {next_text}"
        if self.price_refresh.text() != refresh_text:
            self.price_refresh.setText(refresh_text)
        self._update_api_indicator(price_snapshot)
        self._update_summary_cards(price_snapshot)

        self._update_progress(cycle, price, price_snapshot)
        if self.fields_table.isVisible():
            self._update_field_table(price_snapshot)

    def _update_summary_cards(self, price_snapshot: dict[str, Any]) -> None:
        if not hasattr(self, "price_summary_cards"):
            return
        fields = price_snapshot.get("fields") or {}
        bid = _float_or_none(fields.get("bid") if fields.get("bid") is not None else fields.get("delayedBid"))
        ask = _float_or_none(fields.get("ask") if fields.get("ask") is not None else fields.get("delayedAsk"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            midpoint = (bid + ask) / 2.0
            spread_pct = ((ask - bid) / midpoint) * 100.0 if midpoint > 0 else 0.0
            spread_text = f"{_format_currency(bid)} / {_format_currency(ask)} / {spread_pct:.2f}%"
        else:
            spread_text = "Not available from API"
        actual_value = price_snapshot.get("subscription_market_data_type")
        if actual_value is None:
            actual_value = price_snapshot.get("selected_market_data_type")
        mode = self.DATA_MODE_LABELS.get(actual_value, str(actual_value if actual_value is not None else "not reported"))
        age = price_snapshot.get("api_data_age_seconds")
        if age is None and not bool(price_snapshot.get("market_data_event_tracking")):
            age = price_snapshot.get("age_seconds")
        freshness = self._format_age(age) + " ago" if isinstance(age, (int, float)) else "No actual update yet"
        rth = _format_rth_status(price_snapshot)
        mapping = {
            "Selected price": price_snapshot.get("price"),
            "Source": price_snapshot.get("source"),
            "Freshness": freshness,
            "Data mode": mode,
            "Bid / Ask / Spread": spread_text,
            "RTH status": rth,
            "Current time": _current_time_status_text(),
        }
        for title, value in mapping.items():
            card = self.price_summary_cards.get(title)
            if card is not None:
                card.set_value(value)

    def _format_age(self, value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "-"
        seconds = max(0.0, float(value))
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = seconds / 60.0
        if minutes < 60:
            return f"{minutes:.1f}m"
        return f"{minutes / 60.0:.1f}h"

    def _update_api_indicator(self, price_snapshot: dict[str, Any]) -> None:
        field_count = int(price_snapshot.get("api_non_null_field_count") or 0)
        data_age = price_snapshot.get("api_data_age_seconds")
        change_age = price_snapshot.get("api_data_change_age_seconds")
        update_count = int(price_snapshot.get("api_data_update_count") or price_snapshot.get("api_data_seen_count") or 0)
        change_count = int(price_snapshot.get("api_data_change_count") or 0)
        last_data_at = _format_utc_timestamp(price_snapshot.get("api_last_data_received_at"))
        last_change_at = _format_utc_timestamp(price_snapshot.get("api_last_value_change_at"))
        data_state = str(price_snapshot.get("api_data_state") or "none")
        invalidated = bool(price_snapshot.get("api_data_invalidated"))

        if data_state == "upstream_disconnected":
            dot_object = "ApiIndicatorBad"
            state = "IBKR server link lost; cached quotes invalid"
        elif invalidated or data_state == "invalidated":
            dot_object = "ApiIndicatorWarn"
            state = "Waiting for a fresh streaming update"
        elif data_state == "stale":
            dot_object = "ApiIndicatorWarn"
            state = "API data stale"
        elif data_state in {"receiving", "recent"}:
            dot_object = "ApiIndicatorGood"
            state = "Actual API updates are recent"
        elif data_state == "cached_only":
            dot_object = "ApiIndicatorWarn"
            state = "Cached fields only; no fresh event yet"
        else:
            dot_object = "ApiIndicatorBad"
            state = "API data not received"

        if self.api_indicator_dot.objectName() != dot_object:
            self.api_indicator_dot.setObjectName(dot_object)
            self.api_indicator_dot.style().unpolish(self.api_indicator_dot)
            self.api_indicator_dot.style().polish(self.api_indicator_dot)
        indicator_text = (
            f"{state} | cached non-empty fields {field_count} | "
            f"last actual update {self._format_age(data_age)} ago | "
            f"last value change {self._format_age(change_age)} ago | "
            f"updates {update_count} / value changes {change_count}"
        )
        if self.api_indicator_text.text() != indicator_text:
            self.api_indicator_text.setText(indicator_text)
        invalidation_reason = str(price_snapshot.get("api_data_invalidated_reason") or "")
        tooltip = (
            "Green means an actual ib_async streaming ticker event was received within the freshness window. "
            "Repeated reads of cached non-empty Ticker fields do not refresh the timer, feed ATR, or advance the strategy.\n"
            f"Last actual update: {last_data_at}\n"
            f"Last value change: {last_change_at}\n"
            f"Latest read consumed a new event: {'yes' if price_snapshot.get('market_data_update_consumed') else 'no'}\n"
            f"Invalidation reason: {invalidation_reason or '-'}"
        )
        if self.api_indicator_text.toolTip() != tooltip:
            self.api_indicator_text.setToolTip(tooltip)

    def _update_progress(self, cycle: Optional[dict[str, Any]], price: Any, price_snapshot: Optional[dict[str, Any]] = None) -> None:
        if not cycle or price is None:
            self.progress_label.setText("Strategy progress: waiting for a usable price")
            self.progress_bar.setValue(0)
            return
        try:
            p = float(price)
        except Exception:
            self.progress_label.setText("Strategy progress: price unavailable")
            self.progress_bar.setValue(0)
            return
        stage = cycle.get("stage")
        if stage == Stage.WAIT_INITIAL_DROP.value:
            anchor = cycle.get("anchor_price")
            trigger = cycle.get("drop_trigger_price")
            if anchor is None or trigger is None or float(anchor) <= float(trigger):
                self.progress_label.setText("Stage 1 progress: waiting to set anchor/drop trigger")
                self.progress_bar.setValue(0)
                return
            progress = ((float(anchor) - p) / (float(anchor) - float(trigger))) * 100.0
            self.progress_label.setText(f"Stage 1 drop progress: price {_format_price(p)} vs trigger {_format_price(trigger)}")
            self.progress_bar.setValue(_pct_progress(progress))
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            buy = cycle.get("avg_buy_price")
            target = cycle.get("rise_trigger_price")
            if buy is None or target is None or float(target) <= float(buy):
                self.progress_label.setText("Stage 3 progress: waiting to set minimum-profit trigger")
                self.progress_bar.setValue(0)
                return
            progress = ((p - float(buy)) / (float(target) - float(buy))) * 100.0
            self.progress_label.setText(f"Stage 3 profit progress: price {_format_price(p)} vs trigger {_format_price(target)}")
            self.progress_bar.setValue(_pct_progress(progress))
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            diag = dict((price_snapshot or {}).get("native_order_trigger") or {})
            message = diag.get("message") or "native BUY trailing-stop order is active in TWS"
            self.progress_label.setText(f"Stage 2: {message}")
            self.progress_bar.setValue(50)
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            diag = dict((price_snapshot or {}).get("native_order_trigger") or {})
            message = diag.get("message") or "native SELL trailing-stop order is active in TWS"
            self.progress_label.setText(f"Stage 4: {message}")
            self.progress_bar.setValue(50)
        elif stage == Stage.CYCLE_COMPLETE.value:
            self.progress_label.setText("Stage 5: cycle complete")
            self.progress_bar.setValue(100)
        else:
            self.progress_label.setText("Strategy progress: -")
            self.progress_bar.setValue(0)

    def _update_field_table(self, price_snapshot: dict[str, Any]) -> None:
        fields = price_snapshot.get("fields") or {}
        contract = price_snapshot.get("contract") or {}
        attempts = price_snapshot.get("auto_attempts") or []
        attempts_text = "; ".join(
            f"{item.get('mode')}: {item.get('source') or 'none'}"
            for item in attempts[:4]
            if isinstance(item, dict)
        )
        requested_mode = price_snapshot.get("requested_market_data_type")
        selected_mode = price_snapshot.get("selected_market_data_type")
        actual_mode = price_snapshot.get("subscription_market_data_type")
        requested_label = self.DATA_MODE_LABELS.get(requested_mode, str(requested_mode if requested_mode is not None else "-"))
        selected_label = self.DATA_MODE_LABELS.get(selected_mode, str(selected_mode if selected_mode is not None else "-"))
        actual_label = self.DATA_MODE_LABELS.get(actual_mode, str(actual_mode if actual_mode is not None else "-"))
        native_diag = dict(price_snapshot.get("native_order_trigger") or {})
        rows = [
            ("Current UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
            ("System time", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")),
            ("Selected strategy price", price_snapshot.get("price")),
            ("Selected source", price_snapshot.get("source")),
            ("Native order trigger", native_diag.get("message") if native_diag.get("active") else "-"),
            ("Native trigger method", native_diag.get("trigger_method_label") if native_diag.get("active") else "-"),
            ("Raw trigger value", native_diag.get("raw_last_value") if native_diag.get("active") else "-"),
            ("Raw trigger source", native_diag.get("raw_last_source") if native_diag.get("active") else "-"),
            ("Displayed initial stop", native_diag.get("displayed_initial_stop") if native_diag.get("active") else "-"),
            ("Selected crossed stop", native_diag.get("selected_crossed_displayed_initial_stop") if native_diag.get("active") else "-"),
            ("Raw last crossed stop", native_diag.get("raw_last_crossed_displayed_initial_stop") if native_diag.get("active") else "-"),
            ("Requested mode", requested_label),
            ("Auto selected mode", selected_label),
            ("Actual mode", actual_label),
            ("RTH open", "yes" if price_snapshot.get("rth_open") else "no" if price_snapshot.get("rth_open") is not None else "-"),
            ("RTH status", _format_rth_status(price_snapshot)),
            ("Auto attempts", attempts_text),
            ("Cached API fields present", "yes" if price_snapshot.get("api_data_present") else "no"),
            ("Non-empty API fields", price_snapshot.get("api_non_null_field_count")),
            ("Last actual update age", self._format_age(price_snapshot.get("api_data_age_seconds"))),
            ("Last value-change age", self._format_age(price_snapshot.get("api_data_change_age_seconds"))),
            ("API streaming updates", price_snapshot.get("api_data_update_count")),
            ("API value changes", price_snapshot.get("api_data_change_count")),
            ("Latest read consumed update", "yes" if price_snapshot.get("market_data_update_consumed") else "no"),
            ("Cached fields only", "yes" if price_snapshot.get("cached_fields_only") else "no"),
            ("Event tracking active", "yes" if price_snapshot.get("market_data_event_tracking") else "no"),
            ("Update sequence", price_snapshot.get("market_data_update_sequence")),
            ("Subscription id", price_snapshot.get("market_data_subscription_id")),
            ("Upstream connected", price_snapshot.get("upstream_connected")),
            ("Upstream state", price_snapshot.get("upstream_state")),
            ("Freshness invalidated", "yes" if price_snapshot.get("api_data_invalidated") else "no"),
            ("Freshness invalidation reason", price_snapshot.get("api_data_invalidated_reason")),
            ("Request exchange", price_snapshot.get("request_exchange")),
            ("Request primary exchange", price_snapshot.get("request_primary_exchange")),
            ("Generic ticks", price_snapshot.get("generic_ticks")),
            ("Last", fields.get("last")),
            ("Delayed last", fields.get("delayedLast")),
            ("Bid", fields.get("bid")),
            ("Ask", fields.get("ask")),
            ("Bid/ask midpoint", fields.get("bidAskMidpoint")),
            ("Delayed bid", fields.get("delayedBid")),
            ("Delayed ask", fields.get("delayedAsk")),
            ("Delayed midpoint", fields.get("delayedBidAskMidpoint")),
            ("Market price", fields.get("marketPrice")),
            ("Close", fields.get("close")),
            ("Delayed close", fields.get("delayedClose")),
            ("Mark", fields.get("markPrice")),
            ("Delayed mark", fields.get("delayedMarkPrice")),
            ("conId", contract.get("con_id")),
            ("Contract primary exchange", contract.get("primary_exchange")),
            ("Local symbol", contract.get("local_symbol")),
            ("Trading class", contract.get("trading_class")),
            ("ATR ready", "yes" if price_snapshot.get("atr_ready") else "no"),
            ("ATR value", price_snapshot.get("atr_value")),
            ("ATR %", price_snapshot.get("atr_pct")),
            ("ATR bars", f"{price_snapshot.get('atr_bars_available') or 0}/{price_snapshot.get('atr_bars_required') or '-'}"),
            ("ATR adaptive", "on" if price_snapshot.get("atr_adaptive_enabled") else "off"),
            ("Adaptive drop %", (price_snapshot.get("atr_adaptive_percentages") or {}).get("initial_drop_pct")),
            ("Adaptive BUY rebound/trail %", (price_snapshot.get("atr_adaptive_percentages") or {}).get("buy_rebound_trail_pct")),
            ("Adaptive min profit %", (price_snapshot.get("atr_adaptive_percentages") or {}).get("rise_trigger_pct")),
            ("Adaptive SELL trailing-stop %", (price_snapshot.get("atr_adaptive_percentages") or {}).get("sell_trailing_stop_pct")),
            ("Adaptive protective SELL %", (price_snapshot.get("atr_adaptive_percentages") or {}).get("protective_sell_trailing_stop_pct")),
            ("Error", price_snapshot.get("error")),
        ]
        pairs_per_row = 5
        row_count = (len(rows) + pairs_per_row - 1) // pairs_per_row
        headers = ["Field", "Value"] * pairs_per_row
        self.fields_table.setColumnCount(pairs_per_row * 2)
        self.fields_table.setHorizontalHeaderLabels(headers)
        self.fields_table.setRowCount(row_count)
        for r in range(row_count):
            for pair in range(pairs_per_row):
                item_index = r + pair * row_count
                field_col = pair * 2
                value_col = field_col + 1
                if item_index < len(rows):
                    field, value = rows[item_index]
                    self.fields_table.setItem(r, field_col, QTableWidgetItem(str(field)))
                    self.fields_table.setItem(r, value_col, QTableWidgetItem(_format_field_value(field, value)))
                else:
                    self.fields_table.setItem(r, field_col, QTableWidgetItem(""))
                    self.fields_table.setItem(r, value_col, QTableWidgetItem(""))
        _auto_size_table_columns(self.fields_table, minimum=70, maximum=260, last_maximum=320)
        _fit_table_height_to_rows(self.fields_table, min_rows=4, max_visible_rows=10, min_height=220, max_fit_height=360)


class StopDialog(QDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        show_tws_order_actions: bool = True,
        open_order_count: int = 0,
        show_position_close_action: bool = False,
        unsold_quantity: float = 0.0,
        exit_context: bool = False,
        safe_to_exit: bool = False,
        show_resume_later_exit_action: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("5. Stop strategy")
        self.selected_action: Optional[StopAction] = None
        self.exit_app_after_action = False
        self.exit_context = bool(exit_context)
        self.safe_to_exit = bool(safe_to_exit)
        self.show_position_close_action = bool(show_position_close_action)
        self.show_resume_later_exit_action = bool(show_resume_later_exit_action)
        self.unsold_quantity = max(0.0, float(unsold_quantity or 0.0))
        layout = QVBoxLayout(self)
        intro = QLabel("No strategy is running. Choose Exit app or Cancel." if self.safe_to_exit else "Choose how the bot should handle the current strategy state.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.cancel_btn = QPushButton("Cancel open bot orders")
        self.sell_market_btn = QPushButton("Sell app-bought unsold position with market order")
        self.leave_btn = QPushButton("Leave orders working and recover later")
        self.after_btn = QPushButton("Stop after current cycle / no next cycle")
        self.stop_now_btn = QPushButton("Stop strategy now")
        self.stop_exit_btn = QPushButton("Stop strategy and exit app")
        self.exit_resume_later_btn = QPushButton("Exit app and resume/recover later")
        self.exit_only_btn = QPushButton("Exit app")
        self.close_btn = QPushButton("Cancel" if self.safe_to_exit else "Do not stop")

        self.sell_market_btn.setToolTip(
            "Cancels any app-owned working orders first. If this app has bought shares in the active cycle that have not yet been sold, "
            "the bot submits a SELL market order for that remaining app-owned quantity after existing app SELL orders are no longer working."
        )
        self.stop_now_btn.setToolTip(
            "Stops the local strategy cycle immediately without cancelling or submitting broker orders. Use this when no app-owned open TWS orders are visible."
        )
        self.stop_exit_btn.setToolTip(
            "Stops the local strategy cycle immediately, sends no broker order, then exits the app. Use this when no app-owned open TWS orders are visible."
        )
        self.exit_resume_later_btn.setToolTip(
            "Close the app without sending any stop, cancel, or sell command. The active SQLite cycle and any app-owned TWS orders/position remain for recovery on next start. After restart, click 4. Start strategy to resume monitoring/recovery."
        )
        self.exit_only_btn.setToolTip(
            "Close the app without sending any stop, cancel, or sell command. This is shown only when no strategy cycle is running and no app-owned open TWS orders are visible."
        )

        if self.safe_to_exit and not show_tws_order_actions and not self.show_position_close_action:
            safe_note = QLabel(
                "No strategy cycle is running and no app-owned open TWS orders are visible. "
                "You can close the app without sending any broker or local stop command."
            )
            safe_note.setObjectName("Muted")
            safe_note.setWordWrap(True)
            layout.addWidget(safe_note)
            layout.addWidget(self.exit_only_btn)
        elif show_tws_order_actions:
            if open_order_count > 0:
                count_label = QLabel(f"TWS currently reports {open_order_count} app-owned open order(s).")
                count_label.setObjectName("Muted")
                count_label.setWordWrap(True)
                layout.addWidget(count_label)
            layout.addWidget(self.cancel_btn)
            if self.show_position_close_action:
                layout.addWidget(self.sell_market_btn)
            layout.addWidget(self.leave_btn)
            if self.show_resume_later_exit_action:
                resume_note = QLabel(
                    "Exit app and resume/recover later closes only the GUI/worker. It does not cancel TWS orders, does not sell shares, and does not mark the strategy stopped. Use it only when you intentionally want to continue recovery/monitoring on next app start."
                )
                resume_note.setObjectName("Muted")
                resume_note.setWordWrap(True)
                layout.addWidget(resume_note)
                layout.addWidget(self.exit_resume_later_btn)
            layout.addWidget(self.after_btn)
        elif self.show_position_close_action:
            position_note = QLabel(
                f"No app-owned open TWS orders are visible, but SQLite shows {self.unsold_quantity:g} app-bought unsold share(s) for the active cycle. "
                "To exit the strategy and flatten the app-owned position, use the market SELL option. Refresh from IBKR/TWS in Reconciliation if this does not match TWS."
            )
            position_note.setObjectName("Muted")
            position_note.setWordWrap(True)
            layout.addWidget(position_note)
            layout.addWidget(self.sell_market_btn)
            if self.show_resume_later_exit_action:
                resume_note = QLabel(
                    "Exit app and resume/recover later leaves the app-owned position recorded in SQLite and sends no broker command. On next start, reconnect and click 4. Start strategy to resume monitoring/recovery."
                )
                resume_note.setObjectName("Muted")
                resume_note.setWordWrap(True)
                layout.addWidget(resume_note)
                layout.addWidget(self.exit_resume_later_btn)
            layout.addWidget(self.after_btn)
        else:
            hidden_note = QLabel(
                "No app-owned open orders are currently visible in TWS. You can stop the local strategy now; "
                "no broker order will be cancelled or submitted. Refresh from IBKR/TWS in Reconciliation if you expected working bot orders."
            )
            hidden_note.setObjectName("Muted")
            hidden_note.setWordWrap(True)
            layout.addWidget(hidden_note)
            if self.show_resume_later_exit_action:
                resume_note = QLabel(
                    "Exit app and resume/recover later closes the app without changing the active cycle. On next start, reconnect and click 4. Start strategy to resume monitoring/recovery."
                )
                resume_note.setObjectName("Muted")
                resume_note.setWordWrap(True)
                layout.addWidget(resume_note)
                layout.addWidget(self.exit_resume_later_btn)
            if self.exit_context:
                layout.addWidget(self.stop_exit_btn)
            else:
                layout.addWidget(self.stop_now_btn)
                layout.addWidget(self.stop_exit_btn)

        layout.addWidget(self.close_btn)

        self.cancel_btn.clicked.connect(lambda: self._choose(StopAction.CANCEL_OPEN_BOT_ORDERS))
        self.sell_market_btn.clicked.connect(lambda: self._choose(StopAction.SELL_APP_POSITION_MARKET))
        self.leave_btn.clicked.connect(lambda: self._choose(StopAction.LEAVE_ORDERS_WORKING))
        self.after_btn.clicked.connect(lambda: self._choose(StopAction.STOP_AFTER_CURRENT_CYCLE))
        self.stop_now_btn.clicked.connect(lambda: self._choose(StopAction.STOP_NOW_NO_BROKER_ACTION))
        self.stop_exit_btn.clicked.connect(lambda: self._choose(StopAction.STOP_NOW_NO_BROKER_ACTION, exit_app=True))
        self.exit_resume_later_btn.clicked.connect(self._choose_exit_only)
        self.exit_only_btn.clicked.connect(self._choose_exit_only)
        self.close_btn.clicked.connect(self.reject)

    def _choose(self, action: StopAction, *, exit_app: bool = False) -> None:
        self.selected_action = action
        self.exit_app_after_action = bool(exit_app)
        self.accept()

    def _choose_exit_only(self) -> None:
        self.selected_action = None
        self.exit_app_after_action = True
        self.accept()


class CycleAuditDialog(QDialog):
    """Read-only click-through view for one completed cycle."""

    def __init__(self, row: dict[str, Any], details: dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(920, 640)
        details = self._enriched_details(row, details)
        ticker = row.get("ticker") or "-"
        cycle_number = row.get("cycle_number") or "-"
        self.setWindowTitle(f"Cycle audit log - {ticker} cycle {cycle_number}")
        self.resize(1220, 820)
        layout = QVBoxLayout(self)
        title = QLabel(f"{ticker} cycle {cycle_number} - orders, executions, verbose log and audit events")
        title.setObjectName("StatusLabel")
        layout.addWidget(title)

        tabs = QTabWidget()
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        tabs.addTab(self._summary_tab(row, details), "Summary")
        tabs.addTab(self._timeline_tab(row, details), "Timeline")
        tabs.addTab(self._records_table(details.get("orders") or [], [
            ("created_at", "Created"),
            ("action", "Action"),
            ("order_type", "Type"),
            ("quantity", "Qty"),
            ("trailing_percent", "Trailing %"),
            ("initial_stop_price", "Initial stop"),
            ("status", "Status"),
            ("order_id", "Order ID"),
            ("perm_id", "permId"),
            ("order_ref", "OrderRef"),
        ], "No order rows found for this cycle."), "Orders")
        tabs.addTab(self._records_table(details.get("executions") or [], [
            ("executed_at", "Executed"),
            ("side", "Side"),
            ("shares", "Shares"),
            ("price", "Price"),
            ("avg_price", "Average"),
            ("commission", "Commission"),
            ("execution_id", "Execution ID"),
            ("order_ref", "OrderRef"),
        ], "No execution rows found for this cycle."), "Executions")
        tabs.addTab(self._market_capture_tab(row, details), "Market capture")
        tabs.addTab(self._records_table(details.get("decision_events") or [], [
            ("created_at", "Created"),
            ("event_type", "Event"),
            ("stage_before", "Before"),
            ("stage_after", "After"),
            ("decision_result", "Result"),
            ("broker_order_id", "Order ID"),
            ("perm_id", "permId"),
            ("message", "Message"),
        ], "No structured decision events found for this cycle."), "Decision events")
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QTextEdit.NoWrap)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.text.setMinimumHeight(520)
        self.text.setPlainText(self._format(row, details))
        tabs.addTab(self.text, "Raw log")
        layout.addWidget(tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @classmethod
    def _enriched_details(cls, row: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(details or {})
        enriched.setdefault("cycle", row)
        rows, files = cls._load_market_capture_rows(row, enriched)
        enriched["market_capture_rows"] = rows
        enriched["market_capture_files"] = files
        return enriched

    @staticmethod
    def _capture_ids_from_decisions(details: dict[str, Any]) -> set[str]:
        capture_ids: set[str] = set()
        for event in details.get("decision_events") or []:
            raw = _parse_jsonish(event.get("raw_json") or event.get("raw"))
            if isinstance(raw, dict):
                capture_id = str(raw.get("capture_event_id") or raw.get("event_id") or "").strip()
                if capture_id:
                    capture_ids.add(capture_id)
        return capture_ids

    @staticmethod
    def _capture_path_matches_expected(path: Path, *, ticker: str, cycle_number: str, cycle_id: str, capture_ids: set[str]) -> bool:
        haystack = " ".join(str(part) for part in path.parts[-6:]).upper()
        if capture_ids and any(capture_id.upper() in haystack for capture_id in capture_ids):
            return True
        if ticker and len(ticker) > 1 and re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])", haystack):
            return True
        if cycle_id and cycle_id.upper() in haystack:
            return True
        if cycle_number:
            tokens = {f"CYCLE_{cycle_number}", f"CYCLE-{cycle_number}", f"CYCLE{cycle_number}"}
            if any(token.upper() in haystack for token in tokens):
                return True
        return False

    @staticmethod
    def _capture_row_has_identity(row: dict[str, Any]) -> bool:
        keys = ("ticker", "symbol", "cycle_id", "cycle_number", "order_ref", "orderRef")
        return any(str(row.get(key) or "").strip() for key in keys)

    @staticmethod
    def _capture_row_matches_expected(row: dict[str, Any], *, ticker: str, cycle_number: str, cycle_id: str) -> bool:
        row_ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        row_cycle_id = str(row.get("cycle_id") or "").strip()
        row_cycle_number = str(row.get("cycle_number") or "").strip()
        order_ref = str(row.get("order_ref") or row.get("orderRef") or "").strip().upper()
        if ticker and row_ticker and row_ticker != ticker:
            return False
        if cycle_id and row_cycle_id and row_cycle_id != cycle_id:
            return False
        if cycle_number and row_cycle_number and row_cycle_number != cycle_number:
            return False
        if ticker and order_ref and f"|{ticker}|" not in order_ref and not order_ref.startswith(f"IBKRBOT|{ticker}|"):
            return False
        if cycle_number and order_ref:
            cycle_tokens = (f"CYCLE-{cycle_number}", f"CYCLE_{cycle_number}", f"CYCLE{cycle_number}")
            if "CYCLE" in order_ref and not any(token in order_ref for token in cycle_tokens):
                return False
        return True

    @classmethod
    def _candidate_capture_files(cls, row: dict[str, Any], details: dict[str, Any]) -> list[Path]:
        cycle = details.get("cycle") or row or {}
        ticker = str(row.get("ticker") or cycle.get("ticker") or "").strip().upper()
        cycle_number = row.get("cycle_number") or cycle.get("cycle_number")
        capture_ids = cls._capture_ids_from_decisions(details)
        candidates: list[Path] = []
        try:
            base = debug_captures_dir()
        except Exception:
            return []
        search_dirs: list[Path] = []
        if ticker and cycle_number not in (None, ""):
            search_dirs.append(base / ticker / f"cycle_{cycle_number}")
        if ticker:
            search_dirs.append(base / ticker)
        search_dirs.append(base)
        seen: set[Path] = set()
        for directory in search_dirs:
            try:
                paths = list(directory.glob("**/*.zip")) if directory.exists() else []
            except Exception:
                paths = []
            for path in paths:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                # The exact ticker/cycle folder is trusted. Broader scans are
                # restricted by capture id, ticker, cycle id, or cycle-number
                # tokens so copied historical debug_captures for other cycles do not
                # distort the audit timeline scale.
                exact_cycle_folder = bool(ticker and cycle_number not in (None, "") and path.parent.name == f"cycle_{cycle_number}" and path.parent.parent.name.upper() == ticker)
                path_matches = cls._capture_path_matches_expected(
                    path,
                    ticker=ticker,
                    cycle_number=str(cycle_number or "").strip(),
                    cycle_id=str(cycle.get("id") or row.get("id") or "").strip(),
                    capture_ids=capture_ids,
                )
                if not exact_cycle_folder and not path_matches:
                    continue
                candidates.append(path)
        return candidates

    @staticmethod
    def _identifier_equal(expected: Any, actual: Any) -> bool:
        expected_text = str(expected or "").strip()
        actual_text = str(actual or "").strip()
        if not expected_text or not actual_text:
            return True
        if expected_text == actual_text:
            return True
        try:
            return int(float(expected_text)) == int(float(actual_text))
        except Exception:
            return False

    @staticmethod
    def _capture_exact_cycle_folder(path: Path, ticker: str, cycle_number: Any) -> bool:
        if not ticker or cycle_number in (None, ""):
            return False
        try:
            parts = [part.upper() for part in path.parts]
            raw_cycle = str(cycle_number).strip()
            tokens = {f"CYCLE_{raw_cycle}", f"CYCLE-{raw_cycle}", f"CYCLE{raw_cycle}"}
            try:
                normalized_cycle = str(int(float(raw_cycle)))
                tokens.update({f"CYCLE_{normalized_cycle}", f"CYCLE-{normalized_cycle}", f"CYCLE{normalized_cycle}"})
            except Exception:
                pass
            return ticker.upper() in parts and any(token.upper() in parts for token in tokens)
        except Exception:
            return False

    @classmethod
    def _capture_manifest_or_path_matches_cycle(cls, path: Path, manifest: dict[str, Any], row: dict[str, Any], details: dict[str, Any]) -> bool:
        cycle = details.get("cycle") or row or {}
        expected_cycle_id = str(row.get("id") or cycle.get("id") or "").strip()
        expected_cycle_number = row.get("cycle_number") or cycle.get("cycle_number")
        expected_ticker = str(row.get("ticker") or cycle.get("ticker") or "").strip().upper()
        manifest_cycle_id = str(manifest.get("cycle_id") or "").strip()
        manifest_cycle_number = manifest.get("cycle_number")
        manifest_ticker = str(manifest.get("ticker") or "").strip().upper()
        has_manifest_identity = any([manifest_cycle_id, str(manifest_cycle_number or "").strip(), manifest_ticker])
        if has_manifest_identity:
            if expected_ticker and manifest_ticker and manifest_ticker != expected_ticker:
                return False
            if expected_cycle_id and manifest_cycle_id and manifest_cycle_id != expected_cycle_id:
                return False
            return not (
                expected_cycle_number not in (None, "")
                and manifest_cycle_number not in (None, "")
                and not cls._identifier_equal(expected_cycle_number, manifest_cycle_number)
            )
        # Imported capture ZIPs from before manifest support may lack one. Trust only an
        # exact ticker/cycle folder or a decision-event capture id in the file
        # name; otherwise do not load broad ticker/base-folder captures because
        # they can flatten the per-cycle timeline scale.
        capture_ids = cls._capture_ids_from_decisions(details)
        if capture_ids and any(capture_id in path.name for capture_id in capture_ids):
            return True
        return cls._capture_exact_cycle_folder(path, expected_ticker, expected_cycle_number)

    @classmethod
    def _market_capture_row_matches_cycle(cls, capture_row: dict[str, Any], row: dict[str, Any], details: dict[str, Any]) -> bool:
        cycle = details.get("cycle") or row or {}
        expected_cycle_id = str(row.get("id") or cycle.get("id") or "").strip()
        expected_cycle_number = row.get("cycle_number") or cycle.get("cycle_number")
        expected_ticker = str(row.get("ticker") or cycle.get("ticker") or "").strip().upper()
        fields = capture_row.get("fields") if isinstance(capture_row.get("fields"), dict) else {}
        row_ticker = str(capture_row.get("ticker") or capture_row.get("symbol") or fields.get("ticker") or "").strip().upper()
        row_cycle_id = capture_row.get("cycle_id") or fields.get("cycle_id")
        row_cycle_number = capture_row.get("cycle_number") or fields.get("cycle_number")
        order_ref = str(capture_row.get("order_ref") or capture_row.get("orderRef") or fields.get("order_ref") or fields.get("orderRef") or "").strip().upper()
        if expected_ticker and row_ticker and row_ticker != expected_ticker:
            return False
        if expected_cycle_id and row_cycle_id and str(row_cycle_id).strip() != expected_cycle_id:
            return False
        if expected_cycle_number not in (None, "") and row_cycle_number not in (None, "") and not cls._identifier_equal(expected_cycle_number, row_cycle_number):
            return False
        if expected_ticker and order_ref and f"|{expected_ticker}|" not in order_ref and not order_ref.startswith(f"IBKRBOT|{expected_ticker}|"):
            return False
        if expected_cycle_number not in (None, "") and order_ref:
            raw_cycle = str(expected_cycle_number).strip()
            cycle_tokens = {f"CYCLE-{raw_cycle}", f"CYCLE_{raw_cycle}", f"CYCLE{raw_cycle}"}
            try:
                normalized = str(int(float(raw_cycle)))
                cycle_tokens.update({f"CYCLE-{normalized}", f"CYCLE_{normalized}", f"CYCLE{normalized}"})
            except Exception:
                pass
            if "CYCLE" in order_ref and not any(token in order_ref for token in cycle_tokens):
                return False

        row_time = _timeline_time(capture_row, "captured_at_utc", "event_time_utc", "timestamp", "time")
        cycle_window = cls._cycle_capture_time_window(row, details)
        if row_time is not None and cycle_window is not None:
            start, end = cycle_window
            return start <= row_time <= end
        # Loose imported captures without cycle identity or time are unsafe to
        # use because they can mix multiple cycles and flatten the Y-axis scale.
        return bool(row_cycle_id or row_cycle_number or order_ref)

    @classmethod
    def _cycle_capture_time_window(cls, row: dict[str, Any], details: dict[str, Any]) -> tuple[float, float] | None:
        cycle = details.get("cycle") or row or {}
        values: list[Any] = []
        for key in (
            "created_at",
            "buy_filled_at",
            "protective_sell_filled_at",
            "sell_filled_at",
            "updated_at",
        ):
            values.append(cycle.get(key) or row.get(key))
        for collection_name in ("orders", "executions", "decision_events", "events"):
            for item in details.get(collection_name) or []:
                if not isinstance(item, dict):
                    continue
                for key in ("created_at", "updated_at", "executed_at"):
                    values.append(item.get(key))
        return time_window_from_values(values, pad_seconds=60 * 60)

    @classmethod
    def _capture_file_is_exact_cycle_match(cls, path: Path, manifest: dict[str, Any], row: dict[str, Any], details: dict[str, Any]) -> bool:
        cycle = details.get("cycle") or row or {}
        expected_cycle_id = str(row.get("id") or cycle.get("id") or "").strip()
        expected_cycle_number = row.get("cycle_number") or cycle.get("cycle_number")
        expected_ticker = str(row.get("ticker") or cycle.get("ticker") or "").strip().upper()
        manifest_cycle_id = str(manifest.get("cycle_id") or "").strip()
        manifest_cycle_number = manifest.get("cycle_number")
        manifest_ticker = str(manifest.get("ticker") or "").strip().upper()
        if expected_cycle_id and manifest_cycle_id and manifest_cycle_id == expected_cycle_id:
            return True
        if expected_ticker and manifest_ticker and manifest_ticker != expected_ticker:
            return False
        if expected_cycle_number not in (None, "") and manifest_cycle_number not in (None, "") and cls._identifier_equal(expected_cycle_number, manifest_cycle_number):
            return True
        capture_ids = cls._capture_ids_from_decisions(details)
        if capture_ids and any(capture_id in path.name for capture_id in capture_ids):
            return True
        return cls._capture_exact_cycle_folder(path, expected_ticker, expected_cycle_number)

    @classmethod
    def _load_market_capture_rows(cls, row: dict[str, Any], details: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        cycle = details.get("cycle") or row or {}
        expected_cycle_id = str(row.get("id") or cycle.get("id") or "").strip()
        expected_cycle_number = str(row.get("cycle_number") or cycle.get("cycle_number") or "").strip()
        expected_ticker = str(row.get("ticker") or cycle.get("ticker") or "").strip().upper()
        rows: list[dict[str, Any]] = []
        files: list[str] = []
        for path in cls._candidate_capture_files(row, details):
            try:
                with zipfile.ZipFile(path, "r") as zf:
                    manifest: dict[str, Any] = {}
                    try:
                        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                    except Exception:
                        manifest = {}
                    if not cls._capture_manifest_or_path_matches_cycle(path, manifest, row, details):
                        continue
                    file_rows: list[dict[str, Any]] = []
                    if "market_data.jsonl" in zf.namelist():
                        for line in zf.read("market_data.jsonl").decode("utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                loaded = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(loaded, dict):
                                file_rows.append(loaded)
                    elif "market_data.csv" in zf.namelist():
                        text = zf.read("market_data.csv").decode("utf-8", errors="replace")
                        file_rows.extend(dict(item) for item in csv.DictReader(text.splitlines()))
                    if not file_rows:
                        continue
                    exact_file_match = cls._capture_file_is_exact_cycle_match(path, manifest, row, details)
                    if exact_file_match:
                        matched_rows = [
                            item for item in file_rows
                            if isinstance(item, dict)
                            and cls._capture_row_matches_expected(
                                item,
                                ticker=expected_ticker,
                                cycle_number=expected_cycle_number,
                                cycle_id=expected_cycle_id,
                            )
                        ]
                    else:
                        matched_rows = [
                            item for item in file_rows
                            if isinstance(item, dict) and cls._market_capture_row_matches_cycle(item, row, details)
                        ]
                    if not matched_rows:
                        continue
                    for item in matched_rows:
                        item.setdefault("capture_file", str(path))
                    rows.extend(matched_rows)
                    files.append(str(path))
            except Exception:
                continue
        rows.sort(key=lambda item: (_timeline_time(item, "captured_at_utc", "event_time_utc") is None, _timeline_time(item, "captured_at_utc", "event_time_utc") or 0.0, str(item.get("monotonic_ts") or "")))
        deduped: list[dict[str, Any]] = []
        seen_rows: set[tuple[Any, Any, Any]] = set()
        for item in rows:
            key = (item.get("captured_at_utc"), item.get("monotonic_ts"), item.get("price"))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            deduped.append(item)
        return deduped, files

    @classmethod
    def _outcome_badge(cls, row: dict[str, Any], details: dict[str, Any]) -> str:
        stage = str(row.get("stage") or "")
        net = _float_or_none(row.get("net_pnl"))
        gross = _float_or_none(row.get("gross_pnl"))
        sell_qty = _float_or_none(row.get("sell_filled_qty")) or 0.0
        protective_qty = _float_or_none(row.get("protective_sell_filled_qty")) or 0.0
        has_completed_exit = stage == Stage.CYCLE_COMPLETE.value or sell_qty > 0 or protective_qty > 0 or net is not None or gross is not None

        # Imported databases can contain a transient error_message on a cycle
        # that later completed successfully. Outcome badges should describe the
        # final trade result, not an obsolete transient warning. Active/manual
        # error states still take precedence.
        if stage in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            return "ERROR STOP"
        if row.get("manual_stop") and not has_completed_exit:
            return "MANUAL"
        if (row.get("cancelled") or stage == Stage.STOPPED.value) and not has_completed_exit:
            return "CANCELLED"

        sell_ref = str(row.get("sell_order_ref") or "")
        protective_ref = str(row.get("protective_sell_order_ref") or "")
        if protective_qty > 0 or "PROTECT" in sell_ref.upper() or "PROTECT" in protective_ref.upper() or row.get("protective_exit"):
            return "PROTECTIVE EXIT"
        if (net is not None and net >= 0) or (net is None and gross is not None and gross >= 0):
            return "PROFIT EXIT"
        if net is not None or gross is not None:
            return "LOSS EXIT"
        if row.get("error_message"):
            return "ERROR STOP"
        return "COMPLETED"

    @classmethod
    def _timeline_tab(cls, row: dict[str, Any], details: dict[str, Any]) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        timeline = CycleTimelineWidget(row, details, compact=False)
        timeline.setMinimumHeight(500)
        timeline.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        controls = QHBoxLayout()
        hint = QLabel("Timeline graph: all timed rows share one horizontal timescale. Hover for crosshairs; Ctrl+mouse wheel zooms; drag while zoomed or use the scroll bars to pan.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        zoom_out_btn = QPushButton("Zoom out")
        zoom_in_btn = QPushButton("Zoom in")
        reset_btn = QPushButton("Reset zoom")
        zoom_out_btn.clicked.connect(lambda: timeline.set_zoom(timeline.zoom_factor() / 1.25))
        zoom_in_btn.clicked.connect(lambda: timeline.set_zoom(timeline.zoom_factor() * 1.25))
        reset_btn.clicked.connect(timeline.reset_zoom)
        controls.addWidget(hint, 1)
        controls.addWidget(zoom_out_btn)
        controls.addWidget(zoom_in_btn)
        controls.addWidget(reset_btn)
        layout.addLayout(controls)
        timeline_scroll = QScrollArea()
        timeline_scroll.setWidgetResizable(True)
        timeline_scroll.setFrameShape(QFrame.NoFrame)
        timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        timeline_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        timeline_scroll.setWidget(timeline)
        timeline_scroll.setMinimumHeight(500)
        timeline_scroll.setMaximumHeight(16777215)
        layout.addWidget(timeline_scroll, 1)
        transition_rows = []
        for event in details.get("decision_events") or []:
            before = event.get("stage_before") or ""
            after = event.get("stage_after") or ""
            if not before and not after:
                continue
            transition_rows.append({
                "created_at": event.get("created_at"),
                "stage_before": _stage_display_name(before),
                "stage_after": _stage_display_name(after),
                "event_type": event.get("event_type"),
                "decision_result": event.get("decision_result"),
                "message": event.get("message"),
            })
        risk_rows = []
        for event in list(details.get("decision_events") or []) + list(details.get("events") or []):
            if _is_audit_risk_block_event(event):
                risk_rows.append({
                    "created_at": event.get("created_at"),
                    "event_type": event.get("event_type") or event.get("level"),
                    "decision_result": event.get("decision_result"),
                    "message": event.get("message"),
                })
        info = QLabel(
            f"Timeline sources: {len(details.get('market_capture_rows') or []):,} market-capture rows; "
            f"{len(transition_rows)} stage transition(s); {len(risk_rows)} risk/guard block(s)."
        )
        info.setObjectName("Muted")
        info.setWordWrap(True)
        layout.addWidget(info)
        split = QHBoxLayout()
        split.addWidget(cls._records_table(transition_rows, [
            ("created_at", "Time"),
            ("stage_before", "Before"),
            ("stage_after", "After"),
            ("event_type", "Event"),
            ("decision_result", "Result"),
            ("message", "Message"),
        ], "No stage transitions were recorded for this cycle.", max_visible_rows=4, expand_when_overflow=False), 1)
        split.addWidget(cls._records_table(risk_rows, [
            ("created_at", "Time"),
            ("event_type", "Guard / risk event"),
            ("decision_result", "Result"),
            ("message", "Message"),
        ], "No guard/risk blocks were recorded for this cycle.", max_visible_rows=4, expand_when_overflow=False), 1)
        layout.addLayout(split, 0)
        return tab

    @staticmethod
    def _scrollable_tab(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll


    @classmethod
    def _summary_tab(cls, row: dict[str, Any], details: dict[str, Any]) -> QWidget:
        cycle = details.get("cycle") or row
        tab = QWidget()
        layout = QVBoxLayout(tab)
        compact_timeline = CycleTimelineWidget(row, details, compact=True)
        compact_timeline.setMinimumHeight(500)
        compact_timeline.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(compact_timeline, 1)
        summary_items = [
            ("Outcome", cls._outcome_badge(row, details)),
            ("Entry condition", f"Initial drop {row.get('configured_initial_drop_pct') or cycle.get('initial_drop_pct') or '-'}%; BUY rebound {row.get('configured_buy_rebound_pct') or cycle.get('buy_rebound_trail_pct') or '-'}%"),
            ("Buy order type", row.get("buy_order_type") or cycle.get("buy_order_type") or "TRAIL or MKT from stored order row"),
            ("Buy fill", f"{cycle.get('buy_filled_qty') or row.get('buy_filled_qty') or '-'} @ {cls._money(cycle.get('avg_buy_price') or row.get('avg_buy_price'))}"),
            ("Protective sell status", cycle.get("protective_sell_status") or row.get("protective_sell_enabled_display") or "Not applicable in this stage"),
            ("Exit condition", f"Minimum profit {row.get('configured_min_profit_pct') or cycle.get('rise_trigger_pct') or '-'}%; SELL trailing-stop {row.get('configured_sell_trail_pct') or cycle.get('sell_trailing_stop_pct') or '-'}%"),
            ("Sell fill", f"{cycle.get('sell_filled_qty') or row.get('sell_filled_qty') or '-'} @ {cls._money(cycle.get('avg_sell_price') or row.get('avg_sell_price'))}"),
            ("Gross / net P&L", f"{cls._money(row.get('gross_pnl') or cycle.get('gross_pnl'))} / {cls._money(row.get('net_pnl') or cycle.get('net_pnl'))}"),
            ("Duration", row.get("holding_minutes_display") or row.get("duration") or "Not available from audit rows"),
            ("Slippage", row.get("configured_slippage_buffer_pct") or cycle.get("slippage_buffer_pct") or "Not available from audit rows"),
            ("Market-data mode", row.get("market_data_mode") or cycle.get("market_data_mode") or "Not available from audit rows"),
        ]
        summary_table = cls._multi_pair_key_value_table(summary_items, pairs_per_row=3)
        layout.addWidget(summary_table, 0)
        return tab

    @staticmethod
    def _multi_pair_key_value_table(
        items: list[tuple[str, Any]],
        *,
        pairs_per_row: int = 3,
    ) -> QTableWidget:
        """Lay out summary fields across the full width without scrollbars."""
        pair_count = max(1, int(pairs_per_row))
        row_count = max(1, (len(items) + pair_count - 1) // pair_count)
        table = QTableWidget(row_count, pair_count * 2)
        table.setHorizontalHeaderLabels([label for _ in range(pair_count) for label in ("Field", "Value")])
        _polish_table_widget(
            table,
            stretch_last=False,
            horizontal_scroll=Qt.ScrollBarAlwaysOff,
            vertical_scroll=Qt.ScrollBarAlwaysOff,
            expanding=False,
        )
        for item_idx, (key, value) in enumerate(items):
            row_idx = item_idx // pair_count
            pair_idx = item_idx % pair_count
            key_item = QTableWidgetItem(str(key))
            value_item = QTableWidgetItem(_format_field_value(key, value))
            value_item.setToolTip(str(value_item.text()))
            table.setItem(row_idx, pair_idx * 2, key_item)
            table.setItem(row_idx, pair_idx * 2 + 1, value_item)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        for pair_idx in range(pair_count):
            header.setSectionResizeMode(pair_idx * 2, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(pair_idx * 2 + 1, QHeaderView.Stretch)
        _fit_table_height_to_all_rows(table, min_height=130, max_height=230)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return table

    @staticmethod
    def _key_value_table(items: list[tuple[str, Any]]) -> QTableWidget:
        table = QTableWidget(len(items), 2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        _polish_table_widget(table, stretch_last=False, expanding=False)
        for row_idx, (key, value) in enumerate(items):
            key_item = QTableWidgetItem(str(key))
            value_item = QTableWidgetItem(_format_field_value(key, value))
            table.setItem(row_idx, 0, key_item)
            table.setItem(row_idx, 1, value_item)
        _resize_table_columns_for_available_width(table)
        _fit_table_height_to_rows(table, min_rows=min(3, len(items)), max_visible_rows=14, min_height=110, max_fit_height=360)
        return table

    @classmethod
    def _market_capture_tab(cls, row: dict[str, Any], details: dict[str, Any]) -> QWidget:
        cycle = details.get("cycle") or row
        tab = QWidget()
        layout = QVBoxLayout(tab)
        capture_rows = details.get("market_capture_rows") or []
        capture_files = details.get("market_capture_files") or []
        first_capture = capture_rows[0].get("captured_at_utc") if capture_rows else None
        last_capture = capture_rows[-1].get("captured_at_utc") if capture_rows else None
        items = [
            ("Anchor price", cycle.get("anchor_price") or row.get("anchor_price")),
            ("Drop trigger", cycle.get("drop_trigger_price") or row.get("drop_trigger_price")),
            ("BUY initial stop", cycle.get("buy_initial_trail_stop_price") or row.get("buy_initial_trail_stop_price")),
            ("Average buy", cycle.get("avg_buy_price") or row.get("avg_buy_price")),
            ("Minimum-profit trigger", cycle.get("rise_trigger_price") or row.get("rise_trigger_price")),
            ("SELL initial stop", cycle.get("sell_initial_trail_stop_price") or row.get("sell_initial_trail_stop_price")),
            ("Average sell", cycle.get("avg_sell_price") or row.get("avg_sell_price")),
            ("Buy vs anchor %", row.get("buy_vs_anchor_pct")),
            ("Sell vs buy %", row.get("sell_vs_buy_pct")),
            ("Slippage buffer", row.get("slippage_buffer_enabled_display") or cycle.get("slippage_buffer_enabled")),
            ("Timeline capture ZIP files", len(capture_files)),
            ("Timeline capture rows", len(capture_rows)),
            ("First captured row", first_capture or "No completed capture file found"),
            ("Last captured row", last_capture or "No completed capture file found"),
        ]
        summary_table = cls._key_value_table(items)
        # This metadata table is short and important; show every field without
        # a vertical scrollbar so the operator can scan the capture context at
        # once. The preview/file widgets below absorb remaining vertical space.
        _fit_table_height_to_all_rows(summary_table, min_height=420, max_height=760)
        layout.addWidget(summary_table, 0)
        if capture_rows:
            preview_rows: list[dict[str, Any]] = []
            head = list(capture_rows[:8])
            tail = list(capture_rows[-8:]) if len(capture_rows) > 8 else []
            for item in head + [row for row in tail if row not in head]:
                fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                preview_rows.append({
                    "captured_at_utc": item.get("captured_at_utc") or item.get("event_time_utc") or item.get("timestamp") or item.get("time"),
                    "price": item.get("price") or item.get("selected_price") or item.get("marketPrice") or fields.get("marketPrice") or fields.get("last"),
                    "source": item.get("source") or item.get("price_source") or item.get("data_mode"),
                    "capture_file": Path(str(item.get("capture_file") or "")).name,
                })
            preview_label = QLabel("Market-capture preview: first and last captured rows used by the audit timeline.")
            preview_label.setObjectName("Muted")
            preview_label.setWordWrap(True)
            layout.addWidget(preview_label)
            preview_table = cls._records_table(preview_rows, [
                ("captured_at_utc", "Captured"),
                ("price", "Selected price"),
                ("source", "Source"),
                ("capture_file", "Capture ZIP"),
            ], "No captured rows found for this cycle.")
            preview_table.setMinimumHeight(240)
            preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(preview_table, 2)
        if capture_files:
            file_box = QTextEdit()
            file_box.setReadOnly(True)
            file_box.setMinimumHeight(110)
            file_box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            file_box.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            file_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            file_box.setPlainText("\n".join(str(path) for path in capture_files))
            layout.addWidget(file_box, 1)
        return cls._scrollable_tab(tab)

    @staticmethod
    def _records_table(
        records: list[dict[str, Any]],
        columns: list[tuple[str, str]],
        empty_message: str,
        *,
        max_visible_rows: int = 12,
        expand_when_overflow: bool = True,
    ) -> QTableWidget:
        if not records:
            records = [{columns[0][0]: empty_message}]
        table = QTableWidget(len(records), len(columns))
        table.setHorizontalHeaderLabels([label for _key, label in columns])
        _polish_table_widget(table, stretch_last=True, horizontal_scroll=Qt.ScrollBarAsNeeded, vertical_scroll=Qt.ScrollBarAlwaysOn, expanding=True)
        for row_idx, record in enumerate(records):
            for col_idx, (key, _label) in enumerate(columns):
                item = QTableWidgetItem(_format_field_value(key, record.get(key)))
                item.setToolTip(str(record.get("raw_json") or ""))
                table.setItem(row_idx, col_idx, item)
        _resize_table_columns_for_available_width(table)
        _fit_table_height_to_rows(
            table,
            min_rows=min(4, len(records)),
            max_visible_rows=max_visible_rows,
            min_height=150,
            max_fit_height=360,
            expand_when_overflow=expand_when_overflow,
        )
        return table

    @staticmethod
    def _money(value: Any) -> str:
        return _format_currency(value, 4)

    @classmethod
    def _format(cls, row: dict[str, Any], details: dict[str, Any]) -> str:
        if row.get("__example"):
            return cls._example_text(row)
        cycle = details.get("cycle") or row
        lines: list[str] = []
        lines.append("CYCLE SUMMARY")
        lines.append("=" * 80)
        for key in [
            "id", "ticker", "cycle_number", "stage", "created_at", "updated_at",
            "investment_amount", "budget", "buy_filled_qty", "avg_buy_price",
            "sell_filled_qty", "avg_sell_price", "gross_pnl", "net_pnl",
            "protective_sell_enabled", "slippage_buffer_enabled", "hard_risk_limits_enabled",
        ]:
            if key in cycle:
                value = cycle.get(key)
                if key in {"investment_amount", "budget", "avg_buy_price", "avg_sell_price", "gross_pnl", "net_pnl"}:
                    value = cls._money(value)
                lines.append(f"{key}: {value}")

        lines.append("")
        lines.append("ORDERS")
        lines.append("=" * 80)
        for order in details.get("orders") or []:
            lines.append(
                f"{_format_utc_timestamp(order.get('created_at'))} | {order.get('action')} {order.get('order_type')} "
                f"qty={order.get('quantity')} trail={order.get('trailing_percent')} "
                f"stop={cls._money(order.get('initial_stop_price'))} status={order.get('status')} "
                f"orderId={order.get('order_id')} permId={order.get('perm_id')} ref={order.get('order_ref')}"
            )
            raw = order.get("raw_json")
            if raw:
                lines.append(f"  raw: {raw}")
        if not details.get("orders"):
            lines.append("No order rows found for this cycle.")

        lines.append("")
        lines.append("EXECUTIONS / TRADES")
        lines.append("=" * 80)
        for execution in details.get("executions") or []:
            lines.append(
                f"{_format_utc_timestamp(execution.get('executed_at'))} | {execution.get('side')} shares={execution.get('shares')} "
                f"price={cls._money(execution.get('price'))} avg={cls._money(execution.get('avg_price'))} "
                f"commission={cls._money(execution.get('commission'))} execId={execution.get('execution_id')} "
                f"orderRef={execution.get('order_ref')}"
            )
            raw = execution.get("raw_json")
            if raw:
                lines.append(f"  raw: {raw}")
        if not details.get("executions"):
            lines.append("No execution rows found for this cycle.")

        lines.append("")
        lines.append("DECISION AUDIT EVENTS")
        lines.append("=" * 80)
        for event in details.get("decision_events") or []:
            lines.append(
                f"{_format_utc_timestamp(event.get('created_at'))} | {event.get('event_type')} | "
                f"{event.get('stage_before') or '-'} -> {event.get('stage_after') or '-'} | "
                f"result={event.get('decision_result') or '-'} | orderId={event.get('broker_order_id') or '-'} | "
                f"permId={event.get('perm_id') or '-'} | {event.get('message')}"
            )
            raw = event.get("raw_json")
            if raw:
                lines.append(f"  raw: {raw}")
        if not details.get("decision_events"):
            lines.append("No structured decision events found for this cycle.")

        lines.append("")
        lines.append("VERBOSE LOG EVENTS")
        lines.append("=" * 80)
        for event in details.get("events") or []:
            lines.append(f"{_format_utc_timestamp(event.get('created_at'))} [{event.get('level')}] {event.get('message')}")
            raw = event.get("raw_json")
            if raw:
                lines.append(f"  raw: {raw}")
        if not details.get("events"):
            lines.append("No verbose log events found for this cycle.")
        return "\n".join(lines)

    @classmethod
    def _example_text(cls, row: dict[str, Any]) -> str:
        lines = [
            "EXAMPLE CYCLE LOG",
            "=" * 80,
            "This is built-in v3.0.18 example data only. It verifies the history, timeline, market-capture, and raw-log UI; it is not stored as a completed cycle.",
            f"Ticker: {row.get('ticker')}",
            f"Cycle: {row.get('cycle_number')}",
            f"BUY: {row.get('buy_filled_qty')} shares @ {cls._money(row.get('avg_buy_price'))}",
            f"SELL: @ {cls._money(row.get('avg_sell_price'))}",
            f"Net P/L: {cls._money(row.get('net_pnl'))}",
            "",
            "ORDERS",
            "=" * 80,
            "2026-07-07T14:36:00+00:00 | BUY TRAIL qty=96 trail=0.75 stop=$104.2150 status=Filled ref=IBKRBOT|EXAMPLE|BUY_TRAIL",
            "2026-07-07T15:28:00+00:00 | SELL TRAIL qty=96 trail=0.80 stop=$109.4860 status=Filled ref=IBKRBOT|EXAMPLE|SELL_TRAIL",
            "",
            "EXECUTIONS / TRADES",
            "=" * 80,
            "2026-07-07T14:41:18+00:00 | BOT BUY 96 @ $104.4200 commission=$1.00",
            "2026-07-07T15:39:42+00:00 | BOT SELL 96 @ $110.1800 commission=$1.00",
            "",
            "DECISION AUDIT EVENTS",
            "=" * 80,
            "PRICE_CONFIRMED -> DROP_TRIGGER_HIT -> BUY_TRAIL_SUBMITTED -> BUY_FILL -> MIN_PROFIT_REACHED -> SELL_TRAIL_SUBMITTED -> SELL_FILL -> CAPTURE_FINALIZED",
        ]
        return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self, controller: TradingController):
        super().__init__()
        self.controller = controller
        self.current_snapshot: dict[str, Any] = {}
        self._inputs_loaded_from_snapshot = False
        self._applying_snapshot_to_inputs = False
        self._updating_profit_bounds = False
        self._manual_trading_mode = "live"
        self._field_change_badges: dict[str, QLabel] = {}
        self._field_change_widgets: dict[str, QWidget] = {}
        self._change_widget_keys: dict[int, str] = {}
        self._change_field_labels: dict[str, str] = {}
        self._running_change_baseline: dict[str, Any] = {}
        self._changed_while_running_fields: set[str] = set()
        self._running_cycle_token: Optional[str] = None
        self._zero_disabled_fields: list[tuple[QWidget, QLabel, str]] = []
        self._manual_input_lock_enabled = False
        self._manual_input_lock_widget_ids: set[int] = set()
        self._stop_dialog_exit_requested = False
        self._system_shutdown_in_progress = False
        self._last_system_shutdown_session_key = ""
        self.setWindowTitle("IBKR Portable Trading Bot v3.0.18")
        self.resize(1440, 950)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(500)
        self._autosave_timer.timeout.connect(self._autosave_settings)
        self._visual_refresh_timer = QTimer(self)
        self._visual_refresh_timer.setSingleShot(True)
        self._visual_refresh_timer.setInterval(75)
        self._visual_refresh_timer.timeout.connect(self._run_visual_refresh)
        self._history_filter_timer = QTimer(self)
        self._history_filter_timer.setSingleShot(True)
        self._history_filter_timer.setInterval(200)
        self._history_filter_timer.timeout.connect(self._apply_history_filters)
        self._history_table_refresh_pending = False
        self._flowchart_history_refresh_pending = False
        self._history_columns_sized = False
        self._all_history_rows: list[dict[str, Any]] = []
        self._visible_history_rows: list[dict[str, Any]] = []

        self._build_menu()
        shell = QWidget()
        self.setCentralWidget(shell)
        self.shell_layout = QVBoxLayout(shell)
        self.shell_layout.setContentsMargins(6, 6, 6, 6)
        self.shell_layout.setSpacing(6)
        self.live_status_bar = LiveStatusBar()
        self.live_status_bar.input_lock_btn.toggled.connect(self._manual_input_lock_toggled)
        self.shell_layout.addWidget(self.live_status_bar)
        self.tabs = QTabWidget()
        self.shell_layout.addWidget(self.tabs, 1)
        self.command_bar = self._build_command_bar()
        self.dashboard_tab = QWidget()
        self.flowchart_tab = QWidget()
        self.history_tab = QWidget()
        self.recovery_tab = QWidget()
        self.tabs.addTab(self.dashboard_tab, "Live strategy")
        self.tabs.addTab(self.flowchart_tab, "Strategy flowchart")
        self.tabs.addTab(self.history_tab, "Trade history")
        self.tabs.addTab(self.recovery_tab, "Reconciliation")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(0)
        self._build_dashboard()
        self._build_flowchart()
        self._build_recovery()
        self._build_history()
        self._refresh_manual_input_lock_widget_registry()
        self._install_no_wheel_field_filter()
        self._apply_styles()
        self._apply_view_mode()
        self._update_command_bar_states({})
        self._update_strategy_previews()
        self._update_zero_disabled_indicators()

        self._wire_settings_autosave()

        # Hydrate editable controls from SQLite before worker snapshots start.
        # After this, live snapshots update only status/metrics/logs; they do
        # not overwrite user edits.
        self._apply_snapshot_to_inputs(asdict(self.controller.connection), asdict(self.controller.strategy))
        self._inputs_loaded_from_snapshot = True

        self.controller.signals.snapshot_updated.connect(self._on_snapshot)
        self.controller.signals.history_updated.connect(self._on_history)
        self.controller.signals.connection_changed.connect(self._on_connection_changed)
        self.controller.signals.ticker_search_updated.connect(self._on_ticker_search_results)
        self.controller.start_thread()
        self.controller.refresh_history()

    def _refresh_manual_input_lock_widget_registry(self) -> None:
        """Register configuration widgets disabled by the operator input lock.

        The lock is an accidental-edit guard, not a trading stop. It disables
        fields, configuration selectors, and all five workflow buttons while
        leaving tab navigation, view mode, history, and recovery controls usable.
        """
        widgets: list[QWidget] = []
        for container_name in ("connection_box", "strategy_box"):
            container = getattr(self, container_name, None)
            if container is None:
                continue
            widgets.extend(container.findChildren(QLineEdit))
            widgets.extend(container.findChildren(QSpinBox))
            widgets.extend(container.findChildren(QDoubleSpinBox))
            widgets.extend(container.findChildren(QComboBox))
            widgets.extend(container.findChildren(QCheckBox))
        for attr in ("ticker_search_btn", "ticker_use_match_btn", "ticker_confirm_btn", "browse_platform_btn"):
            widget = getattr(self, attr, None)
            if isinstance(widget, QWidget):
                widgets.append(widget)
        excluded = {id(getattr(self, "view_mode_combo", None)), id(getattr(self.live_status_bar, "input_lock_btn", None))}
        self._manual_input_lock_widget_ids = {id(widget) for widget in widgets if id(widget) not in excluded}

    def _manual_input_lock_toggled(self, checked: bool) -> None:
        self._manual_input_lock_enabled = bool(checked)
        if hasattr(self, "live_status_bar"):
            self.live_status_bar.set_input_lock_state(self._manual_input_lock_enabled)
        stage = ((self.current_snapshot or {}).get("active_cycle") or {}).get("stage")
        self._update_input_locks(stage)
        self._update_command_bar_states(self.current_snapshot)

    def _install_no_wheel_field_filter(self) -> None:
        self._no_wheel_edit_filter = NoWheelEditFilter(self)
        widgets = list(self.findChildren(QAbstractSpinBox)) + list(self.findChildren(QComboBox))
        for widget in widgets:
            widget.installEventFilter(self._no_wheel_edit_filter)
            widget.setFocusPolicy(Qt.StrongFocus)

    def _on_tab_changed(self, index: int) -> None:
        if hasattr(self, "command_bar"):
            # The command/view-mode bar is parented inside the Live strategy
            # tab. Other tabs cannot show it, and the widget is never collapsed
            # to zero height. This avoids the Windows layout bug where it could
            # return below the visible window until the window was maximized.
            self.command_bar.setVisible(True)
            self.command_bar.setEnabled(True)
            self.command_bar.setMaximumHeight(16777215)
            if index == 0:
                QTimer.singleShot(0, self._refresh_live_tab_layout)
        if hasattr(self, "recovery_corner_btn"):
            active = index == getattr(self, "recovery_tab_index", -1)
            if bool(self.recovery_corner_btn.property("activeRecovery")) != active:
                self.recovery_corner_btn.setProperty("activeRecovery", active)
                self.recovery_corner_btn.style().unpolish(self.recovery_corner_btn)
                self.recovery_corner_btn.style().polish(self.recovery_corner_btn)
        if index == 0 and self.current_snapshot and hasattr(self, "stage_ribbon"):
            cycle = self.current_snapshot.get("active_cycle")
            stage = cycle.get("stage") if cycle else None
            self.stage_ribbon.set_stage(stage)
            self._update_metrics(cycle)
            self._update_price_feed(
                self.current_snapshot.get("price_snapshot"),
                self.current_snapshot.get("price_poll_interval_seconds"),
            )
            self._update_strategy_previews()
            self._update_input_change_indicators(cycle)
            self._update_dynamic_graphs()
        elif index == 1 and hasattr(self, "flowchart_panel"):
            if self._flowchart_history_refresh_pending:
                self._apply_history_filters(force_flowchart=True)
            self._update_dynamic_graphs()
        elif index == 2 and hasattr(self, "history_table"):
            if self._history_table_refresh_pending:
                self._apply_history_filters(force_table=True)

    def _refresh_live_tab_layout(self) -> None:
        """Refresh Live strategy geometry after tab switches/resizes."""
        for attr in ("dashboard_tab", "command_bar"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.updateGeometry()
                widget.update()
        if hasattr(self, "tabs"):
            self.tabs.updateGeometry()
            self.tabs.update()
        central = self.centralWidget()
        if central is not None:
            central.updateGeometry()
            central.update()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        export_action = QAction("Export trade history CSV", self)
        export_action.triggered.connect(self._export_history)
        file_menu.addAction(export_action)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _build_command_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("CommandBar")
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.command_step_buttons: dict[str, QPushButton] = {}
        self.command_steps: dict[str, CommandStepCard] = {}
        step_specs = [
            ("connect", "1. Connect to IB Gateway API", self._connect_clicked),
            ("ticker", "2. Search / select ticker", self._search_ticker_clicked),
            ("confirm", "3. Confirm ticker + get price", self._confirm_ticker_price_clicked),
            ("start", "4. Start strategy", self._start_clicked),
            ("stop", "5. Stop strategy", self._stop_clicked),
        ]
        for key, label, callback in step_specs:
            button = QPushButton(label)
            button.setObjectName("CommandStepButton")
            self._make_button_bold(button)
            button.clicked.connect(callback)
            card = CommandStepCard(label, button)
            self.command_step_buttons[key] = button
            self.command_steps[key] = card
            layout.addWidget(card, 3)
        # The bottom command bar is the single workflow control surface.
        # Keep these aliases for existing state/lock code and tests.
        self.start_btn = self.command_step_buttons["start"]
        self.stop_btn = self.command_step_buttons["stop"]
        mode_box = QFrame()
        mode_box.setObjectName("ViewModeBox")
        mode_layout = QVBoxLayout(mode_box)
        mode_layout.setContentsMargins(8, 4, 8, 4)
        mode_label = QLabel("View mode")
        mode_label.setObjectName("Muted")
        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItems(["Simple", "Advanced", "Debug"])
        self.view_mode_combo.setCurrentText(DEFAULT_VIEW_MODE)
        self.view_mode_combo.setToolTip("Simple hides diagnostics. Advanced is the default operating view. Debug adds raw API/internal troubleshooting detail.")
        self.view_mode_combo.currentTextChanged.connect(self._apply_view_mode)
        self.view_mode_help_label = QLabel(VIEW_MODE_HELP[DEFAULT_VIEW_MODE])
        self.view_mode_help_label.setObjectName("Muted")
        self.view_mode_help_label.setWordWrap(True)
        self.view_mode_help_label.setToolTip("Simple = operational summary. Advanced = normal supervision. Debug = troubleshooting detail.")
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.view_mode_combo)
        mode_layout.addWidget(self.view_mode_help_label)
        mode_box.setMinimumWidth(260)
        layout.addWidget(mode_box, 1)
        return bar

    def _update_command_bar_states(self, snapshot: Optional[dict[str, Any]] = None) -> None:
        if not hasattr(self, "command_steps"):
            return
        snapshot = snapshot or self.current_snapshot or {}
        connected = bool(snapshot.get("connected"))
        status_text = str(snapshot.get("status") or "")
        cycle = snapshot.get("active_cycle") or {}
        price_snapshot = snapshot.get("price_snapshot") or {}
        broker_connectivity = snapshot.get("broker_connectivity") or {}
        local_connected = bool(broker_connectivity.get("local_connected", connected))
        upstream_connected = broker_connectivity.get("upstream_connected")
        upstream_recovery_pending = bool(snapshot.get("upstream_recovery_pending"))
        broker_ready = bool(
            connected
            and local_connected
            and upstream_connected is True
            and not upstream_recovery_pending
        )
        ticker_text = self.ticker_edit.text().strip().upper() if hasattr(self, "ticker_edit") else ""
        has_selected_contract = bool(self._contract_con_id_from_ui() if hasattr(self, "con_id_edit") else None)
        has_price = price_snapshot.get("price") is not None
        stage_value = str(cycle.get("stage") or "")
        active_stage = stage_value in {
            Stage.WAIT_INITIAL_DROP.value,
            Stage.BUY_TRAIL_ACTIVE.value,
            Stage.WAIT_RISE_TRIGGER.value,
            Stage.SELL_TRAIL_ACTIVE.value,
        }
        startup_resume_required = bool(snapshot.get("startup_resume_required"))
        guard_blocker = _blocking_cycle_message(cycle) if cycle else ""
        connection_error = "error" in status_text.lower()
        short_platform = "IB Gateway"
        try:
            profile = self._selected_profile_data()
            short_platform = "IB Gateway" if str(profile.get("platform") or GATEWAY_PLATFORM) == GATEWAY_PLATFORM else "TWS"
        except Exception:
            pass
        if hasattr(self, "command_step_buttons") and "connect" in self.command_step_buttons:
            connect_text = f"1. Connect to {short_platform} API"
            if self.command_step_buttons["connect"].text() != connect_text:
                self.command_step_buttons["connect"].setText(connect_text)
        if connection_error and not connected:
            self.command_steps["connect"].set_state("Error", True, status_text[:80])
        elif connected and not local_connected:
            self.command_steps["connect"].set_state("Error", True, "Local API socket is not available")
        elif connected and upstream_connected is not True:
            self.command_steps["connect"].set_state(
                "Blocked",
                False,
                "Gateway/TWS is local-only; waiting for its IBKR server connection",
            )
        elif connected and upstream_recovery_pending:
            self.command_steps["connect"].set_state("Blocked", False, "Broker state is being reconciled")
        elif connected:
            self.command_steps["connect"].set_state("Done", False, "Local and upstream broker links are ready")
        else:
            self.command_steps["connect"].set_state("Ready", True, "Start here")
        if active_stage:
            ticker_done = bool(has_selected_contract or cycle.get("ticker") or ticker_text)
            price_done = bool(has_price or cycle.get("last_price"))
            self.command_steps["ticker"].set_state(
                "Done" if ticker_done else "Blocked",
                False,
                "Locked while strategy is running",
            )
            self.command_steps["confirm"].set_state(
                "Done" if price_done else "Blocked",
                False,
                "Locked while strategy is running",
            )
        else:
            if not broker_ready:
                detail = "Wait for broker reconciliation" if upstream_recovery_pending else "IBKR server connection is not ready"
                self.command_steps["ticker"].set_state("Blocked", False, detail)
            elif has_selected_contract:
                self.command_steps["ticker"].set_state("Done", True, "Contract selected")
            elif ticker_text:
                self.command_steps["ticker"].set_state("Ready", True, "Search/select a contract")
            else:
                self.command_steps["ticker"].set_state("Not ready", False, "Enter ticker")
            if not broker_ready:
                detail = "Wait for broker reconciliation" if upstream_recovery_pending else "IBKR server connection is not ready"
                self.command_steps["confirm"].set_state("Blocked", False, detail)
            elif has_price:
                self.command_steps["confirm"].set_state("Done", True, "First usable price received")
            elif has_selected_contract or ticker_text:
                self.command_steps["confirm"].set_state("Ready", True, "Confirm ticker and read price")
            else:
                self.command_steps["confirm"].set_state("Not ready", False, "Search/select ticker first")
        if not broker_ready:
            detail = "Wait for broker reconciliation" if upstream_recovery_pending else "IBKR server connection is not ready"
            self.command_steps["start"].set_state("Blocked", False, detail)
        elif startup_resume_required:
            self.command_steps["start"].set_state("Ready", True, "Click to resume stored cycle")
        elif active_stage and guard_blocker:
            self.command_steps["start"].set_state("Blocked", False, "Trade guard is blocking BUY")
        elif active_stage:
            self.command_steps["start"].set_state("Done", False, "Strategy running")
        elif not connected:
            self.command_steps["start"].set_state("Blocked", False, "Connect first")
        elif not has_price:
            self.command_steps["start"].set_state("Blocked", False, "Confirm ticker price first")
        elif cycle.get("stage") in {Stage.ERROR.value, Stage.MANUAL_REVIEW.value}:
            self.command_steps["start"].set_state("Error", False, "Resolve recovery state")
        else:
            self.command_steps["start"].set_state("Ready", True, "Start strategy")

        can_stop = bool(cycle) and stage_value not in {"", Stage.IDLE.value, Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value}
        safe_no_running_strategy = stage_value in {"", Stage.IDLE.value, Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value}
        if startup_resume_required:
            self.command_steps["stop"].set_state("Blocked", False, "Start required before monitoring")
        elif can_stop:
            self.command_steps["stop"].set_state("Ready", True, "Open stop strategy options")
        elif safe_no_running_strategy:
            self.command_steps["stop"].set_state("Ready", True, "No strategy running; open Exit app option")
        else:
            self.command_steps["stop"].set_state("Blocked", False, "Resolve recovery state before exit")

        if bool(getattr(self, "_manual_input_lock_enabled", False)):
            for key in ("connect", "ticker", "confirm", "start", "stop"):
                self.command_steps[key].set_state(
                    "Locked",
                    False,
                    "Unlock the top-bar input lock to use this workflow action.",
                )

    def _apply_view_mode(self, *args: Any) -> None:
        mode = self.view_mode_combo.currentText() if hasattr(self, "view_mode_combo") else DEFAULT_VIEW_MODE
        simple = mode == "Simple"
        debug = mode == "Debug"
        if hasattr(self, "view_mode_help_label"):
            self.view_mode_help_label.setText(VIEW_MODE_HELP.get(mode, VIEW_MODE_HELP[DEFAULT_VIEW_MODE]))
        for attr, visible in [
            ("connection_box", not simple),
            ("strategy_box", not simple),
            ("event_log_box", True),
            ("pnl_state_box", not simple),
        ]:
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setVisible(bool(visible))
        if hasattr(self, "price_panel"):
            self.price_panel.set_debug_mode(debug)
        if hasattr(self, "flowchart_panel"):
            self.flowchart_panel.set_compact_mode(simple)

    def _build_dashboard(self) -> None:
        outer = QVBoxLayout(self.dashboard_tab)
        outer.setContentsMargins(10, 10, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer.addWidget(scroll, 1)
        outer.addWidget(self.command_bar, 0)

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self.stage_ribbon = StageRibbon()
        root.addWidget(self.stage_ribbon)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.connection_box = self._connection_group()
        self.strategy_box = self._strategy_group()
        top.addWidget(self.connection_box, 1)
        top.addWidget(self.strategy_box, 2)
        root.addLayout(top)

        self.price_panel = PricePanel()
        root.addWidget(self.price_panel)

        # Keep the live graph immediately below the price monitor so the
        # operator can read price feed, chart, and then detailed state in order.
        self.strategy_graph_box = QGroupBox("Market and strategy graph")
        strategy_graph_layout = QVBoxLayout(self.strategy_graph_box)
        strategy_graph_layout.setContentsMargins(8, 10, 8, 8)
        self.strategy_graph = StrategyGraphWidget()
        self.strategy_graph.setMinimumHeight(260)
        strategy_graph_layout.addWidget(self.strategy_graph, 1)
        root.addWidget(self.strategy_graph_box, 0)

        mid = QHBoxLayout()
        mid.setSpacing(10)
        self.market_state_box = self._market_state_group()
        self.order_state_box = self._order_state_group()
        self.pnl_state_box = self._pnl_state_group()
        mid.addWidget(self.market_state_box, 5)
        mid.addWidget(self.order_state_box, 1)
        mid.addWidget(self.pnl_state_box, 1)
        root.addLayout(mid)

        # Workflow actions live only in the fixed bottom command bar. The
        # Recovery / audit log therefore receives the full dashboard width in
        # Simple, Advanced, and Debug modes.
        self.event_log_box = self._event_log_group()
        root.addWidget(self.event_log_box, 1)

    def _connection_group(self) -> QGroupBox:
        box = QGroupBox("IBKR API connection")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.profile_combo = QComboBox()
        for profile in DEFAULT_CONNECTION_PROFILES:
            self.profile_combo.addItem(profile.label, profile.to_dict())
        self.profile_combo.addItem("Custom", {"key": "custom"})

        self.platform_combo = QComboBox()
        self.platform_combo.addItem("Trader Workstation", TWS_PLATFORM)
        self.platform_combo.addItem("IB Gateway", GATEWAY_PLATFORM)
        self.platform_combo.setCurrentIndex(1)

        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setPlaceholderText("127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(4001)
        self.client_spin = QSpinBox()
        self.client_spin.setRange(0, 999999)
        self.client_spin.setValue(11)
        self.account_edit = QLineEdit()
        self.account_edit.setPlaceholderText("Optional override; blank uses IBKR default")
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        self.market_data_combo = QComboBox()
        self.market_data_combo.addItem("Auto best available", 0)
        self.market_data_combo.addItem("Live / account permissions", 1)
        self.market_data_combo.addItem("Frozen", 2)
        self.market_data_combo.addItem("Delayed", 3)
        self.market_data_combo.addItem("Delayed frozen", 4)

        self.platform_path_edit = QLineEdit()
        self.platform_path_edit.setPlaceholderText("Optional path to IB Gateway")
        self.browse_platform_btn = QPushButton("Browse")
        self.start_platform_btn = QPushButton("Launch IB Gateway")
        path_row = QHBoxLayout()
        path_row.addWidget(self.platform_path_edit, 1)
        path_row.addWidget(self.browse_platform_btn)

        self.connect_btn = QPushButton("1. Connect to IB Gateway API")
        self.disconnect_btn = QPushButton("Disconnect")
        self._make_button_bold(self.connect_btn)
        btns = QHBoxLayout()
        btns.addWidget(self.start_platform_btn)
        btns.addWidget(self.connect_btn)
        btns.addWidget(self.disconnect_btn)

        self.connection_hint_label = QLabel(
            "Use a profile, log in to TWS/IB Gateway manually, complete 2FA, then connect. "
            "The Account field is optional; leave it blank to let IBKR select the account."
        )
        self.connection_hint_label.setObjectName("Muted")
        self.connection_hint_label.setWordWrap(True)
        self.connection_status = QLabel("Disconnected")
        self.connection_status.setObjectName("StatusLabel")
        self.connection_status.setWordWrap(True)
        self.connection_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.connection_status.setMinimumWidth(120)
        self.connection_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.db_path_label = QLabel("")
        self.db_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.db_path_label.setWordWrap(True)

        form.addRow("Profile", self.profile_combo)
        form.addRow("Platform", self.platform_combo)
        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Client ID", self.client_spin)
        form.addRow("Account", self.account_edit)
        self.connection_risk_label = QLabel("Profile mode: live")
        self.connection_risk_label.setObjectName("Muted")
        self.connection_risk_label.setWordWrap(True)
        form.addRow("Profile mode", self.connection_risk_label)
        form.addRow("Market data", self.market_data_combo)
        form.addRow("App path", path_row)
        form.addRow(btns)
        form.addRow("Help", self.connection_hint_label)
        form.addRow("Status", self.connection_status)
        form.addRow("SQLite", self.db_path_label)

        self.connect_btn.clicked.connect(self._connect_clicked)
        self.disconnect_btn.clicked.connect(self.controller.disconnect_tws)
        self.start_platform_btn.clicked.connect(self._start_platform_clicked)
        self.browse_platform_btn.clicked.connect(self._browse_platform_path)
        self._update_connection_buttons()
        return box

    def _strategy_group(self) -> QGroupBox:
        box = QGroupBox("STRATEGY INPUTS")
        box.setObjectName("StrategyInputsBox")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText("e.g. AAPL")
        self.primary_exchange_edit = QLineEdit()
        self.primary_exchange_edit.setPlaceholderText("Optional, e.g. NASDAQ")
        self.con_id_edit = QLineEdit()
        self.con_id_edit.setPlaceholderText("Filled by API selector, optional")
        self.con_id_edit.setReadOnly(True)
        self.investment_spin = self._money_spin(10000.0)
        self.initial_drop_spin = self._pct_spin(2.0)
        self.buy_rebound_spin = self._pct_spin(1.0, allow_zero=True)
        self.rise_trigger_spin = self._pct_spin(3.0)
        self.sell_trail_spin = self._pct_spin(1.0, allow_zero=True)
        self.atr_adaptive_check = QCheckBox("Use ATR adaptive percentages")
        self.atr_adaptive_check.setChecked(True)
        self.atr_block_until_ready_check = QCheckBox("Block new BUY until ATR has enough RTH data")
        self.atr_block_until_ready_check.setChecked(True)
        self.atr_min_profit_adaptive_check = QCheckBox("Adapt Minimum profit % with ATR")
        self.atr_min_profit_adaptive_check.setChecked(True)
        self.atr_protective_sell_adaptive_check = QCheckBox("Adapt Protective SELL trailing-stop % with ATR")
        self.atr_protective_sell_adaptive_check.setChecked(False)
        self.atr_period_spin = QSpinBox(); self.atr_period_spin.setRange(2, 200); self.atr_period_spin.setValue(14); self.atr_period_spin.setSuffix(" bars")
        self.atr_bar_seconds_spin = QSpinBox(); self.atr_bar_seconds_spin.setRange(5, 3600); self.atr_bar_seconds_spin.setValue(60); self.atr_bar_seconds_spin.setSuffix(" s bars")
        self.atr_initial_drop_mult_spin = self._mult_spin(1.50)
        self.atr_buy_rebound_mult_spin = self._mult_spin(0.75, allow_zero=True)
        self.atr_min_profit_mult_spin = self._mult_spin(1.00)
        self.atr_sell_trail_mult_spin = self._mult_spin(1.00, allow_zero=True)
        self.atr_protective_sell_mult_spin = self._mult_spin(3.00)
        self.atr_min_pct_spin = self._pct_spin(0.10)
        self.atr_max_pct_spin = self._pct_spin(20.00)
        self.atr_status_label = QLabel("ATR adaptive mode OFF")
        self.atr_status_label.setObjectName("Muted")
        self.atr_status_label.setWordWrap(True)
        self.protective_sell_check = QCheckBox("Enable protective SELL trailing-stop immediately after BUY fill")
        self.protective_sell_check.setChecked(False)
        self.protective_sell_trail_spin = self._pct_spin(3.0)
        self.slippage_buffer_check = QCheckBox("Use BUY/SELL slippage buffer")
        self.slippage_buffer_check.setChecked(False)
        self.slippage_buffer_spin = self._pct_spin(0.50)
        self.hard_risk_limits_check = QCheckBox("Enable hard risk limits")
        self.hard_risk_limits_check.setChecked(False)
        self.max_daily_loss_ticker_spin = self._money_spin(0.0)
        self.max_daily_loss_total_spin = self._money_spin(0.0)
        self.max_cycles_ticker_day_spin = QSpinBox()
        self.max_cycles_ticker_day_spin.setRange(0, 100000)
        self.max_cycles_ticker_day_spin.setValue(0)
        self.max_consecutive_losses_spin = QSpinBox()
        self.max_consecutive_losses_spin.setRange(0, 100000)
        self.max_consecutive_losses_spin.setValue(0)
        self.max_spread_pct_spin = self._pct_spin(1.00)
        self.max_gap_pct_spin = self._pct_spin(0.00, allow_zero=True)
        self.min_trade_price_spin = self._money_spin(0.00)
        for money_spin in [self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.min_trade_price_spin]:
            money_spin.setMinimum(0.0)
        self.max_daily_loss_ticker_spin.setValue(0.0)
        self.max_daily_loss_total_spin.setValue(0.0)
        self.min_trade_price_spin.setValue(0.0)
        for pct_spin in [self.max_spread_pct_spin, self.max_gap_pct_spin]:
            pct_spin.setMinimum(0.0)
        self.block_delayed_live_check = QCheckBox("Block live orders when API data is delayed/frozen")
        self.block_delayed_live_check.setChecked(True)
        self.what_if_check = QCheckBox("Run IBKR what-if margin check before BUY")
        self.what_if_check.setChecked(True)
        self.stale_data_guard_check = QCheckBox("Block stale API data")
        self.stale_data_guard_check.setChecked(True)
        self.max_price_age_spin = QDoubleSpinBox(); self.max_price_age_spin.setRange(0.5, 3600.0); self.max_price_age_spin.setDecimals(1); self.max_price_age_spin.setValue(3.0); self.max_price_age_spin.setSuffix(" s")
        self.max_bidask_age_spin = QDoubleSpinBox(); self.max_bidask_age_spin.setRange(0.5, 3600.0); self.max_bidask_age_spin.setDecimals(1); self.max_bidask_age_spin.setValue(3.0); self.max_bidask_age_spin.setSuffix(" s")
        self.max_rth_age_spin = QDoubleSpinBox(); self.max_rth_age_spin.setRange(1.0, 3600.0); self.max_rth_age_spin.setDecimals(1); self.max_rth_age_spin.setValue(60.0); self.max_rth_age_spin.setSuffix(" s")
        self.volatility_filter_check = QCheckBox("Block high recent volatility")
        self.volatility_filter_check.setChecked(False)
        self.volatility_window_spin = QSpinBox(); self.volatility_window_spin.setRange(10, 86400); self.volatility_window_spin.setValue(300); self.volatility_window_spin.setSuffix(" s")
        self.max_recent_move_spin = self._pct_spin(5.00)
        self.session_timing_guard_check = QCheckBox("Block new BUY near open/close")
        self.session_timing_guard_check.setChecked(True)
        self.no_new_buy_first_spin = QSpinBox(); self.no_new_buy_first_spin.setRange(0, 240); self.no_new_buy_first_spin.setValue(5); self.no_new_buy_first_spin.setSuffix(" min")
        self.no_new_buy_last_spin = QSpinBox(); self.no_new_buy_last_spin.setRange(0, 240); self.no_new_buy_last_spin.setValue(15); self.no_new_buy_last_spin.setSuffix(" min")
        self.cancel_buy_before_close_spin = QSpinBox(); self.cancel_buy_before_close_spin.setRange(0, 240); self.cancel_buy_before_close_spin.setValue(5); self.cancel_buy_before_close_spin.setSuffix(" min")
        self.reinvest_check = QCheckBox("Reinvest realized net profit from this ticker")
        self.reinvest_check.setChecked(True)
        self.auto_repeat_check = QCheckBox("Auto-repeat indefinitely")
        self.auto_repeat_check.setChecked(True)

        self.ticker_matches_combo = QComboBox()
        self.ticker_matches_combo.addItem("No API ticker search yet", None)
        self.ticker_search_btn = QPushButton("Search for ticker")
        self.ticker_use_match_btn = QPushButton("2. Use selected match")
        self.ticker_confirm_btn = QPushButton("3. Confirm ticker + get first price")
        for numbered_btn in [self.ticker_use_match_btn, self.ticker_confirm_btn]:
            self._make_button_bold(numbered_btn)

        self.ticker_info = self._info_badge()
        self.primary_exchange_info = self._info_badge()
        self.con_id_info = self._info_badge()
        self.investment_info = self._info_badge()
        self.initial_drop_info = self._info_badge()
        self.buy_rebound_info = self._info_badge()
        self.rise_trigger_info = self._info_badge()
        self.sell_trail_info = self._info_badge()
        self.atr_adaptive_info = self._info_badge()
        self.atr_block_until_ready_info = self._info_badge()
        self.atr_min_profit_adaptive_info = self._info_badge()
        self.atr_protective_sell_adaptive_info = self._info_badge()
        self.atr_period_info = self._info_badge()
        self.atr_bar_seconds_info = self._info_badge()
        self.atr_initial_drop_mult_info = self._info_badge()
        self.atr_buy_rebound_mult_info = self._info_badge()
        self.atr_min_profit_mult_info = self._info_badge()
        self.atr_sell_trail_mult_info = self._info_badge()
        self.atr_protective_sell_mult_info = self._info_badge()
        self.atr_min_pct_info = self._info_badge()
        self.atr_max_pct_info = self._info_badge()
        self.ticker_selector_info = self._info_badge()
        self.reinvest_info = self._info_badge()
        self.auto_repeat_info = self._info_badge()
        self.protective_sell_info = self._info_badge()
        self.protective_sell_trail_info = self._info_badge()
        self.slippage_buffer_info = self._info_badge()
        self.slippage_buffer_pct_info = self._info_badge()
        self.hard_risk_limits_info = self._info_badge()
        self.max_daily_loss_ticker_info = self._info_badge()
        self.max_daily_loss_total_info = self._info_badge()
        self.max_cycles_info = self._info_badge()
        self.max_consecutive_info = self._info_badge()
        self.max_spread_info = self._info_badge()
        self.min_trade_price_info = self._info_badge()
        self.max_gap_info = self._info_badge()
        self.block_delayed_live_info = self._info_badge()
        self.what_if_info = self._info_badge()
        self.stale_data_info = self._info_badge()
        self.max_price_age_info = self._info_badge()
        self.max_bidask_age_info = self._info_badge()
        self.max_rth_age_info = self._info_badge()
        self.volatility_filter_info = self._info_badge()
        self.volatility_window_info = self._info_badge()
        self.max_recent_move_info = self._info_badge()
        self.session_timing_info = self._info_badge()
        self.no_new_buy_first_info = self._info_badge()
        self.no_new_buy_last_info = self._info_badge()
        self.cancel_buy_before_close_info = self._info_badge()

        self._mark_zero_disabled(self.buy_rebound_spin, "0 = BUY trailing-stop disabled; market BUY after drop")
        self._mark_zero_disabled(self.sell_trail_spin, "0 = SELL trailing-stop disabled; market SELL at trigger")
        self._mark_zero_disabled(self.atr_buy_rebound_mult_spin, "0 = ATR BUY trail disabled")
        self._mark_zero_disabled(self.atr_sell_trail_mult_spin, "0 = ATR SELL trail disabled")
        self._mark_zero_disabled(self.max_daily_loss_ticker_spin, "0 = disabled")
        self._mark_zero_disabled(self.max_daily_loss_total_spin, "0 = disabled")
        self._mark_zero_disabled(self.max_cycles_ticker_day_spin, "0 = disabled")
        self._mark_zero_disabled(self.max_consecutive_losses_spin, "0 = disabled")
        self._mark_zero_disabled(self.max_spread_pct_spin, "0 = disabled")
        self._mark_zero_disabled(self.min_trade_price_spin, "0 = disabled")
        self._mark_zero_disabled(self.max_gap_pct_spin, "0 = disabled")
        self._mark_zero_disabled(self.no_new_buy_first_spin, "0 = disabled")
        self._mark_zero_disabled(self.no_new_buy_last_spin, "0 = disabled")
        self._mark_zero_disabled(self.cancel_buy_before_close_spin, "0 = disabled")

        self._register_change_tracked_inputs([
            ("ticker", "Ticker", self.ticker_edit, "contract"),
            ("primary_exchange", "Primary exchange", self.primary_exchange_edit, "contract"),
            ("con_id", "IBKR conId", self.con_id_edit, "contract"),
            ("investment_amount", "Investment amount", self.investment_spin, "entry"),
            ("initial_drop_pct", "Initial drop %", self.initial_drop_spin, "entry"),
            ("buy_rebound_trail_pct", "BUY rebound/trail %", self.buy_rebound_spin, "entry"),
            ("rise_trigger_pct", "Minimum profit %", self.rise_trigger_spin, "exit"),
            ("sell_trailing_stop_pct", "SELL trailing-stop %", self.sell_trail_spin, "exit"),
            ("protective_sell_enabled", "Protective SELL enabled", self.protective_sell_check, "protective"),
            ("protective_sell_trailing_stop_pct", "Protective SELL trailing-stop %", self.protective_sell_trail_spin, "protective"),
            ("slippage_buffer_enabled", "Slippage buffer enabled", self.slippage_buffer_check, "slippage"),
            ("slippage_buffer_pct", "Slippage buffer %", self.slippage_buffer_spin, "slippage"),
            ("atr_adaptive_enabled", "ATR adaptive mode", self.atr_adaptive_check, "atr_general"),
            ("atr_block_new_buy_until_ready", "ATR warmup BUY block", self.atr_block_until_ready_check, "risk"),
            ("atr_adapt_minimum_profit_enabled", "ATR adapts minimum profit", self.atr_min_profit_adaptive_check, "atr_exit"),
            ("atr_adapt_protective_sell_enabled", "ATR adapts Protective SELL", self.atr_protective_sell_adaptive_check, "atr_exit"),
            ("atr_period", "ATR period", self.atr_period_spin, "atr_general"),
            ("atr_bar_seconds", "ATR bar size", self.atr_bar_seconds_spin, "atr_general"),
            ("atr_initial_drop_multiplier", "ATR initial-drop multiplier", self.atr_initial_drop_mult_spin, "atr_entry"),
            ("atr_buy_rebound_multiplier", "ATR BUY-rebound multiplier", self.atr_buy_rebound_mult_spin, "atr_entry"),
            ("atr_minimum_profit_multiplier", "ATR minimum-profit multiplier", self.atr_min_profit_mult_spin, "atr_exit"),
            ("atr_sell_trail_multiplier", "ATR SELL-trail multiplier", self.atr_sell_trail_mult_spin, "atr_exit"),
            ("atr_protective_sell_multiplier", "ATR protective SELL multiplier", self.atr_protective_sell_mult_spin, "atr_exit"),
            ("atr_min_pct", "ATR min adaptive %", self.atr_min_pct_spin, "atr_general"),
            ("atr_max_pct", "ATR max adaptive %", self.atr_max_pct_spin, "atr_general"),
            ("hard_risk_limits_enabled", "Hard risk limits", self.hard_risk_limits_check, "risk"),
            ("max_daily_loss_ticker", "Max ticker loss/day", self.max_daily_loss_ticker_spin, "risk"),
            ("max_daily_loss_total", "Max total loss/day", self.max_daily_loss_total_spin, "risk"),
            ("max_cycles_per_ticker_day", "Max cycles", self.max_cycles_ticker_day_spin, "risk"),
            ("max_consecutive_losses", "Max consecutive losses", self.max_consecutive_losses_spin, "risk"),
            ("max_spread_pct", "Max spread %", self.max_spread_pct_spin, "risk"),
            ("min_trade_price", "Min trade price", self.min_trade_price_spin, "risk"),
            ("max_gap_from_prev_close_pct", "Max gap from close %", self.max_gap_pct_spin, "risk"),
            ("block_delayed_data_in_live", "Block delayed/frozen live data", self.block_delayed_live_check, "risk"),
            ("what_if_check_enabled", "IBKR what-if check", self.what_if_check, "risk"),
            ("stale_data_guard_enabled", "Stale data guard", self.stale_data_guard_check, "risk"),
            ("max_selected_price_age_seconds", "Max selected price age", self.max_price_age_spin, "risk"),
            ("max_bid_ask_age_seconds", "Max bid/ask age", self.max_bidask_age_spin, "risk"),
            ("max_rth_status_age_seconds", "Max RTH status age", self.max_rth_age_spin, "risk"),
            ("volatility_filter_enabled", "Volatility filter", self.volatility_filter_check, "risk"),
            ("volatility_window_seconds", "Volatility window", self.volatility_window_spin, "risk"),
            ("max_recent_price_move_pct", "Max recent move %", self.max_recent_move_spin, "risk"),
            ("session_timing_guard_enabled", "Session timing guard", self.session_timing_guard_check, "risk"),
            ("no_new_buy_first_minutes", "No new BUY first", self.no_new_buy_first_spin, "risk"),
            ("no_new_buy_last_minutes", "No new BUY last", self.no_new_buy_last_spin, "risk"),
            ("cancel_buy_before_close_minutes", "Cancel BUY before close", self.cancel_buy_before_close_spin, "risk"),
            ("reinvest_profits", "Reinvest profits", self.reinvest_check, "automation"),
            ("auto_repeat", "Auto-repeat", self.auto_repeat_check, "automation"),
        ])

        entry_box = QGroupBox("ENTRY")
        entry_box.setObjectName("EntryBox")
        entry_grid = QGridLayout(entry_box)
        entry_grid.setHorizontalSpacing(12)
        entry_grid.setVerticalSpacing(7)
        entry_fields = [
            ("Investment amount", self._pct_field(self.investment_spin, self.investment_info)),
            ("Initial drop %", self._pct_field(self.initial_drop_spin, self.initial_drop_info)),
            ("BUY rebound/trail %", self._pct_field(self.buy_rebound_spin, self.buy_rebound_info)),
        ]
        for i, (label, widget) in enumerate(entry_fields):
            row = i // 2
            col = (i % 2) * 2
            entry_grid.addWidget(QLabel(label), row, col)
            entry_grid.addWidget(widget, row, col + 1)
        self.entry_preview_label = QLabel("Entry preview: waiting for a usable reference price.")
        self.entry_preview_label.setObjectName("PreviewLabel")
        self.entry_preview_label.setWordWrap(True)
        entry_grid.addWidget(self.entry_preview_label, 2, 0, 1, 4)
        grid.addWidget(entry_box, 1, 0, 1, 4)

        ticker_box = QGroupBox("Ticker search / contract confirmation")
        ticker_grid = QGridLayout(ticker_box)
        ticker_grid.setHorizontalSpacing(12)
        ticker_grid.setVerticalSpacing(7)
        ticker_grid.addWidget(QLabel("Ticker"), 0, 0)
        ticker_grid.addWidget(self._pct_field(self.ticker_edit, self.ticker_info), 0, 1)
        ticker_grid.addWidget(QLabel("Primary exchange"), 0, 2)
        ticker_grid.addWidget(self._pct_field(self.primary_exchange_edit, self.primary_exchange_info), 0, 3)
        ticker_grid.addWidget(QLabel("IBKR conId"), 1, 0)
        ticker_grid.addWidget(self._pct_field(self.con_id_edit, self.con_id_info), 1, 1)
        ticker_grid.addWidget(self._label_with_info("API ticker selector", self.ticker_selector_info), 1, 2)
        ticker_grid.addWidget(self.ticker_matches_combo, 1, 3)
        selector_buttons = QHBoxLayout()
        selector_buttons.addWidget(self.ticker_search_btn)
        selector_buttons.addWidget(self.ticker_use_match_btn)
        selector_buttons.addWidget(self.ticker_confirm_btn)
        ticker_grid.addLayout(selector_buttons, 2, 0, 1, 4)
        self.contract_label = QLabel("Contract: STK / SMART / optional primary exchange / USD.")
        self.contract_label.setObjectName("Muted")
        self.contract_label.setWordWrap(True)
        ticker_grid.addWidget(self.contract_label, 3, 0, 1, 4)
        grid.addWidget(ticker_box, 0, 0, 1, 4)

        exit_box = QGroupBox("EXIT")
        exit_box.setObjectName("ExitBox")
        exit_grid = QGridLayout(exit_box)
        exit_grid.setHorizontalSpacing(12)
        exit_grid.setVerticalSpacing(7)
        exit_grid.addWidget(QLabel("Minimum profit %"), 0, 0)
        exit_grid.addWidget(self._pct_field(self.rise_trigger_spin, self.rise_trigger_info), 0, 1)
        exit_grid.addWidget(QLabel("SELL trailing-stop %"), 0, 2)
        exit_grid.addWidget(self._pct_field(self.sell_trail_spin, self.sell_trail_info), 0, 3)
        exit_grid.addWidget(self._pct_field(self.protective_sell_check, self.protective_sell_info), 1, 0, 1, 2)
        exit_grid.addWidget(QLabel("Protective SELL trailing-stop %"), 1, 2)
        exit_grid.addWidget(self._pct_field(self.protective_sell_trail_spin, self.protective_sell_trail_info), 1, 3)
        exit_grid.addWidget(self._pct_field(self.slippage_buffer_check, self.slippage_buffer_info), 2, 0, 1, 2)
        exit_grid.addWidget(QLabel("Slippage buffer %"), 2, 2)
        exit_grid.addWidget(self._pct_field(self.slippage_buffer_spin, self.slippage_buffer_pct_info), 2, 3)
        self.exit_preview_label = QLabel("Exit preview: waiting for an estimated or actual BUY reference.")
        self.exit_preview_label.setObjectName("PreviewLabel")
        self.exit_preview_label.setWordWrap(True)
        exit_grid.addWidget(self.exit_preview_label, 3, 0, 1, 4)
        grid.addWidget(exit_box, 2, 0, 1, 4)

        atr_box = QGroupBox("ATR mode")
        atr_grid = QGridLayout(atr_box)
        atr_grid.setHorizontalSpacing(12)
        atr_grid.setVerticalSpacing(7)
        atr_grid.addWidget(self._pct_field(self.atr_adaptive_check, self.atr_adaptive_info), 0, 0, 1, 4)
        atr_grid.addWidget(self._pct_field(self.atr_block_until_ready_check, self.atr_block_until_ready_info), 1, 0, 1, 4)
        atr_grid.addWidget(self._pct_field(self.atr_min_profit_adaptive_check, self.atr_min_profit_adaptive_info), 2, 0, 1, 2)
        atr_grid.addWidget(self._pct_field(self.atr_protective_sell_adaptive_check, self.atr_protective_sell_adaptive_info), 2, 2, 1, 2)
        atr_grid.addWidget(QLabel("ATR period"), 3, 0)
        atr_grid.addWidget(self._pct_field(self.atr_period_spin, self.atr_period_info), 3, 1)
        atr_grid.addWidget(QLabel("Bar size"), 3, 2)
        atr_grid.addWidget(self._pct_field(self.atr_bar_seconds_spin, self.atr_bar_seconds_info), 3, 3)
        atr_grid.addWidget(QLabel("Initial drop"), 4, 0)
        atr_grid.addWidget(self._pct_field(self.atr_initial_drop_mult_spin, self.atr_initial_drop_mult_info), 4, 1)
        atr_grid.addWidget(QLabel("BUY rebound"), 4, 2)
        atr_grid.addWidget(self._pct_field(self.atr_buy_rebound_mult_spin, self.atr_buy_rebound_mult_info), 4, 3)
        atr_grid.addWidget(QLabel("Minimum profit"), 5, 0)
        atr_grid.addWidget(self._pct_field(self.atr_min_profit_mult_spin, self.atr_min_profit_mult_info), 5, 1)
        atr_grid.addWidget(QLabel("SELL trailing-stop"), 5, 2)
        atr_grid.addWidget(self._pct_field(self.atr_sell_trail_mult_spin, self.atr_sell_trail_mult_info), 5, 3)
        atr_grid.addWidget(QLabel("Protective SELL"), 6, 0)
        atr_grid.addWidget(self._pct_field(self.atr_protective_sell_mult_spin, self.atr_protective_sell_mult_info), 6, 1)
        atr_grid.addWidget(QLabel("Min adaptive %"), 7, 0)
        atr_grid.addWidget(self._pct_field(self.atr_min_pct_spin, self.atr_min_pct_info), 7, 1)
        atr_grid.addWidget(QLabel("Max adaptive %"), 7, 2)
        atr_grid.addWidget(self._pct_field(self.atr_max_pct_spin, self.atr_max_pct_info), 7, 3)
        atr_grid.addWidget(self.atr_status_label, 8, 0, 1, 4)
        grid.addWidget(atr_box, 3, 0, 1, 4)

        risk_box = QGroupBox("Risk and timing")
        risk_grid = QGridLayout(risk_box)
        risk_grid.setHorizontalSpacing(12)
        risk_grid.setVerticalSpacing(7)
        risk_grid.addWidget(self._pct_field(self.hard_risk_limits_check, self.hard_risk_limits_info), 0, 0, 1, 4)
        risk_grid.addWidget(QLabel("Max ticker loss/day"), 1, 0)
        risk_grid.addWidget(self._pct_field(self.max_daily_loss_ticker_spin, self.max_daily_loss_ticker_info), 1, 1)
        risk_grid.addWidget(QLabel("Max total loss/day"), 1, 2)
        risk_grid.addWidget(self._pct_field(self.max_daily_loss_total_spin, self.max_daily_loss_total_info), 1, 3)
        risk_grid.addWidget(QLabel("Max cycles"), 2, 0)
        risk_grid.addWidget(self._pct_field(self.max_cycles_ticker_day_spin, self.max_cycles_info), 2, 1)
        risk_grid.addWidget(QLabel("Max consecutive losses"), 2, 2)
        risk_grid.addWidget(self._pct_field(self.max_consecutive_losses_spin, self.max_consecutive_info), 2, 3)
        risk_grid.addWidget(QLabel("Max spread %"), 3, 0)
        risk_grid.addWidget(self._pct_field(self.max_spread_pct_spin, self.max_spread_info), 3, 1)
        risk_grid.addWidget(QLabel("Min trade price"), 3, 2)
        risk_grid.addWidget(self._pct_field(self.min_trade_price_spin, self.min_trade_price_info), 3, 3)
        risk_grid.addWidget(QLabel("Max gap from close %"), 4, 0)
        risk_grid.addWidget(self._pct_field(self.max_gap_pct_spin, self.max_gap_info), 4, 1)
        risk_grid.addWidget(self._pct_field(self.block_delayed_live_check, self.block_delayed_live_info), 4, 2, 1, 2)
        risk_grid.addWidget(self._pct_field(self.what_if_check, self.what_if_info), 5, 0, 1, 2)
        risk_grid.addWidget(self._pct_field(self.stale_data_guard_check, self.stale_data_info), 5, 2, 1, 2)
        risk_grid.addWidget(QLabel("Max selected price age"), 6, 0)
        risk_grid.addWidget(self._pct_field(self.max_price_age_spin, self.max_price_age_info), 6, 1)
        risk_grid.addWidget(QLabel("Max bid/ask age"), 6, 2)
        risk_grid.addWidget(self._pct_field(self.max_bidask_age_spin, self.max_bidask_age_info), 6, 3)
        risk_grid.addWidget(QLabel("Max RTH status age"), 7, 0)
        risk_grid.addWidget(self._pct_field(self.max_rth_age_spin, self.max_rth_age_info), 7, 1)
        risk_grid.addWidget(self._pct_field(self.volatility_filter_check, self.volatility_filter_info), 7, 2, 1, 2)
        risk_grid.addWidget(QLabel("Volatility window"), 8, 0)
        risk_grid.addWidget(self._pct_field(self.volatility_window_spin, self.volatility_window_info), 8, 1)
        risk_grid.addWidget(QLabel("Max recent move %"), 8, 2)
        risk_grid.addWidget(self._pct_field(self.max_recent_move_spin, self.max_recent_move_info), 8, 3)
        risk_grid.addWidget(self._pct_field(self.session_timing_guard_check, self.session_timing_info), 9, 0, 1, 2)
        risk_grid.addWidget(QLabel("No new BUY first"), 10, 0)
        risk_grid.addWidget(self._pct_field(self.no_new_buy_first_spin, self.no_new_buy_first_info), 10, 1)
        risk_grid.addWidget(QLabel("No new BUY last"), 10, 2)
        risk_grid.addWidget(self._pct_field(self.no_new_buy_last_spin, self.no_new_buy_last_info), 10, 3)
        risk_grid.addWidget(QLabel("Cancel BUY before close"), 11, 0)
        risk_grid.addWidget(self._pct_field(self.cancel_buy_before_close_spin, self.cancel_buy_before_close_info), 11, 1)
        self.risk_zero_disabled_summary = QLabel("Values set to 0 are disabled.")
        self.risk_zero_disabled_summary.setObjectName("Muted")
        self.risk_zero_disabled_summary.setWordWrap(True)
        risk_grid.addWidget(self.risk_zero_disabled_summary, 12, 0, 1, 4)
        grid.addWidget(risk_box, 4, 0, 1, 4)

        automation_box = QGroupBox("Automation")
        automation_grid = QGridLayout(automation_box)
        automation_grid.addWidget(self._pct_field(self.reinvest_check, self.reinvest_info), 0, 0, 1, 2)
        automation_grid.addWidget(self._pct_field(self.auto_repeat_check, self.auto_repeat_info), 0, 2, 1, 2)
        self.changed_while_running_label = QLabel("Changed while running: no active cycle.")
        self.changed_while_running_label.setObjectName("PreviewLabel")
        self.changed_while_running_label.setWordWrap(True)
        automation_grid.addWidget(self.changed_while_running_label, 1, 0, 1, 4)
        grid.addWidget(automation_box, 5, 0, 1, 4)

        self.edit_lock_label = QLabel("Fields lock automatically when changing them would require replacing an active native order.")
        self.edit_lock_label.setObjectName("Muted")
        self.edit_lock_label.setWordWrap(True)
        grid.addWidget(self.edit_lock_label, 6, 0, 1, 4)

        self.profit_guard_label = QLabel("")
        self.profit_guard_label.setObjectName("ProfitGuardGood")
        self.profit_guard_label.setWordWrap(True)
        grid.addWidget(self.profit_guard_label, 7, 0, 1, 4)

        self.profit_guard_graph = ProfitGuardWidget()
        grid.addWidget(self.profit_guard_graph, 8, 0, 1, 4)
        self.native_trail_note = QLabel("Trailing percentages above 0 use native TWS/IB Gateway trailing-stop orders after acceptance; 0 disables trailing for that side and uses a market order at the configured trigger. Chart levels are app-side planning estimates.")
        self.native_trail_note.setObjectName("Muted")
        self.native_trail_note.setWordWrap(True)
        grid.addWidget(self.native_trail_note, 9, 0, 1, 4)

        self.ticker_search_btn.clicked.connect(self._search_ticker_clicked)
        self.ticker_use_match_btn.clicked.connect(self._use_selected_ticker_match)
        self.ticker_confirm_btn.clicked.connect(self._confirm_ticker_price_clicked)
        self.ticker_edit.textEdited.connect(self._clear_selected_contract_con_id)
        self.ticker_edit.returnPressed.connect(self._search_ticker_clicked)
        self.primary_exchange_edit.textEdited.connect(self._clear_selected_contract_con_id)
        for widget in [self.initial_drop_spin, self.buy_rebound_spin, self.rise_trigger_spin, self.sell_trail_spin, self.atr_period_spin, self.atr_bar_seconds_spin, self.atr_initial_drop_mult_spin, self.atr_buy_rebound_mult_spin, self.atr_min_profit_mult_spin, self.atr_sell_trail_mult_spin, self.atr_protective_sell_mult_spin, self.atr_min_pct_spin, self.atr_max_pct_spin, self.protective_sell_trail_spin, self.slippage_buffer_spin, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_window_spin, self.max_recent_move_spin, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin]:
            widget.valueChanged.connect(self._strategy_visual_inputs_changed)
        for widget in [self.atr_adaptive_check, self.atr_block_until_ready_check, self.atr_min_profit_adaptive_check, self.atr_protective_sell_adaptive_check, self.protective_sell_check, self.slippage_buffer_check, self.hard_risk_limits_check, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.volatility_filter_check, self.session_timing_guard_check]:
            widget.toggled.connect(self._strategy_visual_inputs_changed)
        self._last_suggested_risk_limits: dict[str, float | int] = {}
        self._last_suggested_safety_defaults: dict[str, float | int] = {}
        self.investment_spin.valueChanged.connect(self._apply_suggested_risk_limits_from_amount)
        self.investment_spin.valueChanged.connect(self._apply_suggested_broker_timing_defaults_from_amount)
        self._apply_suggested_risk_limits_from_amount(force=True)
        self._apply_suggested_broker_timing_defaults_from_amount(force=True)
        self._apply_profit_guard_bounds()
        return box

    def _price_feed_group(self) -> QGroupBox:
        box = QGroupBox("Price data feed - live API subscription")
        root = QVBoxLayout(box)

        header = QHBoxLayout()
        self.price_big_value = QLabel("-")
        self.price_big_value.setObjectName("PriceBig")
        self.price_status_label = QLabel("No price yet")
        self.price_status_label.setObjectName("PriceStatusBad")
        self.price_source_label = QLabel("Source: -")
        self.price_source_label.setObjectName("Muted")
        self.price_updated_label = QLabel("Last update: -")
        self.price_updated_label.setObjectName("Muted")
        self.price_mode_label = QLabel("Requested mode: -")
        self.price_mode_label.setObjectName("Muted")
        header.addWidget(self.price_big_value, 2)
        header.addWidget(self.price_status_label, 1)
        header.addWidget(self.price_source_label, 1)
        header.addWidget(self.price_updated_label, 1)
        header.addWidget(self.price_mode_label, 1)
        root.addLayout(header)

        grid = QGridLayout()
        self.price_cards: dict[str, MetricCard] = {}
        for idx, title in enumerate([
            "Last",
            "Delayed last",
            "Bid",
            "Ask",
            "Midpoint",
            "Delayed midpoint",
            "Mark",
            "Delayed mark",
            "Close",
            "Delayed close",
            "Generic ticks",
            "Recent API error",
        ]):
            card = MetricCard(title)
            self.price_cards[title] = card
            grid.addWidget(card, idx // 6, idx % 6)
        root.addLayout(grid)
        return box

    def _market_state_group(self) -> QGroupBox:
        box = QGroupBox("Market and strategy state")
        layout = QVBoxLayout(box)
        grid = QGridLayout()
        self.metrics: dict[str, MetricCard] = {}
        for idx, title in enumerate([
            "Current last price",
            "Anchor price",
            "Initial drop trigger",
            "BUY initial trailing-stop",
            "Average buy fill",
            "Minimum-profit trigger price",
            "Protective SELL stop",
            "SELL initial trailing-stop",
            "Stage",
        ]):
            card = MetricCard(title)
            self.metrics[title] = card
            grid.addWidget(card, idx // 2, idx % 2)
        layout.addLayout(grid)
        self.current_stage_panel = CurrentStagePanel()
        layout.addWidget(self.current_stage_panel)
        self.why_not_moving_panel = WhyNotMovingPanel()
        layout.addWidget(self.why_not_moving_panel)
        return box

    def _order_state_group(self) -> QGroupBox:
        box = QGroupBox("Order and position state")
        box.setMinimumWidth(220)
        box.setMaximumWidth(300)
        layout = QVBoxLayout(box)
        for title in [
            "Quantity",
            "Buy filled qty",
            "Sell filled qty",
            "Buy order ID",
            "Buy permId",
            "Protective order ID",
            "Protective status",
            "Sell order ID",
            "Sell permId",
            "OrderRef",
        ]:
            card = MetricCard(title)
            card.setMinimumHeight(58)
            self.metrics[title] = card
            layout.addWidget(card)
        layout.addStretch(1)
        return box

    def _pnl_state_group(self) -> QGroupBox:
        box = QGroupBox("Budget and P/L")
        box.setMinimumWidth(220)
        box.setMaximumWidth(300)
        layout = QVBoxLayout(box)
        for title in [
            "Investment amount",
            "Cycle budget",
            "Reinvested profit",
            "Gross P/L",
            "Net P/L",
        ]:
            card = MetricCard(title)
            card.setMinimumHeight(58)
            self.metrics[title] = card
            layout.addWidget(card)
        layout.addStretch(1)
        return box

    def _event_log_group(self) -> QGroupBox:
        box = QGroupBox("Recovery / audit log")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(box)
        self.event_log = QTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMinimumHeight(220)
        self.event_log.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.event_log.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        layout.addWidget(self.event_log)
        return box

    def _build_history(self) -> None:
        root = QVBoxLayout(self.history_tab)
        filters = QHBoxLayout()
        self.history_ticker_filter = QLineEdit()
        self.history_ticker_filter.setPlaceholderText("Filter ticker, optional")
        self.history_from_filter = QLineEdit()
        self.history_from_filter.setPlaceholderText("YYYY-MM-DD")
        self.history_to_filter = QLineEdit()
        self.history_to_filter.setPlaceholderText("YYYY-MM-DD")
        self.history_outcome_filter = QComboBox()
        self.history_outcome_filter.addItems(["All outcomes", "Profitable", "Losing", "Profit exit", "Protective exit", "Manual/error", "Cancelled"])
        self.history_atr_filter = QComboBox()
        self.history_atr_filter.addItems(["ATR all", "ATR on", "ATR off"])
        self.history_mode_filter = QComboBox()
        self.history_mode_filter.addItems(["Paper/live all", "Paper", "Live"])
        self.history_refresh_btn = QPushButton("Refresh")
        self.history_export_btn = QPushButton("Export CSV")
        filters.addWidget(QLabel("Ticker"))
        filters.addWidget(self.history_ticker_filter)
        filters.addWidget(QLabel("From"))
        filters.addWidget(self.history_from_filter)
        filters.addWidget(QLabel("To"))
        filters.addWidget(self.history_to_filter)
        filters.addWidget(self.history_outcome_filter)
        filters.addWidget(self.history_atr_filter)
        filters.addWidget(self.history_mode_filter)
        filters.addWidget(self.history_refresh_btn)
        filters.addWidget(self.history_export_btn)
        root.addLayout(filters)

        summary_box = QGroupBox("Completed trade summary")
        summary_grid = QGridLayout(summary_box)
        self.history_summary_cards: dict[str, MetricCard] = {}
        for idx, title in enumerate([
            "Total cycles",
            "Win rate",
            "Average net %",
            "Median net %",
            "Best net P/L",
            "Worst net P/L",
            "Total net P/L",
            "Total commissions",
            "Max losing streak",
            "Avg hold",
            "Max drawdown",
        ]):
            card = MetricCard(title)
            self.history_summary_cards[title] = card
            summary_grid.addWidget(card, idx // 3, idx % 3)
        root.addWidget(summary_box)

        self.history_table = QTableWidget(0, 28)
        self.history_table.setHorizontalHeaderLabels([
            "Outcome",
            "Ticker",
            "Cycle",
            "Buy time",
            "Sell time",
            "Qty",
            "Avg buy",
            "Avg sell",
            "Sell vs buy %",
            "Gross P/L",
            "Gross %",
            "Net P/L",
            "Net %",
            "Budget",
            "Reinvested",
            "Buy vs anchor %",
            "Initial stop vs buy %",
            "Min profit %",
            "Initial drop %",
            "BUY rebound/trail %",
            "SELL trailing-stop %",
            "Protective",
            "Protective SELL trailing-stop %",
            "Slippage",
            "Slippage %",
            "Buy order ref",
            "Sell order ref",
            "Updated",
        ])
        self.history_table.setSortingEnabled(True)
        _polish_table_widget(self.history_table, stretch_last=False, horizontal_scroll=Qt.ScrollBarAsNeeded, vertical_scroll=Qt.ScrollBarAsNeeded, expanding=True)
        self.history_table.setWordWrap(False)
        self.history_table.setMinimumHeight(320)
        root.addWidget(self.history_table, 1)
        self.history_refresh_btn.clicked.connect(lambda: self.controller.refresh_history(self.history_ticker_filter.text()))
        self.history_export_btn.clicked.connect(self._export_history)
        self.history_table.cellClicked.connect(self._history_row_clicked)
        self.history_ticker_filter.textChanged.connect(self._schedule_history_filter)
        self.history_from_filter.textChanged.connect(self._schedule_history_filter)
        self.history_to_filter.textChanged.connect(self._schedule_history_filter)
        self.history_outcome_filter.currentIndexChanged.connect(self._apply_history_filters)
        self.history_atr_filter.currentIndexChanged.connect(self._apply_history_filters)
        self.history_mode_filter.currentIndexChanged.connect(self._apply_history_filters)

    def _build_flowchart(self) -> None:
        root = QVBoxLayout(self.flowchart_tab)
        root.setContentsMargins(0, 0, 0, 0)
        self.flowchart_panel = FlowchartPanel()
        root.addWidget(self.flowchart_panel, 1)

    def _build_recovery(self) -> None:
        root = QVBoxLayout(self.recovery_tab)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.recovery_status_label = QLabel("No recovery issue detected.")
        self.recovery_status_label.setObjectName("PriceStatusGood")
        self.recovery_status_label.setWordWrap(True)
        root.addWidget(self.recovery_status_label)

        guidance_box = QGroupBox("Reconciliation screen: SQLite vs IBKR/TWS")
        guidance_layout = QVBoxLayout(guidance_box)
        guidance_layout.setSpacing(8)

        self.recovery_refresh_step_label = QLabel("1. Refresh current broker facts")
        self.recovery_refresh_step_label.setObjectName("RecoveryStepTitle")
        guidance_layout.addWidget(self.recovery_refresh_step_label)
        refresh_row = QHBoxLayout()
        self.recovery_refresh_broker_btn = QPushButton("Refresh from IBKR/TWS")
        self.recovery_refresh_broker_btn.setObjectName("RecoveryPrimaryButton")
        self.recovery_refresh_status_label = QLabel("Broker state: Not refreshed")
        self.recovery_refresh_status_label.setObjectName("RecoveryRefreshStatus")
        self.recovery_refresh_status_label.setProperty("state", "not_refreshed")
        self.recovery_refresh_status_label.setWordWrap(True)
        self.recovery_export_bundle_btn = QPushButton("Export audit bundle")
        refresh_row.addWidget(self.recovery_refresh_broker_btn, 0)
        refresh_row.addWidget(self.recovery_refresh_status_label, 1)
        refresh_row.addWidget(self.recovery_export_bundle_btn, 0)
        guidance_layout.addLayout(refresh_row)

        self.recovery_compare_step_label = QLabel("2. Compare SQLite with IBKR/TWS")
        self.recovery_compare_step_label.setObjectName("RecoveryStepTitle")
        guidance_layout.addWidget(self.recovery_compare_step_label)
        self.recovery_compare_table = QTableWidget(0, 4)
        self.recovery_compare_table.setHorizontalHeaderLabels([
            "Area",
            "SQLite / local state",
            "IBKR / TWS report",
            "Safest interpretation / action",
        ])
        self.recovery_compare_table.verticalHeader().setVisible(False)
        _polish_table_widget(self.recovery_compare_table, stretch_last=False, horizontal_scroll=Qt.ScrollBarAsNeeded, vertical_scroll=Qt.ScrollBarAsNeeded, expanding=True)
        # Keep the guided comparison readable without forcing the lower audit log
        # and advanced actions off-screen on laptop-height windows.
        self.recovery_compare_table.setMinimumHeight(150)
        self.recovery_compare_table.setMaximumHeight(260)
        guidance_layout.addWidget(self.recovery_compare_table, 0)

        self.recovery_recommendation_label = QLabel("Recommended action: Refresh from IBKR/TWS before resolving a recovery problem.")
        self.recovery_recommendation_label.setObjectName("RecoveryRecommendation")
        self.recovery_recommendation_label.setWordWrap(True)
        guidance_layout.addWidget(self.recovery_recommendation_label)

        self.recovery_button_hint_label = QLabel("Broker-dependent actions stay disabled until the refresh is successful, current, and matches the active local cycle. Only app-owned OrderRef data is used.")
        self.recovery_button_hint_label.setObjectName("Muted")
        self.recovery_button_hint_label.setWordWrap(True)
        guidance_layout.addWidget(self.recovery_button_hint_label)

        self.recovery_action_step_label = QLabel("3. Resolve the situation")
        self.recovery_action_step_label.setObjectName("RecoveryStepTitle")
        guidance_layout.addWidget(self.recovery_action_step_label)
        guided_buttons = QHBoxLayout()
        self.recovery_resume_btn = QPushButton("Reconcile and resume")
        self.recovery_stop_cycle_btn = QPushButton("Stop after current cycle")
        self.recovery_cancel_app_order_btn = QPushButton("Cancel visible app-owned orders")
        self.recovery_mark_manual_btn = QPushButton("Mark manually handled")
        self.recovery_resume_btn.setObjectName("RecoveryPrimaryButton")
        self.recovery_stop_cycle_btn.setObjectName("RecoveryCautionButton")
        self.recovery_cancel_app_order_btn.setObjectName("RecoveryDangerButton")
        self.recovery_mark_manual_btn.setObjectName("RecoveryDangerButton")
        for button in [
            self.recovery_resume_btn,
            self.recovery_stop_cycle_btn,
            self.recovery_cancel_app_order_btn,
            self.recovery_mark_manual_btn,
        ]:
            guided_buttons.addWidget(button)
        guidance_layout.addLayout(guided_buttons)
        root.addWidget(guidance_box, 0)

        # Lower recovery area: the audit/details log expands to fill all spare
        # space, while the advanced action box stays below it. Avoid large fixed
        # minimum heights here; otherwise Qt can clip/overlap the bottom action
        # box when the window is restored from maximized state or used on a
        # smaller laptop screen.
        recovery_lower_panel = QWidget()
        recovery_lower_layout = QVBoxLayout(recovery_lower_panel)
        recovery_lower_layout.setContentsMargins(0, 0, 0, 0)
        recovery_lower_layout.setSpacing(8)
        recovery_lower_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.recovery_details = QTextEdit()
        self.recovery_details.setReadOnly(True)
        self.recovery_details.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.recovery_details.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.recovery_details.setMinimumHeight(180)
        self.recovery_details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        recovery_lower_layout.addWidget(self.recovery_details, 1)

        advanced_box = QGroupBox("Advanced stop strategy actions")
        advanced_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        advanced_box.setMinimumHeight(104)
        advanced_box.setMaximumHeight(132)
        advanced_layout = QVBoxLayout(advanced_box)
        advanced_hint = QLabel("Use these only after comparing SQLite and TWS. They call the existing stop strategy controls and do not change strategy math.")
        advanced_hint.setObjectName("Muted")
        advanced_hint.setWordWrap(True)
        advanced_layout.addWidget(advanced_hint)
        buttons = QHBoxLayout()
        self.recovery_sell_market_btn = QPushButton("Sell app-bought unsold position")
        self.recovery_leave_orders_btn = QPushButton("Leave orders working")
        self.recovery_sell_market_btn.setToolTip("Submits a SELL market order for the unsold app-bought quantity after app-owned SELL orders are no longer working.")
        buttons.addWidget(self.recovery_sell_market_btn)
        buttons.addWidget(self.recovery_leave_orders_btn)
        advanced_layout.addLayout(buttons)
        recovery_lower_layout.addWidget(advanced_box, 0)
        root.addWidget(recovery_lower_panel, 1)

        self.recovery_sell_market_btn.clicked.connect(self._recovery_sell_market_clicked)
        self.recovery_leave_orders_btn.clicked.connect(self._recovery_leave_orders_clicked)
        self.recovery_resume_btn.clicked.connect(self._recovery_resume_clicked)
        self.recovery_stop_cycle_btn.clicked.connect(lambda: self.controller.request_stop(StopAction.STOP_AFTER_CURRENT_CYCLE))
        self.recovery_cancel_app_order_btn.clicked.connect(self._recovery_cancel_app_order_clicked)
        self.recovery_mark_manual_btn.clicked.connect(self._recovery_mark_manual_clicked)
        self.recovery_refresh_broker_btn.clicked.connect(self._recovery_refresh_broker_clicked)
        self.recovery_export_bundle_btn.clicked.connect(self._recovery_export_bundle_clicked)

    def _recovery_export_bundle_clicked(self) -> None:
        method = getattr(self.controller, "export_audit_bundle", None)
        if not callable(method):
            QMessageBox.warning(self, "Audit export", "This build does not expose an audit export command.")
            return
        try:
            path = method()
        except Exception as exc:
            QMessageBox.warning(self, "Audit export", f"Could not create the audit export bundle:\n{exc}")
            return
        QMessageBox.information(self, "Audit export", f"Audit export bundle created:\n{path}")

    def _recovery_refresh_is_current_or_warn(self, action_label: str) -> bool:
        refresh = _recovery_refresh_status(self.current_snapshot or {})
        if bool(refresh.get("is_current")):
            return True
        reason = str(refresh.get("reason") or "The broker refresh is not current.")
        QMessageBox.warning(
            self,
            "Refresh from IBKR/TWS required",
            f"Refresh from IBKR/TWS before {action_label}.\n\nCurrent refresh status: {reason}",
        )
        return False

    def _recovery_resume_clicked(self) -> None:
        if not self._recovery_refresh_is_current_or_warn("reconciling and resuming"):
            return
        method = getattr(self.controller, "resume_recovery_monitoring", None)
        if callable(method):
            method()
            return
        QMessageBox.warning(self, "Recovery", "This build does not expose a recovery resume command.")

    def _recovery_refresh_broker_clicked(self) -> None:
        method = getattr(self.controller, "refresh_broker_state", None)
        if callable(method):
            method()
            return
        QMessageBox.warning(self, "Recovery", "This build does not expose a broker-state refresh command.")

    def _recovery_sell_market_clicked(self) -> None:
        if not self._recovery_refresh_is_current_or_warn("submitting a market SELL"):
            return
        self.controller.request_stop(StopAction.SELL_APP_POSITION_MARKET)

    def _recovery_leave_orders_clicked(self) -> None:
        if not self._recovery_refresh_is_current_or_warn("leaving app-owned orders working"):
            return
        self.controller.request_stop(StopAction.LEAVE_ORDERS_WORKING)

    def _recovery_cancel_app_order_clicked(self) -> None:
        if not self._recovery_refresh_is_current_or_warn("cancelling app-owned orders"):
            return
        open_orders = self._visible_tws_open_app_orders()
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        if not cycle and not open_orders:
            QMessageBox.information(self, "Recovery", "No app-owned open order is visible to cancel.")
            return
        if open_orders:
            visible = "\n".join(
                f"- {order.get('order_ref') or '-'} / id {order.get('order_id') or '-'} / status {order.get('status') or '-'}"
                for order in open_orders[:8]
            )
        else:
            visible = "The active SQLite cycle contains a working app-owned order reference."
        message = (
            "Cancel the app-owned order(s) shown in Reconciliation?\n\n"
            f"{visible}\n\n"
            "The app will only use app-owned OrderRef/order IDs. It will not cancel manual TWS orders."
        )
        choice = QMessageBox.question(
            self,
            "Cancel visible app-owned orders",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return
        method = getattr(self.controller, "cancel_recovery_app_order", None)
        if callable(method):
            method()
            return
        self.controller.request_stop(StopAction.CANCEL_OPEN_BOT_ORDERS)

    def _recovery_mark_manual_clicked(self) -> None:
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        if not cycle:
            QMessageBox.information(self, "Recovery", "No active SQLite cycle is visible to mark manually handled.")
            return
        ticker = cycle.get("ticker") or "the active ticker"
        stage = cycle.get("stage") or "unknown stage"
        refresh = _recovery_refresh_status(self.current_snapshot or {})
        message = (
            f"Mark {ticker} / {stage} as manually handled in SQLite?\n\n"
            "This does not submit or cancel any broker order. Use it only after you have reconciled TWS orders, executions, and position outside the app."
        )
        operator_note = "Confirmed in the Reconciliation screen after operator TWS reconciliation."
        if not bool(refresh.get("is_current")):
            message += (
                "\n\nWARNING: The app does not have a current successful broker refresh. "
                f"Status: {refresh.get('reason') or 'unknown'}. Continuing is a manual override and confirms that you independently verified TWS."
            )
            operator_note = "Manual override confirmed without a current app broker refresh after independent TWS verification."
        choice = QMessageBox.question(
            self,
            "Mark manually handled",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return
        method = getattr(self.controller, "mark_recovery_manually_handled", None)
        if callable(method):
            method(operator_note)
            return
        QMessageBox.warning(self, "Recovery", "This build does not expose a mark-manually-handled command.")

    def _pct_spin(self, value: float, *, allow_zero: bool = False) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0 if allow_zero else 0.01, 99.99)
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setValue(value)
        spin.setSuffix(" %")
        return spin

    def _mult_spin(self, value: float, *, allow_zero: bool = False) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0 if allow_zero else 0.01, 50.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.05)
        spin.setValue(value)
        spin.setSuffix(" × ATR")
        return spin

    def _money_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.01, 1_000_000_000.0)
        spin.setDecimals(2)
        spin.setSingleStep(100.0)
        spin.setValue(value)
        spin.setPrefix("$ ")
        return spin

    def _make_button_bold(self, button: QPushButton) -> None:
        font = button.font()
        font.setBold(True)
        button.setFont(font)


    def _info_badge(self) -> QLabel:
        # Invisible compatibility target for helper paths that still request an
        # info badge. Current tooltips are attached directly to editable controls
        # rather than shown through visible question-mark badges.
        label = QLabel("")
        label.setVisible(False)
        label.setFixedWidth(0)
        label.setMinimumHeight(0)
        return label

    def _pct_field(self, spin: QWidget, info: QLabel) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(spin, 1)
        zero_text = str(spin.property("zeroDisabledText") or "")
        if zero_text:
            zero_label = QLabel(zero_text)
            zero_label.setObjectName("ZeroDisabledLabel")
            zero_label.setProperty("disabledAtZero", True)
            zero_label.setVisible(False)
            zero_label.setToolTip(zero_text)
            zero_label.setMinimumWidth(86)
            self._zero_disabled_fields.append((spin, zero_label, zero_text))
            layout.addWidget(zero_label, 0)
        key = str(spin.property("changeFieldKey") or "")
        if key:
            badge = QLabel("Idle")
            badge.setObjectName("ApplicabilityBadge")
            badge.setProperty("state", "idle")
            badge.setAlignment(Qt.AlignCenter)
            badge.setMinimumWidth(116)
            badge.setToolTip("When a cycle is running, this shows whether edits affect the current cycle, the next broker order, the next cycle only, or nothing in the current stage.")
            self._field_change_badges[key] = badge
            layout.addWidget(badge, 0)
        return wrapper

    def _mark_zero_disabled(self, widget: QWidget, text: str = "0 = disabled") -> None:
        widget.setProperty("zeroDisabledText", text)
        widget.setToolTip((widget.toolTip() + "\n" if widget.toolTip() else "") + text)

    def _numeric_zero_disabled_value(self, widget: QWidget) -> Optional[float]:
        try:
            if isinstance(widget, QDoubleSpinBox):
                return float(widget.value())
            if isinstance(widget, QSpinBox):
                return float(widget.value())
        except Exception:
            return None
        return None

    def _update_zero_disabled_indicators(self) -> None:
        disabled_labels: list[str] = []
        field_names = {
            id(self.buy_rebound_spin) if hasattr(self, "buy_rebound_spin") else -1: "BUY trailing-stop",
            id(self.sell_trail_spin) if hasattr(self, "sell_trail_spin") else -1: "SELL trailing-stop",
            id(self.max_daily_loss_ticker_spin) if hasattr(self, "max_daily_loss_ticker_spin") else -1: "max ticker loss/day",
            id(self.max_daily_loss_total_spin) if hasattr(self, "max_daily_loss_total_spin") else -1: "max total loss/day",
            id(self.max_cycles_ticker_day_spin) if hasattr(self, "max_cycles_ticker_day_spin") else -1: "max cycles",
            id(self.max_consecutive_losses_spin) if hasattr(self, "max_consecutive_losses_spin") else -1: "max consecutive losses",
            id(self.max_spread_pct_spin) if hasattr(self, "max_spread_pct_spin") else -1: "max spread %",
            id(self.min_trade_price_spin) if hasattr(self, "min_trade_price_spin") else -1: "min trade price",
            id(self.max_gap_pct_spin) if hasattr(self, "max_gap_pct_spin") else -1: "max gap from close %",
            id(self.no_new_buy_first_spin) if hasattr(self, "no_new_buy_first_spin") else -1: "no new BUY first",
            id(self.no_new_buy_last_spin) if hasattr(self, "no_new_buy_last_spin") else -1: "no new BUY last",
            id(self.cancel_buy_before_close_spin) if hasattr(self, "cancel_buy_before_close_spin") else -1: "cancel BUY before close",
        }
        for widget, label, text in getattr(self, "_zero_disabled_fields", []):
            value = self._numeric_zero_disabled_value(widget)
            disabled = value is not None and abs(value) < 1e-9
            label.setVisible(disabled)
            label.setText(text)
            if disabled:
                name = field_names.get(id(widget))
                if name:
                    disabled_labels.append(name)
        if hasattr(self, "risk_zero_disabled_summary"):
            risk_disabled = [
                name for name in disabled_labels
                if name not in {"BUY trailing-stop", "SELL trailing-stop"}
            ]
            if risk_disabled:
                self.risk_zero_disabled_summary.setText("Disabled by value 0: " + ", ".join(risk_disabled) + ".")
            else:
                self.risk_zero_disabled_summary.setText("Values set to 0 are disabled for fields that support a zero-off setting.")

    def _register_change_tracked_inputs(self, fields: list[tuple[str, str, QWidget, str]]) -> None:
        for key, label, widget, category in fields:
            clean_key = str(key)
            widget.setProperty("changeFieldKey", clean_key)
            widget.setProperty("changeFieldCategory", str(category))
            widget.setProperty("changeFieldLabel", str(label))
            self._field_change_widgets[clean_key] = widget
            self._change_widget_keys[id(widget)] = clean_key
            self._change_field_labels[clean_key] = str(label)

    def _field_value_for_change_tracking(self, widget: QWidget) -> Any:
        if isinstance(widget, QCheckBox):
            return bool(widget.isChecked())
        if isinstance(widget, QComboBox):
            return widget.currentData() if widget.currentData() is not None else widget.currentText()
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        if isinstance(widget, QDoubleSpinBox):
            return round(float(widget.value()), 8)
        if isinstance(widget, QSpinBox):
            return int(widget.value())
        try:
            return str(widget.property("value") or "")
        except Exception:
            return ""

    def _current_change_field_values(self) -> dict[str, Any]:
        return {key: self._field_value_for_change_tracking(widget) for key, widget in self._field_change_widgets.items()}

    def _mark_sender_changed_if_running(self) -> None:
        if self._applying_snapshot_to_inputs:
            return
        sender = self.sender()
        if sender is None:
            return
        key = self._change_widget_keys.get(id(sender))
        if not key:
            return
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        if not self._is_active_stage(cycle.get("stage")):
            return
        if not self._running_change_baseline:
            self._running_change_baseline = self._current_change_field_values()
        current_value = self._field_value_for_change_tracking(self._field_change_widgets[key])
        baseline_value = self._running_change_baseline.get(key)
        if current_value == baseline_value:
            self._changed_while_running_fields.discard(key)
        else:
            self._changed_while_running_fields.add(key)

    @staticmethod
    def _change_badge_style_token(label: str) -> str:
        if label == "Current cycle":
            return "current"
        if label == "Next order":
            return "next_order"
        if label == "Next cycle only":
            return "next_cycle"
        if label == "Not applicable now":
            return "not_applicable"
        return "idle"

    def _field_applicability(self, key: str, stage: Optional[str]) -> tuple[str, str]:
        category = str(self._field_change_widgets.get(key).property("changeFieldCategory") if key in self._field_change_widgets else "")
        if not self._is_active_stage(stage):
            return "Idle", "No active cycle. The value will be used when the next cycle starts."
        if category == "contract":
            return "Next cycle only", "Ticker, conId, exchange, and contract identity are fixed for the active cycle and recovery matching."
        if stage == Stage.WAIT_INITIAL_DROP.value:
            if category in {"entry", "atr_entry", "atr_exit", "atr_general", "risk", "exit", "protective", "slippage"}:
                return "Current cycle", "No native order has been submitted yet; this edit is still eligible for the active waiting cycle."
            if category == "automation":
                return "Current cycle", "Automation settings are read when this cycle completes and before the next cycle is started."
        if stage == Stage.BUY_TRAIL_ACTIVE.value:
            if category in {"exit", "protective", "slippage", "atr_exit", "atr_general"}:
                return "Next order", "The BUY order is already working; this edit can only affect the later protective/final SELL decision in the current cycle."
            if category == "automation":
                return "Current cycle", "Automation settings are read when this active cycle completes."
            return "Next cycle only", "The BUY order has already been submitted, so entry sizing, ATR entry values, and entry guards cannot change that broker order."
        if stage == Stage.WAIT_RISE_TRIGGER.value:
            if category in {"exit", "slippage", "atr_exit", "atr_general"}:
                return "Next order", "The BUY fill is recorded; this edit can only affect the final SELL order decision before that order is submitted."
            if category == "protective":
                return "Not applicable now", "The protective SELL decision has already passed for this cycle; edit affects a future cycle only."
            if category == "automation":
                return "Current cycle", "Automation settings are read when this active cycle completes."
            return "Next cycle only", "Entry sizing, ATR entry values, and entry guards are no longer relevant after the BUY fill."
        if stage == Stage.SELL_TRAIL_ACTIVE.value:
            if category == "automation":
                return "Current cycle", "Auto-repeat can still affect what happens after this SELL order completes."
            if category in {"exit", "slippage", "atr_exit", "atr_general"}:
                return "Not applicable now", "The final SELL order is already working in IBKR/TWS; changing this value cannot alter that native order."
            return "Next cycle only", "The active cycle is already in its final broker-order stage."
        return "Not applicable now", "The active stage is paused, stopped, or waiting for recovery; edits are not applied to broker state."

    def _label_with_info(self, text: str, info: QLabel) -> QWidget:
        label = QLabel(text)
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(label)
        layout.addStretch(1)
        return wrapper

    def _set_pct_tooltip(self, spin: QWidget, info: QLabel, text: str) -> None:
        if spin.toolTip() != text:
            spin.setToolTip(text)
        if info.toolTip() != text:
            info.setToolTip(text)

    def _update_percentage_tooltips(self) -> None:
        if not hasattr(self, "initial_drop_info"):
            return
        initial_drop = float(self.initial_drop_spin.value())
        buy_rebound = float(self.buy_rebound_spin.value())
        minimum_profit = float(self.rise_trigger_spin.value())
        sell = float(self.sell_trail_spin.value())
        levels = projected_minimum_profit_levels(initial_drop, buy_rebound, minimum_profit, sell, anchor=100.0)

        self._set_pct_tooltip(
            self.initial_drop_spin,
            self.initial_drop_info,
            "Initial drop %.\n"
            f"Current value: {initial_drop:.2f}%.\n"
            "Stage 1: before any position is opened, the app waits until last price drops from the current anchor.\n"
            "If last price rises before the drop is reached, the anchor resets upward.\n"
            f"Example with anchor $100.00: drop trigger = {_format_currency(levels['drop_trigger'])}.\n"
            "No stock is owned during this drop stage.",
        )
        self._set_pct_tooltip(
            self.buy_rebound_spin,
            self.buy_rebound_info,
            "BUY rebound/trail %.\n"
            f"Current value: {buy_rebound:.2f}%.\n"
            "Stage 2: after the initial drop, a value above 0 places a native TWS BUY trailing-stop order.\n"
            "The stop trails downward while price keeps falling. A rebound by this percentage triggers a market buy.\n"
            "Set this field to 0.00% to disable BUY trailing-stop behavior and submit a BUY market order immediately when the initial-drop condition is met.\n"
            f"Example with anchor $100.00: projected BUY trigger/reference = {_format_currency(levels['projected_buy_stop'])}.",
        )
        self._set_pct_tooltip(
            self.rise_trigger_spin,
            self.rise_trigger_info,
            "Minimum profit %.\n"
            f"Current value: {minimum_profit:.2f}%.\n"
            "Stage 3: after the buy fill, the app waits until it can place the SELL trailing-stop with the first stop price already protecting this profit.\n"
            "Profit is measured against the actual average buy fill. The cycle-anchor comparison is shown only as context.\n"
            f"Example with anchor $100.00: projected buy reference = {_format_currency(levels['protected_reference'])}; "
            f"minimum initial SELL stop = {_format_currency(levels['minimum_sell_stop'])}; "
            f"last price needed before placing SELL trailing-stop = {_format_currency(levels['required_last_price'])}.\n"
            "This is before commissions, fees, gaps, and market-order slippage.",
        )
        self._set_pct_tooltip(
            self.sell_trail_spin,
            self.sell_trail_info,
            "SELL trailing-stop %.\n"
            f"Current value: {sell:.2f}%.\n"
            "Stage 4: after the minimum-profit trigger is reached, a value above 0 places a native TWS SELL trailing-stop order.\n"
            "A wider SELL trailing-stop does not reduce the configured minimum profit; it raises the last price required before the SELL trailing-stop can be placed.\n"
            "Set this field to 0.00% to disable SELL trailing-stop behavior and submit a SELL market order as soon as the minimum-profit condition is met.\n"
            f"Example with anchor $100.00: last price must reach {_format_currency(levels['required_last_price'])} so the first SELL stop/reference is at least {_format_currency(levels['minimum_sell_stop'])}.\n"
            f"Projected protection: {levels['profit_vs_projected_buy_pct']:.2f}% vs projected buy, {levels['profit_vs_anchor_pct']:.2f}% vs anchor.",
        )

        self._update_additional_strategy_tooltips()

    def _update_additional_strategy_tooltips(self) -> None:
        if not hasattr(self, "ticker_info"):
            return
        amount = float(self.investment_spin.value()) if hasattr(self, "investment_spin") else 0.0
        risk_defaults = suggested_hard_risk_defaults(amount, **self._risk_default_market_kwargs())
        zero_text = "Enter 0 to disable only this individual limit."
        tooltips = [
            (self.ticker_edit, self.ticker_info, "Ticker symbol used for IBKR contract search and SMART-routed stock orders. Press Enter here to search for ticker matches."),
            (self.primary_exchange_edit, self.primary_exchange_info, "Optional primary exchange for contract identification, for example NASDAQ. Routing remains SMART."),
            (self.con_id_edit, self.con_id_info, "Read-only IBKR contract ID from the selected API match. Blank means the app qualifies by ticker/exchange/currency."),
            (self.investment_spin, self.investment_info, f"Base amount for each BUY cycle. Current default-based hard-risk suggestions use ${amount:,.2f} as the reference."),
            (self.ticker_matches_combo, self.ticker_selector_info, "API search result selector. Select the stock contract you want, then click '2. Use selected match'."),
            (self.atr_adaptive_check, self.atr_adaptive_info, "Default ON. The app collects RTH-only subscribed API price observations and builds rolling ATR bars whether adaptation is enabled or disabled. When enabled, calculated ATR values can update Initial drop %, BUY rebound/trail %, Minimum profit % if enabled below, and SELL trailing-stop %. Outside RTH, collection and adaptive updates pause. The in-memory ATR history is not retained across app restarts."),
            (
                self.atr_block_until_ready_check,
                self.atr_block_until_ready_info,
                "Default ON. While ATR lacks enough RTH-only app-observed bars, Stage 1 has no initial-drop trigger. "
                "The readiness tick starts a fresh anchor, so a decline observed during warmup cannot trigger an immediate BUY.",
            ),
            (self.atr_min_profit_adaptive_check, self.atr_min_profit_adaptive_info, "When enabled, ATR also updates Minimum profit %. When disabled, you can manually set Minimum profit % while still using ATR for Initial drop %, BUY rebound/trail %, and SELL trailing-stop %."),
            (self.atr_protective_sell_adaptive_check, self.atr_protective_sell_adaptive_info, "Optional. When enabled, Protective SELL trailing-stop % is also generated from ATR% times the Protective SELL multiplier below. This only changes the app's configured protective trail value before the protective order is submitted."),
            (self.atr_period_spin, self.atr_period_info, f"Number of completed app-observed bars used for ATR. Current value {self.atr_period_spin.value()} bars."),
            (self.atr_bar_seconds_spin, self.atr_bar_seconds_info, f"Seconds per app-observed ATR bar. Current value {self.atr_bar_seconds_spin.value()} seconds. Shorter bars react faster but can be noisier."),
            (self.atr_initial_drop_mult_spin, self.atr_initial_drop_mult_info, f"Adaptive Initial drop % = ATR% × this multiplier. Current multiplier {self.atr_initial_drop_mult_spin.value():.2f}."),
            (self.atr_buy_rebound_mult_spin, self.atr_buy_rebound_mult_info, f"Adaptive BUY rebound/trail % = ATR% × this multiplier. Set multiplier to 0 to write 0.00% and disable BUY trailing-stop behavior. Current multiplier {self.atr_buy_rebound_mult_spin.value():.2f}."),
            (self.atr_min_profit_mult_spin, self.atr_min_profit_mult_info, f"Adaptive Minimum profit % = ATR% × this multiplier. Current multiplier {self.atr_min_profit_mult_spin.value():.2f}."),
            (self.atr_sell_trail_mult_spin, self.atr_sell_trail_mult_info, f"Adaptive SELL trailing-stop % = ATR% × this multiplier. Set multiplier to 0 to write 0.00% and disable SELL trailing-stop behavior. Current multiplier {self.atr_sell_trail_mult_spin.value():.2f}."),
            (self.atr_protective_sell_mult_spin, self.atr_protective_sell_mult_info, f"Optional Protective SELL trailing-stop % = ATR% × this multiplier when the protective ATR checkbox is enabled. Current multiplier {self.atr_protective_sell_mult_spin.value():.2f}."),
            (self.atr_min_pct_spin, self.atr_min_pct_info, f"Lower clamp for ATR-generated percentages. Current minimum {self.atr_min_pct_spin.value():.2f}%."),
            (self.atr_max_pct_spin, self.atr_max_pct_info, f"Upper clamp for ATR-generated percentages. Current maximum {self.atr_max_pct_spin.value():.2f}%."),
            (self.reinvest_check, self.reinvest_info, "When enabled, positive realized net profit from this ticker is added to the next cycle budget."),
            (self.auto_repeat_check, self.auto_repeat_info, "When enabled, the bot starts the next cycle automatically after a completed exit."),
            (self.protective_sell_check, self.protective_sell_info, "Enable protective SELL. When this is on, the bot places a native SELL trailing-stop immediately after any BUY fill, before waiting for the minimum-profit exit. It reduces the time a filled position is unprotected. When the minimum-profit condition is reached, the bot cancels this protective order first and then places the final profit-protecting SELL trailing-stop. This is optional because it can sell at a loss if the stock falls before the minimum-profit condition is reached."),
            (self.protective_sell_trail_spin, self.protective_sell_trail_info, f"Protective SELL trailing-stop percentage. Current value {self.protective_sell_trail_spin.value():.2f}%. A smaller value protects more tightly but can exit earlier; a wider value gives the stock more room but increases the possible drawdown before the protective order triggers."),
            (self.slippage_buffer_check, self.slippage_buffer_info, "Use slippage buffer. When enabled, the bot plans as if the BUY fill could be worse than the displayed BUY stop and as if the SELL fill could be worse than the displayed SELL stop. It reduces BUY quantity and raises the minimum-profit SELL activation level. It does not change the IBKR order type and cannot guarantee the final execution price."),
            (self.slippage_buffer_spin, self.slippage_buffer_pct_info, f"Slippage buffer percentage. Current value {self.slippage_buffer_spin.value():.2f}%. BUY sizing uses projected BUY stop multiplied by this buffer. Minimum-profit SELL activation is also raised so the planned profit still has room for a worse market fill. This is a planning guard, not a broker guarantee."),
            (self.hard_risk_limits_check, self.hard_risk_limits_info, "Optional master switch for app-side hard risk limits. Individual numeric limits with value 0 are disabled."),
            (self.max_daily_loss_ticker_spin, self.max_daily_loss_ticker_info, f"Blocks new BUY entries for this ticker after daily app net P/L reaches this loss. Suggested from amount: ${risk_defaults['max_daily_loss_ticker']:,.2f}. {zero_text}"),
            (self.max_daily_loss_total_spin, self.max_daily_loss_total_info, f"Blocks new BUY entries after total daily app net P/L reaches this loss. Suggested from amount: ${risk_defaults['max_daily_loss_total']:,.2f}. {zero_text}"),
            (self.max_cycles_ticker_day_spin, self.max_cycles_info, f"Maximum completed cycles allowed for this ticker in total. Suggested: {risk_defaults['max_cycles_per_ticker_day']}. {zero_text}"),
            (self.max_consecutive_losses_spin, self.max_consecutive_info, f"Blocks new BUY entries after this many consecutive losing completed cycles. Suggested: {risk_defaults['max_consecutive_losses']}. {zero_text}"),
            (self.max_spread_pct_spin, self.max_spread_info, f"Maximum bid/ask spread percentage before a new BUY can be sent. This field is user-controlled and is never rewritten from live bid/ask data. Current value: {self.max_spread_pct_spin.value():.2f}%. {zero_text}"),
            (self.min_trade_price_spin, self.min_trade_price_info, f"Minimum permitted sizing price for a new BUY. Suggested: ${risk_defaults['min_trade_price']:,.2f}. {zero_text}"),
            (self.max_gap_pct_spin, self.max_gap_info, f"Maximum absolute gap from previous close before a new BUY can be sent. Default 0 disables this guard. Suggested: {risk_defaults['max_gap_from_prev_close_pct']:.2f}%. {zero_text}"),
            (self.block_delayed_live_check, self.block_delayed_live_info, "Default ON. Blocks live-profile BUY orders if the API feed is delayed, frozen, or delayed-frozen."),
            (self.what_if_check, self.what_if_info, "Default ON. Runs an IBKR what-if margin/buying-power pre-check before the real BUY order is submitted."),
            (self.stale_data_guard_check, self.stale_data_info, "Default ON. Blocks new BUY entries when selected price, bid/ask, or RTH status is stale."),
            (self.max_price_age_spin, self.max_price_age_info, f"Maximum selected-price age when stale-data guard is enabled. Current value {self.max_price_age_spin.value():.1f}s."),
            (self.max_bidask_age_spin, self.max_bidask_age_info, f"Maximum bid/ask age when stale-data guard is enabled. Current value {self.max_bidask_age_spin.value():.1f}s."),
            (self.max_rth_age_spin, self.max_rth_age_info, f"Maximum RTH status age when stale-data guard is enabled. Current value {self.max_rth_age_spin.value():.1f}s."),
            (self.volatility_filter_check, self.volatility_filter_info, "Default OFF. Check to block new BUY entries when app-observed recent price movement exceeds the configured maximum."),
            (self.volatility_window_spin, self.volatility_window_info, f"Recent price observation window. Current value {self.volatility_window_spin.value()} seconds."),
            (self.max_recent_move_spin, self.max_recent_move_info, f"Maximum recent observed move. Current value {self.max_recent_move_spin.value():.2f}%."),
            (self.session_timing_guard_check, self.session_timing_info, "Default ON. Blocks new BUY entries near market open/close and optionally cancels active BUY trailing-stop orders before close."),
            (self.no_new_buy_first_spin, self.no_new_buy_first_info, f"No new BUY entries during the first {self.no_new_buy_first_spin.value()} minutes of RTH when enabled."),
            (self.no_new_buy_last_spin, self.no_new_buy_last_info, f"No new BUY entries during the last {self.no_new_buy_last_spin.value()} minutes of RTH when enabled."),
            (self.cancel_buy_before_close_spin, self.cancel_buy_before_close_info, f"Cancel unfilled app BUY trailing-stop order {self.cancel_buy_before_close_spin.value()} minutes before RTH close when enabled."),
        ]
        for widget, info, text in tooltips:
            self._set_pct_tooltip(widget, info, text)

    def _risk_default_market_kwargs(self) -> dict[str, Any]:
        """Build market inputs used by non-spread safety suggestions.

        The helper is deliberately tolerant: missing or malformed fields simply
        produce amount-only defaults. Price and previous close can inform the
        broker-timing suggestions. Live bid/ask data is deliberately excluded:
        Max spread % is controlled only by saved or direct user input.
        """
        snapshot = (getattr(self, "current_snapshot", {}) or {}).get("price_snapshot") or {}
        fields = snapshot.get("fields") or {}

        def pick(*names: str) -> Any:
            for name in names:
                value = fields.get(name)
                if value is not None:
                    return value
            return None

        return {
            "market_price": snapshot.get("price"),
            "previous_close": pick("close", "delayedClose"),
        }

    def _apply_suggested_broker_timing_defaults_from_amount(self, *args: Any, force: bool = False) -> None:
        if not hasattr(self, "investment_spin") or not hasattr(self, "max_price_age_spin"):
            return
        kwargs = self._risk_default_market_kwargs()
        suggestions = suggested_broker_timing_defaults(
            float(self.investment_spin.value()),
            market_price=kwargs.get("market_price"),
            previous_close=kwargs.get("previous_close"),
        )
        previous = getattr(self, "_last_suggested_safety_defaults", {}) or {}

        def maybe_set(widget: Any, key: str) -> None:
            current = widget.value()
            old = previous.get(key)
            if force or old is None or abs(float(current) - float(old)) < 1e-9:
                widget.blockSignals(True)
                widget.setValue(suggestions[key])
                widget.blockSignals(False)

        maybe_set(self.max_price_age_spin, "max_selected_price_age_seconds")
        maybe_set(self.max_bidask_age_spin, "max_bid_ask_age_seconds")
        maybe_set(self.max_rth_age_spin, "max_rth_status_age_seconds")
        maybe_set(self.volatility_window_spin, "volatility_window_seconds")
        maybe_set(self.max_recent_move_spin, "max_recent_price_move_pct")
        maybe_set(self.no_new_buy_first_spin, "no_new_buy_first_minutes")
        maybe_set(self.no_new_buy_last_spin, "no_new_buy_last_minutes")
        maybe_set(self.cancel_buy_before_close_spin, "cancel_buy_before_close_minutes")
        self._last_suggested_safety_defaults = dict(suggestions)
        self._update_additional_strategy_tooltips()

    def _apply_suggested_risk_limits_from_amount(self, *args: Any, force: bool = False) -> None:
        if not hasattr(self, "investment_spin") or not hasattr(self, "max_daily_loss_ticker_spin"):
            return
        suggestions = suggested_hard_risk_defaults(float(self.investment_spin.value()), **self._risk_default_market_kwargs())
        previous = getattr(self, "_last_suggested_risk_limits", {}) or {}

        def maybe_set(widget: Any, key: str) -> None:
            current = widget.value()
            old = previous.get(key)
            if force or old is None or abs(float(current) - float(old)) < 1e-9:
                widget.blockSignals(True)
                widget.setValue(suggestions[key])
                widget.blockSignals(False)

        maybe_set(self.max_daily_loss_ticker_spin, "max_daily_loss_ticker")
        maybe_set(self.max_daily_loss_total_spin, "max_daily_loss_total")
        maybe_set(self.max_cycles_ticker_day_spin, "max_cycles_per_ticker_day")
        maybe_set(self.max_consecutive_losses_spin, "max_consecutive_losses")
        maybe_set(self.min_trade_price_spin, "min_trade_price")
        maybe_set(self.max_gap_pct_spin, "max_gap_from_prev_close_pct")
        self._last_suggested_risk_limits = dict(suggestions)
        self._update_additional_strategy_tooltips()
        self._update_zero_disabled_indicators()

    def _selected_profile_data(self) -> dict[str, Any]:
        if not hasattr(self, "profile_combo"):
            return normalize_profile_dict({"key": "gateway_live"})
        data = self.profile_combo.currentData() or {}
        if isinstance(data, dict) and data.get("key") != "custom":
            return normalize_profile_dict(data)
        return normalize_profile_dict({
            "key": "custom",
            "platform": self._selected_platform(),
            "trading_mode": self._manual_trading_mode,
            "host": self.host_edit.text().strip() if hasattr(self, "host_edit") else "127.0.0.1",
            "port": self.port_spin.value() if hasattr(self, "port_spin") else 4001,
        })

    def _selected_platform(self) -> str:
        data = self.platform_combo.currentData() if hasattr(self, "platform_combo") else GATEWAY_PLATFORM
        value = str(data or GATEWAY_PLATFORM).lower()
        return value if value in {TWS_PLATFORM, GATEWAY_PLATFORM} else GATEWAY_PLATFORM

    def _selected_trading_mode(self) -> str:
        data = self.profile_combo.currentData() if hasattr(self, "profile_combo") else None
        if isinstance(data, dict) and data.get("key") != "custom":
            mode = str(normalize_profile_dict(data).get("trading_mode") or "live").lower()
        else:
            mode = str(getattr(self, "_manual_trading_mode", "live") or "live").lower()
        return "paper" if mode == "paper" else "live"

    def _update_connection_buttons(self) -> None:
        if not hasattr(self, "connect_btn"):
            return
        profile = self._selected_profile_data()
        platform = str(profile.get("platform") or self._selected_platform())
        mode = str(profile.get("trading_mode") or self._selected_trading_mode())
        host = profile.get("host") or (self.host_edit.text().strip() if hasattr(self, "host_edit") else "-")
        port = profile.get("port") or (self.port_spin.value() if hasattr(self, "port_spin") else "-")
        label = platform_label(platform)
        short_label = "IB Gateway" if platform == GATEWAY_PLATFORM else "TWS"
        self.connect_btn.setText(f"1. Connect to {short_label} API")
        self.start_platform_btn.setText(f"Launch {short_label}")
        self.platform_path_edit.setPlaceholderText(f"Optional path to {short_label}")
        self.connection_hint_label.setText(
            f"Selected: {label} {mode.upper()} at {host}:{port}. Log in manually and complete 2FA. "
            "The bot connects to the API socket only after the platform is running and logged in. "
            "Account is optional; blank leaves account selection to IBKR."
        )
        if hasattr(self, "connection_risk_label"):
            risk = "LIVE ORDERS" if mode == "live" else "Paper trading"
            self.connection_risk_label.setText(f"Profile mode: {mode} ({risk})")

    def _set_custom_profile_if_needed(self) -> None:
        if not hasattr(self, "profile_combo") or self._applying_snapshot_to_inputs:
            return
        key = profile_key_for(self._selected_platform(), self._selected_trading_mode(), self.host_edit.text().strip(), self.port_spin.value())
        for i in range(self.profile_combo.count()):
            data = self.profile_combo.itemData(i) or {}
            if isinstance(data, dict) and data.get("key") == key:
                if self.profile_combo.currentIndex() != i:
                    self.profile_combo.blockSignals(True)
                    self.profile_combo.setCurrentIndex(i)
                    self.profile_combo.blockSignals(False)
                self._manual_trading_mode = self._selected_trading_mode()
                self._update_connection_buttons()
                return
        # Host/port no longer match a built-in profile. Keep the previous paper/live
        # mode from the last selected profile and switch visually to Custom.
        for i in range(self.profile_combo.count()):
            data = self.profile_combo.itemData(i) or {}
            if isinstance(data, dict) and data.get("key") == "custom":
                if self.profile_combo.currentIndex() != i:
                    self.profile_combo.blockSignals(True)
                    self.profile_combo.setCurrentIndex(i)
                    self.profile_combo.blockSignals(False)
                break
        self._update_connection_buttons()

    def _on_profile_changed(self, index: int) -> None:
        if self._applying_snapshot_to_inputs:
            return
        data = self.profile_combo.itemData(index) or {}
        if not isinstance(data, dict) or data.get("key") == "custom":
            self._update_connection_buttons()
            return
        profile = normalize_profile_dict(data)
        self._applying_snapshot_to_inputs = True
        try:
            platform = str(profile.get("platform") or GATEWAY_PLATFORM)
            mode = str(profile.get("trading_mode") or "live")
            host = str(profile.get("host") or "127.0.0.1")
            port = int(profile.get("port") or default_port(platform, mode))
            self._manual_trading_mode = mode
            for i in range(self.platform_combo.count()):
                if self.platform_combo.itemData(i) == platform:
                    self.platform_combo.setCurrentIndex(i)
                    break
            self.host_edit.setText(host)
            self.port_spin.setValue(port)
        finally:
            self._applying_snapshot_to_inputs = False
        self._update_connection_buttons()
        self._schedule_settings_autosave()

    def _on_platform_changed(self, *args: Any) -> None:
        if self._applying_snapshot_to_inputs:
            return
        self.port_spin.setValue(default_port(self._selected_platform(), self._selected_trading_mode()))
        self._update_connection_buttons()
        self._set_custom_profile_if_needed()

    def _wire_settings_autosave(self) -> None:
        for widget in [self.host_edit, self.account_edit, self.platform_path_edit, self.ticker_edit, self.primary_exchange_edit]:
            widget.textEdited.connect(self._schedule_settings_autosave)
        self.host_edit.textEdited.connect(lambda *_: self._set_custom_profile_if_needed())
        for widget in [
            self.port_spin,
            self.client_spin,
            self.investment_spin,
            self.initial_drop_spin,
            self.buy_rebound_spin,
            self.rise_trigger_spin,
            self.sell_trail_spin,
            self.atr_period_spin,
            self.atr_bar_seconds_spin,
            self.atr_initial_drop_mult_spin,
            self.atr_buy_rebound_mult_spin,
            self.atr_min_profit_mult_spin,
            self.atr_sell_trail_mult_spin,
            self.atr_protective_sell_mult_spin,
            self.atr_min_pct_spin,
            self.atr_max_pct_spin,
            self.protective_sell_trail_spin,
            self.slippage_buffer_spin,
            self.max_daily_loss_ticker_spin,
            self.max_daily_loss_total_spin,
            self.max_cycles_ticker_day_spin,
            self.max_consecutive_losses_spin,
            self.max_spread_pct_spin,
            self.min_trade_price_spin,
            self.max_gap_pct_spin,
            self.max_price_age_spin,
            self.max_bidask_age_spin,
            self.max_rth_age_spin,
            self.volatility_window_spin,
            self.max_recent_move_spin,
            self.no_new_buy_first_spin,
            self.no_new_buy_last_spin,
            self.cancel_buy_before_close_spin,
        ]:
            widget.valueChanged.connect(self._schedule_settings_autosave)
        self.port_spin.valueChanged.connect(lambda *_: self._set_custom_profile_if_needed())
        for widget in [self.market_data_combo, self.platform_combo, self.profile_combo]:
            widget.currentIndexChanged.connect(self._schedule_settings_autosave)
        for widget in [self.reinvest_check, self.auto_repeat_check, self.atr_adaptive_check, self.atr_block_until_ready_check, self.atr_min_profit_adaptive_check, self.atr_protective_sell_adaptive_check, self.protective_sell_check, self.slippage_buffer_check, self.hard_risk_limits_check, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.volatility_filter_check, self.session_timing_guard_check]:
            widget.toggled.connect(self._schedule_settings_autosave)

    def _schedule_history_filter(self, *args: Any) -> None:
        self._history_filter_timer.start()

    def _schedule_visual_refresh(self) -> None:
        self._visual_refresh_timer.start()

    def _run_visual_refresh(self) -> None:
        if self._applying_snapshot_to_inputs:
            return
        self._update_connection_buttons()
        self._apply_profit_guard_bounds()
        if hasattr(self, "atr_adaptive_check"):
            self._set_atr_percentage_field_state(self.atr_adaptive_check.isChecked())
        self._update_strategy_previews()
        self._update_zero_disabled_indicators()
        self._update_input_change_indicators(((self.current_snapshot or {}).get("active_cycle") or {}))
        self._update_command_bar_states(self.current_snapshot)
        self._update_dynamic_graphs()

    def _schedule_settings_autosave(self, *args: Any) -> None:
        if self._applying_snapshot_to_inputs:
            return
        self._mark_sender_changed_if_running()
        self._autosave_timer.start()
        self._schedule_visual_refresh()

    def _strategy_visual_inputs_changed(self, *args: Any) -> None:
        if self._applying_snapshot_to_inputs:
            return
        self._mark_sender_changed_if_running()
        self._schedule_visual_refresh()

    def _update_strategy_previews(self) -> None:
        if not hasattr(self, "entry_preview_label"):
            return
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        price_snapshot = (self.current_snapshot or {}).get("price_snapshot") or {}
        reference, reference_label = self._strategy_map_reference()
        try:
            levels = projected_minimum_profit_levels(
                float(self.initial_drop_spin.value()),
                float(self.buy_rebound_spin.value()),
                float(self.rise_trigger_spin.value()),
                float(self.sell_trail_spin.value()),
                anchor=reference,
                slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
                slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
            )
        except Exception:
            levels = {}
        selected_price = price_snapshot.get("price")
        projected_buy = float(levels.get("projected_buy_stop") or 0.0)
        sizing_price = float(levels.get("buy_sizing_price") or projected_buy or 0.0)
        qty = int(float(self.investment_spin.value()) // sizing_price) if sizing_price > 0 else 0
        notional = qty * sizing_price
        entry_text = (
            f"With selected price {_format_field_value('selected price', selected_price)} and {reference_label} {_format_currency(reference)}: "
            f"initial drop trigger {_format_currency(levels.get('drop_trigger'))}; "
            f"projected BUY stop {_format_currency(projected_buy)}; "
            f"estimated quantity {qty:,} shares; estimated notional {_format_currency(notional)}."
        )
        if self.entry_preview_label.text() != entry_text:
            self.entry_preview_label.setText(entry_text)

        if hasattr(self, "exit_preview_label"):
            avg_buy = _float_or_none(cycle.get("avg_buy_price")) or projected_buy
            anchor = _float_or_none(cycle.get("anchor_price")) or reference
            min_stop = minimum_sell_stop_price_for_profit(
                avg_buy,
                anchor,
                float(self.rise_trigger_spin.value()),
                slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
                slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
            ) if avg_buy else 0.0
            sell_trail = max(0.0, float(self.sell_trail_spin.value())) / 100.0
            required_last = min_stop / max(1e-12, 1.0 - sell_trail) if min_stop else 0.0
            source = "average buy" if cycle.get("avg_buy_price") else "projected BUY stop"
            protective_text = (
                f"Protective SELL trailing-stop {self.protective_sell_trail_spin.value():.2f}% enabled."
                if self.protective_sell_check.isChecked()
                else "Protective SELL disabled."
            )
            exit_text = (
                f"Using {source} {_format_currency(avg_buy)}: minimum protected price {_format_currency(min_stop)}; "
                f"required price before SELL trailing-stop {_format_currency(required_last)}. {protective_text}"
            )
            if self.exit_preview_label.text() != exit_text:
                self.exit_preview_label.setText(exit_text)

    def _update_input_change_indicators(self, cycle: Optional[dict[str, Any]]) -> None:
        if not hasattr(self, "changed_while_running_label"):
            return
        cycle = cycle or {}
        stage = cycle.get("stage")
        active = self._is_active_stage(stage)
        cycle_token = str(cycle.get("id") or cycle.get("cycle_number") or "") if active else ""
        if not active:
            self._running_cycle_token = None
            self._running_change_baseline = {}
            self._changed_while_running_fields.clear()
            summary = "Changed while running: no active cycle. Per-field badges will activate when a cycle is running."
            if self.changed_while_running_label.text() != summary:
                self.changed_while_running_label.setText(summary)
            for key, badge in self._field_change_badges.items():
                label, tip = self._field_applicability(key, stage)
                state = self._change_badge_style_token(label)
                if badge.text() != label:
                    badge.setText(label)
                style_changed = badge.property("state") != state
                if style_changed:
                    badge.setProperty("state", state)
                if badge.toolTip() != tip:
                    badge.setToolTip(tip)
                if style_changed:
                    badge.style().unpolish(badge)
                    badge.style().polish(badge)
            return
        if self._running_cycle_token != cycle_token:
            self._running_cycle_token = cycle_token
            self._running_change_baseline = self._current_change_field_values()
            self._changed_while_running_fields.clear()
        for key, widget in self._field_change_widgets.items():
            current = self._field_value_for_change_tracking(widget)
            baseline = self._running_change_baseline.get(key)
            if current == baseline:
                self._changed_while_running_fields.discard(key)
        changed_summaries: list[str] = []
        for key, badge in self._field_change_badges.items():
            label, tip = self._field_applicability(key, stage)
            changed = key in self._changed_while_running_fields
            badge_text = f"Changed: {label}" if changed else label
            if badge.text() != badge_text:
                badge.setText(badge_text)
            state = "changed" if changed else self._change_badge_style_token(label)
            style_changed = badge.property("state") != state
            if style_changed:
                badge.setProperty("state", state)
            field_name = self._change_field_labels.get(key, key)
            if changed:
                baseline = self._running_change_baseline.get(key)
                current = self._field_value_for_change_tracking(self._field_change_widgets[key])
                tooltip = f"{field_name} was changed while this cycle is running. {tip} Baseline: {baseline}; current: {current}."
                changed_summaries.append(f"{field_name} - {label}")
            else:
                tooltip = f"{field_name}: {tip}"
            if badge.toolTip() != tooltip:
                badge.setToolTip(tooltip)
            if style_changed:
                badge.style().unpolish(badge)
                badge.style().polish(badge)
        if changed_summaries:
            shown = "; ".join(changed_summaries[:8])
            extra = "" if len(changed_summaries) <= 8 else f"; +{len(changed_summaries) - 8} more"
            summary = f"Changed while running: {shown}{extra}."
        else:
            summary = "Changed while running: none yet. Per-field badges show what an edit would affect in the current stage."
        if self.changed_while_running_label.text() != summary:
            self.changed_while_running_label.setText(summary)


    def _apply_profit_guard_bounds(self, *args: Any) -> None:
        if self._updating_profit_bounds:
            return
        if not hasattr(self, "rise_trigger_spin") or not hasattr(self, "sell_trail_spin"):
            return
        self._updating_profit_bounds = True
        try:
            # Compatibility semantics: the persisted field name rise_trigger_pct is
            # presented as Minimum profit %. The minimum is measured from the
            # actual average BUY fill; the controller waits for a high-enough last
            # price before placing the final SELL trailing stop.
            self.rise_trigger_spin.setMinimum(PROFIT_GUARD_EPSILON_PCT)
            self.rise_trigger_spin.setMaximum(99.99)
            self.sell_trail_spin.setMinimum(0.0)
            self.sell_trail_spin.setMaximum(99.99)

            initial = float(self.initial_drop_spin.value())
            buy_rebound = float(self.buy_rebound_spin.value())
            minimum_profit = float(self.rise_trigger_spin.value())
            sell = float(self.sell_trail_spin.value())
            levels = projected_minimum_profit_levels(
                initial,
                buy_rebound,
                minimum_profit,
                sell,
                anchor=100.0,
                slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
                slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
            )
            projected_buy = float(levels["projected_buy_stop"])
            minimum_sell_stop = float(levels["minimum_sell_stop"])
            required_last = float(levels["required_last_price"])
            profit_vs_buy = ((minimum_sell_stop / projected_buy) - 1.0) * 100.0 if projected_buy > 0 else 0.0
            profit_vs_anchor = ((minimum_sell_stop / 100.0) - 1.0) * 100.0

            if hasattr(self, "profit_guard_label"):
                self.profit_guard_label.setText(
                    f"Minimum-profit guard: first SELL stop protects {minimum_profit:.2f}%"
                    + (f" plus {self.slippage_buffer_spin.value():.2f}% slippage buffer" if self.slippage_buffer_check.isChecked() else "")
                    + f". Projected stop {_format_currency(minimum_sell_stop)}; last price needed {_format_currency(required_last)}; "
                    + f"protected vs projected buy {profit_vs_buy:.2f}%; protected vs anchor {profit_vs_anchor:.2f}%."
                )
                self.profit_guard_label.setObjectName("ProfitGuardGood")
                self.profit_guard_label.style().unpolish(self.profit_guard_label)
                self.profit_guard_label.style().polish(self.profit_guard_label)
            if hasattr(self, "profit_guard_graph"):
                reference, reference_label = self._strategy_map_reference() if hasattr(self, "_strategy_map_reference") else (100.0, "normalized baseline")
                self.profit_guard_graph.set_values(
                    initial,
                    buy_rebound,
                    minimum_profit,
                    sell,
                    reference,
                    reference_label,
                    protective_sell_enabled=self.protective_sell_check.isChecked(),
                    protective_sell_trail_pct=float(self.protective_sell_trail_spin.value()),
                    slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
                    slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
                    hard_risk_limits_enabled=self.hard_risk_limits_check.isChecked(),
                    max_daily_loss_ticker=float(self.max_daily_loss_ticker_spin.value()),
                    max_daily_loss_total=float(self.max_daily_loss_total_spin.value()),
                    max_cycles_per_ticker_day=int(self.max_cycles_ticker_day_spin.value()),
                    max_consecutive_losses=int(self.max_consecutive_losses_spin.value()),
                    max_spread_pct=float(self.max_spread_pct_spin.value()),
                    min_trade_price=float(self.min_trade_price_spin.value()),
                    max_gap_pct=float(self.max_gap_pct_spin.value()),
                    block_delayed_live=self.block_delayed_live_check.isChecked(),
                    what_if_enabled=self.what_if_check.isChecked(),
                    stale_data_guard_enabled=self.stale_data_guard_check.isChecked(),
                    max_price_age_seconds=float(self.max_price_age_spin.value()),
                    volatility_filter_enabled=self.volatility_filter_check.isChecked(),
                    max_recent_move_pct=float(self.max_recent_move_spin.value()),
                    session_timing_guard_enabled=self.session_timing_guard_check.isChecked(),
                    no_new_buy_first_minutes=int(self.no_new_buy_first_spin.value()),
                    no_new_buy_last_minutes=int(self.no_new_buy_last_spin.value()),
                    cancel_buy_before_close_minutes=int(self.cancel_buy_before_close_spin.value()),
                )
            self._update_percentage_tooltips()
        finally:
            self._updating_profit_bounds = False

    def _strategy_map_reference(self) -> tuple[float, str]:
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        price_snapshot = (self.current_snapshot or {}).get("price_snapshot") or {}
        for key, label in (("anchor_price", "cycle anchor"), ("last_price", "cycle last price")):
            try:
                value = float(cycle.get(key) or 0.0)
                if value > 0:
                    return value, label
            except Exception:
                pass
        try:
            value = float(price_snapshot.get("price") or 0.0)
            if value > 0:
                return value, "current API price"
        except Exception:
            pass
        return 100.0, "normalized baseline"

    def _risk_summary_for_map(self) -> str:
        parts: list[str] = []
        if getattr(self, "block_delayed_live_check", None) is not None and self.block_delayed_live_check.isChecked():
            parts.append("live data only")
        if getattr(self, "max_spread_pct_spin", None) is not None and self.max_spread_pct_spin.value() > 0:
            parts.append(f"spread <= {self.max_spread_pct_spin.value():.2f}%")
        if getattr(self, "max_gap_pct_spin", None) is not None and self.max_gap_pct_spin.value() > 0:
            parts.append(f"gap <= {self.max_gap_pct_spin.value():.2f}%")
        if getattr(self, "max_cycles_ticker_day_spin", None) is not None and self.max_cycles_ticker_day_spin.value() > 0:
            parts.append(f"cycles <= {self.max_cycles_ticker_day_spin.value()} total")
        if getattr(self, "max_consecutive_losses_spin", None) is not None and self.max_consecutive_losses_spin.value() > 0:
            parts.append(f"loss streak <= {self.max_consecutive_losses_spin.value()}")
        if getattr(self, "max_daily_loss_ticker_spin", None) is not None and self.max_daily_loss_ticker_spin.value() > 0:
            parts.append(f"ticker loss <= {_format_currency(self.max_daily_loss_ticker_spin.value(), 0)}")
        if getattr(self, "max_daily_loss_total_spin", None) is not None and self.max_daily_loss_total_spin.value() > 0:
            parts.append(f"total loss <= {_format_currency(self.max_daily_loss_total_spin.value(), 0)}")
        if getattr(self, "min_trade_price_spin", None) is not None and self.min_trade_price_spin.value() > 0:
            parts.append(f"min price {_format_currency(self.min_trade_price_spin.value(), 2)}")
        return "; ".join(parts[:4]) if parts else "zero-valued limits off"

    def _update_dynamic_graphs(self) -> None:
        cycle = (self.current_snapshot or {}).get("active_cycle")
        price_snapshot = (self.current_snapshot or {}).get("price_snapshot")
        active_index = self.tabs.currentIndex() if hasattr(self, "tabs") else 0
        dashboard_active = active_index == 0
        flowchart_active = active_index == 1
        if dashboard_active and hasattr(self, "profit_guard_graph"):
            reference, reference_label = self._strategy_map_reference()
            self.profit_guard_graph.set_values(
                float(self.initial_drop_spin.value()),
                float(self.buy_rebound_spin.value()),
                float(self.rise_trigger_spin.value()),
                float(self.sell_trail_spin.value()),
                reference,
                reference_label,
                protective_sell_enabled=self.protective_sell_check.isChecked(),
                protective_sell_trail_pct=float(self.protective_sell_trail_spin.value()),
                slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
                slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
                hard_risk_limits_enabled=self.hard_risk_limits_check.isChecked(),
                max_daily_loss_ticker=float(self.max_daily_loss_ticker_spin.value()),
                max_daily_loss_total=float(self.max_daily_loss_total_spin.value()),
                max_cycles_per_ticker_day=int(self.max_cycles_ticker_day_spin.value()),
                max_consecutive_losses=int(self.max_consecutive_losses_spin.value()),
                max_spread_pct=float(self.max_spread_pct_spin.value()),
                min_trade_price=float(self.min_trade_price_spin.value()),
                max_gap_pct=float(self.max_gap_pct_spin.value()),
                block_delayed_live=self.block_delayed_live_check.isChecked(),
                what_if_enabled=self.what_if_check.isChecked(),
                stale_data_guard_enabled=self.stale_data_guard_check.isChecked(),
                max_price_age_seconds=float(self.max_price_age_spin.value()),
                volatility_filter_enabled=self.volatility_filter_check.isChecked(),
                max_recent_move_pct=float(self.max_recent_move_spin.value()),
                session_timing_guard_enabled=self.session_timing_guard_check.isChecked(),
                no_new_buy_first_minutes=int(self.no_new_buy_first_spin.value()),
                no_new_buy_last_minutes=int(self.no_new_buy_last_spin.value()),
                cancel_buy_before_close_minutes=int(self.cancel_buy_before_close_spin.value()),
            )
            self._update_percentage_tooltips()
        strategy = self._strategy_from_ui()
        if hasattr(self, "strategy_graph"):
            self.strategy_graph.update_data(cycle, price_snapshot, strategy, repaint=dashboard_active)
        if dashboard_active and hasattr(self, "current_stage_panel"):
            self.current_stage_panel.update_data(cycle, price_snapshot, strategy)
        if dashboard_active and hasattr(self, "why_not_moving_panel"):
            self.why_not_moving_panel.update_data(cycle, price_snapshot)
        if flowchart_active and hasattr(self, "flowchart_panel"):
            self.flowchart_panel.update_data(cycle, price_snapshot, strategy)

    def _autosave_settings(self) -> None:
        self.controller.save_draft_settings(self._connection_from_ui(), self._strategy_from_ui())

    def _save_resume_checkpoint(self, reason: str) -> bool:
        """Synchronously persist the same state used by resume/recover later."""
        # Do not stop the periodic GUI autosave timer here. This method blocks
        # the GUI thread while the durable checkpoint is written, so the timer
        # cannot interleave with it. Keeping the timer armed matters when a
        # Windows shutdown is cancelled or a manual checkpoint fails.
        try:
            connection = self._connection_from_ui()
            strategy = self._strategy_from_ui()
        except Exception:
            connection = self.controller.connection
            strategy = self.controller.strategy

        method = getattr(self.controller, "checkpoint_for_resume_later", None)
        if not callable(method):
            # Compatibility fallback for test doubles and older controller
            # boundaries. Production builds use the synchronous path.
            self.controller.save_draft_settings(connection, strategy)
            return True
        try:
            result = method(connection, strategy, reason=reason, timeout=4.0)
        except Exception:
            return False
        return True if result is None else bool(result)

    def handle_system_shutdown(self, session_manager: Any = None) -> None:
        """Commit resumable state for a Windows logoff/restart/shutdown."""
        session_key = ""
        get_session_key = getattr(session_manager, "sessionKey", None)
        if callable(get_session_key):
            try:
                session_key = str(get_session_key() or "")
            except Exception:
                session_key = ""
        if session_key and session_key == self._last_system_shutdown_session_key:
            return
        if session_key:
            self._last_system_shutdown_session_key = session_key

        self._system_shutdown_in_progress = True
        self._save_resume_checkpoint("windows_session_shutdown")
        # Do not stop the worker or exit from commitDataRequest. If another
        # application cancels Windows shutdown, this process must remain usable.
        # If shutdown proceeds, Qt closes the windows and main() performs the
        # normal worker cleanup after the event loop ends.

    def _connection_from_ui(self) -> ConnectionSettings:
        return ConnectionSettings(
            host=self.host_edit.text().strip(),
            port=int(self.port_spin.value()),
            client_id=int(self.client_spin.value()),
            account=self.account_edit.text().strip(),
            trading_mode=self._selected_trading_mode(),
            platform=self._selected_platform(),
            platform_path=self.platform_path_edit.text().strip(),
            market_data_type=int(self.market_data_combo.currentData()),
        )

    def _strategy_from_ui(self) -> StrategySettings:
        return StrategySettings(
            ticker=self.ticker_edit.text().strip().upper(),
            investment_amount=float(self.investment_spin.value()),
            initial_drop_pct=float(self.initial_drop_spin.value()),
            buy_rebound_trail_pct=float(self.buy_rebound_spin.value()),
            rise_trigger_pct=float(self.rise_trigger_spin.value()),
            sell_trailing_stop_pct=float(self.sell_trail_spin.value()),
            atr_adaptive_enabled=self.atr_adaptive_check.isChecked(),
            atr_adapt_minimum_profit_enabled=self.atr_min_profit_adaptive_check.isChecked(),
            atr_block_new_buy_until_ready=self.atr_block_until_ready_check.isChecked(),
            atr_adapt_protective_sell_enabled=self.atr_protective_sell_adaptive_check.isChecked(),
            atr_protective_sell_multiplier=float(self.atr_protective_sell_mult_spin.value()),
            atr_period=int(self.atr_period_spin.value()),
            atr_bar_seconds=int(self.atr_bar_seconds_spin.value()),
            atr_initial_drop_multiplier=float(self.atr_initial_drop_mult_spin.value()),
            atr_buy_rebound_multiplier=float(self.atr_buy_rebound_mult_spin.value()),
            atr_minimum_profit_multiplier=float(self.atr_min_profit_mult_spin.value()),
            atr_sell_trail_multiplier=float(self.atr_sell_trail_mult_spin.value()),
            atr_min_pct=float(self.atr_min_pct_spin.value()),
            atr_max_pct=float(self.atr_max_pct_spin.value()),
            protective_sell_enabled=self.protective_sell_check.isChecked(),
            protective_sell_trailing_stop_pct=float(self.protective_sell_trail_spin.value()),
            slippage_buffer_enabled=self.slippage_buffer_check.isChecked(),
            slippage_buffer_pct=float(self.slippage_buffer_spin.value()),
            hard_risk_limits_enabled=self.hard_risk_limits_check.isChecked(),
            max_daily_loss_ticker=float(self.max_daily_loss_ticker_spin.value()),
            max_daily_loss_total=float(self.max_daily_loss_total_spin.value()),
            max_cycles_per_ticker_day=int(self.max_cycles_ticker_day_spin.value()),
            max_consecutive_losses=int(self.max_consecutive_losses_spin.value()),
            max_spread_pct=float(self.max_spread_pct_spin.value()),
            min_trade_price=float(self.min_trade_price_spin.value()),
            max_gap_from_prev_close_pct=float(self.max_gap_pct_spin.value()),
            block_delayed_data_in_live=self.block_delayed_live_check.isChecked(),
            what_if_check_enabled=self.what_if_check.isChecked(),
            stale_data_guard_enabled=self.stale_data_guard_check.isChecked(),
            max_selected_price_age_seconds=float(self.max_price_age_spin.value()),
            max_bid_ask_age_seconds=float(self.max_bidask_age_spin.value()),
            max_rth_status_age_seconds=float(self.max_rth_age_spin.value()),
            volatility_filter_enabled=self.volatility_filter_check.isChecked(),
            volatility_window_seconds=int(self.volatility_window_spin.value()),
            max_recent_price_move_pct=float(self.max_recent_move_spin.value()),
            session_timing_guard_enabled=self.session_timing_guard_check.isChecked(),
            no_new_buy_first_minutes=int(self.no_new_buy_first_spin.value()),
            no_new_buy_last_minutes=int(self.no_new_buy_last_spin.value()),
            cancel_buy_before_close_minutes=int(self.cancel_buy_before_close_spin.value()),
            reinvest_profits=self.reinvest_check.isChecked(),
            auto_repeat=self.auto_repeat_check.isChecked(),
            rth_only=True,
            exchange="SMART",
            primary_exchange=self.primary_exchange_edit.text().strip().upper(),
            contract_con_id=self._contract_con_id_from_ui(),
            currency="USD",
            sec_type="STK",
            tif="GTC",
        )

    def _contract_con_id_from_ui(self) -> Optional[int]:
        text = self.con_id_edit.text().strip() if hasattr(self, "con_id_edit") else ""
        if not text:
            return None
        try:
            value = int(text)
        except Exception:
            return None
        return value if value > 0 else None

    def _set_selected_contract_con_id(self, value: Any) -> None:
        if not hasattr(self, "con_id_edit"):
            return
        try:
            con_id = int(value) if value not in (None, "") else 0
        except Exception:
            con_id = 0
        self.con_id_edit.setText(str(con_id) if con_id > 0 else "")

    def _clear_selected_contract_con_id(self, *args: Any) -> None:
        if hasattr(self, "con_id_edit") and self.con_id_edit.text():
            self.con_id_edit.clear()
            self.contract_label.setText("Contract: STK / SMART / optional primary exchange / USD.")
            self._schedule_settings_autosave()

    def _connect_clicked(self) -> None:
        self.controller.connect_tws(self._connection_from_ui())

    def _start_platform_clicked(self) -> None:
        self.controller.start_ibkr_platform(self._connection_from_ui())

    def _browse_platform_path(self) -> None:
        title = f"Select {platform_label(self._selected_platform())} executable"
        path, _ = QFileDialog.getOpenFileName(self, title, "", "Executable files (*.exe);;All files (*)")
        if path:
            self.platform_path_edit.setText(path)
            self._schedule_settings_autosave()

    def _search_ticker_clicked(self) -> None:
        pattern = self.ticker_edit.text().strip()
        if not pattern:
            QMessageBox.warning(self, "Ticker required", "Enter a ticker symbol or company name first.")
            return
        self.ticker_matches_combo.clear()
        self.ticker_matches_combo.addItem("Searching IBKR API...", None)
        self.controller.search_tickers(self._connection_from_ui(), pattern)

    def _on_ticker_search_results(self, rows: list[dict[str, Any]]) -> None:
        self.ticker_matches_combo.clear()
        if not rows:
            self.ticker_matches_combo.addItem("No API matches returned", None)
            return
        for row in rows:
            label = row.get("label") or row.get("symbol") or "Contract match"
            self.ticker_matches_combo.addItem(str(label), row)

    def _selected_ticker_match(self) -> Optional[dict[str, Any]]:
        data = self.ticker_matches_combo.currentData() if hasattr(self, "ticker_matches_combo") else None
        return data if isinstance(data, dict) else None

    def _use_selected_ticker_match(self) -> None:
        match = self._selected_ticker_match()
        if not match:
            QMessageBox.information(self, "No match selected", "Search for ticker first, then select a contract.")
            return
        symbol = str(match.get("symbol") or "").upper().strip()
        primary_exchange = str(match.get("primary_exchange") or match.get("exchange") or "").upper().strip()
        if symbol:
            self.ticker_edit.setText(symbol)
        if primary_exchange:
            self.primary_exchange_edit.setText(primary_exchange)
        self._set_selected_contract_con_id(match.get("con_id"))
        self._schedule_settings_autosave()

    def _confirm_ticker_price_clicked(self) -> None:
        match = self._selected_ticker_match()
        if match:
            symbol = str(match.get("symbol") or "").upper().strip()
            primary_exchange = str(match.get("primary_exchange") or match.get("exchange") or "").upper().strip()
            if symbol:
                self.ticker_edit.setText(symbol)
            if primary_exchange:
                self.primary_exchange_edit.setText(primary_exchange)
            self._set_selected_contract_con_id(match.get("con_id"))
        strategy = self._strategy_from_ui()
        if not strategy.normalized_ticker():
            QMessageBox.warning(self, "Ticker required", "Enter or select a ticker first.")
            return
        self.controller.confirm_ticker_price(self._connection_from_ui(), strategy)

    def _start_clicked(self) -> None:
        connection = self._connection_from_ui()
        strategy = self._strategy_from_ui()
        errors = connection.validate() + strategy.validate()
        if errors:
            QMessageBox.warning(self, "Invalid input", "\n".join(errors))
            return
        summary = [
            f"Ticker: {strategy.normalized_ticker()}",
            f"Profile: {connection.platform} / {connection.trading_mode.upper()} / {connection.host}:{connection.port}",
            f"Investment amount: {_format_currency(strategy.investment_amount, 2)}",
            f"Protective SELL: {'ON' if strategy.protective_sell_enabled else 'OFF'} ({strategy.protective_sell_trailing_stop_pct:.2f}%)",
            f"Slippage buffer: {'ON' if strategy.slippage_buffer_enabled else 'OFF'} ({strategy.slippage_buffer_pct:.2f}% for BUY sizing and SELL profit trigger)",
            f"Hard risk limits: {'ON' if strategy.hard_risk_limits_enabled else 'OFF'}",
            "RTH-only: ON",
        ]
        if connection.trading_mode == "live":
            ok = QMessageBox.question(
                self,
                "Live mode pre-flight confirmation",
                "Live mode can place real orders. Review before arming the strategy:\n\n" + "\n".join(summary),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ok != QMessageBox.Yes:
                return
        self.controller.start_strategy(connection, strategy)

    def _visible_tws_open_app_orders(self) -> list[dict[str, Any]]:
        orders, _superseded = _reconciled_open_app_orders(self.current_snapshot)
        return orders

    def _persisted_app_unsold_quantity(self, cycle: dict[str, Any]) -> float:
        """Read the latest app-owned unsold quantity without using broker holdings."""
        fallback = _app_owned_unsold_quantity(cycle)
        ticker = str(cycle.get("ticker") or "").strip().upper()
        method = getattr(self.controller, "app_owned_unsold_position", None)
        if not ticker or not callable(method):
            return fallback
        try:
            summary = method(ticker) or {}
            quantity = _float_or_none(summary.get("quantity"))
            return max(0.0, quantity) if quantity is not None else fallback
        except Exception:
            return fallback

    def _request_stop_action(self, action: StopAction, *, wait_for_local_state: bool = False) -> bool:
        """Request a stop action and optionally wait until the worker applies it.

        The ordinary stop buttons stay asynchronous so broker-affecting actions
        remain visible to the operator. The explicit stop-and-exit path waits
        for the local STOPPED state to be persisted before closing; otherwise a
        restart can still see the previous active cycle and keep ticker inputs
        locked.
        """
        if wait_for_local_state:
            method = getattr(self.controller, "request_stop_and_wait", None)
            if callable(method):
                try:
                    return bool(method(action, timeout=4.0))
                except Exception as exc:
                    QMessageBox.warning(self, "Stop strategy", f"Stop action could not be confirmed before exit:\n{exc}")
                    return False
        self.controller.request_stop(action)
        return True

    def _open_stop_strategy_dialog(self) -> None:
        open_orders = self._visible_tws_open_app_orders()
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        stage_value = str(cycle.get("stage") or "")
        safe_no_running_strategy = stage_value in {"", Stage.IDLE.value, Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value}
        unsold_qty = self._persisted_app_unsold_quantity(cycle)
        show_position_close = bool(unsold_qty > 0)
        dialog = StopDialog(
            self,
            show_tws_order_actions=bool(open_orders),
            open_order_count=len(open_orders),
            show_position_close_action=show_position_close,
            unsold_quantity=unsold_qty,
            safe_to_exit=(not open_orders and not show_position_close and safe_no_running_strategy),
            show_resume_later_exit_action=bool(cycle and not safe_no_running_strategy),
        )
        if dialog.exec() == QDialog.Accepted:
            if dialog.selected_action is not None:
                wait_for_local_state = bool(dialog.exit_app_after_action and dialog.selected_action == StopAction.STOP_NOW_NO_BROKER_ACTION)
                if not self._request_stop_action(dialog.selected_action, wait_for_local_state=wait_for_local_state):
                    return
            if dialog.exit_app_after_action:
                self._stop_dialog_exit_requested = True
                self.close()

    def _stop_clicked(self) -> None:
        self._open_stop_strategy_dialog()

    def _on_connection_changed(self, connected: bool, status: str) -> None:
        if self.connection_status.text() != status:
            self.connection_status.setText(status)

    def _on_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.current_snapshot = snapshot
        connection = snapshot.get("connection") or {}
        strategy = snapshot.get("strategy") or {}
        cycle = snapshot.get("active_cycle")
        status_text = str(snapshot.get("status", ""))
        if self.connection_status.text() != status_text:
            self.connection_status.setText(status_text)
        db_path_text = str(snapshot.get("db_path", ""))
        if self.db_path_label.text() != db_path_text:
            self.db_path_label.setText(db_path_text)
        if hasattr(self, "live_status_bar"):
            self.live_status_bar.update_data(snapshot)
        self._update_command_bar_states(snapshot)

        # Load persisted settings once on startup. Do not continuously push
        # worker snapshots back into editable controls; otherwise a periodic
        # snapshot can overwrite values while the user is changing them.
        if not self._inputs_loaded_from_snapshot:
            self._apply_snapshot_to_inputs(connection, strategy)
            self._inputs_loaded_from_snapshot = True
        stage = cycle.get("stage") if cycle else None
        self._update_input_locks(stage)
        self._apply_atr_adaptive_snapshot_to_inputs(snapshot)
        self._apply_suggested_risk_limits_from_amount()
        dashboard_active = not hasattr(self, "tabs") or self.tabs.currentIndex() == 0
        if dashboard_active:
            self.stage_ribbon.set_stage(stage)
            self._update_metrics(cycle)
            self._update_price_feed(snapshot.get("price_snapshot"), snapshot.get("price_poll_interval_seconds"))
            self._update_strategy_previews()
            self._update_input_change_indicators(cycle)
        self._update_dynamic_graphs()
        events = snapshot.get("events") or []
        events_signature = repr(events[-80:])
        if events_signature != getattr(self, "_last_events_signature", None):
            self._last_events_signature = events_signature
            self._update_event_log(events)
        history_summary = snapshot.get("history_summary") or {}
        history_signature = repr(sorted(history_summary.items()))
        if history_signature != getattr(self, "_last_history_summary_signature", None):
            self._last_history_summary_signature = history_signature
            self._update_history_summary(history_summary)
        recovery_refresh = _recovery_refresh_status(snapshot)
        recovery_payload = {
            "status": snapshot.get("status"),
            "connected": snapshot.get("connected"),
            "recovery_required": snapshot.get("recovery_required"),
            "recovery_confidence": snapshot.get("recovery_confidence"),
            "active_cycle": snapshot.get("active_cycle"),
            "stale_active_cycle": snapshot.get("stale_active_cycle"),
            "stale_active_cycle_age_seconds": snapshot.get("stale_active_cycle_age_seconds"),
            "broker_recovery": snapshot.get("broker_recovery"),
            "broker_refresh_state": recovery_refresh.get("state"),
            "broker_refresh_reason": recovery_refresh.get("reason"),
            "events_tail": events[-20:],
        }
        recovery_signature = repr(recovery_payload)
        if recovery_signature != getattr(self, "_last_recovery_panel_signature", None):
            self._last_recovery_panel_signature = recovery_signature
            self._update_recovery_panel(snapshot)

    def _apply_atr_adaptive_snapshot_to_inputs(self, snapshot: dict[str, Any]) -> None:
        if not hasattr(self, "atr_adaptive_check"):
            return
        strategy = snapshot.get("strategy") or {}
        price_snapshot = snapshot.get("price_snapshot") or {}
        enabled = bool(strategy.get("atr_adaptive_enabled", self.atr_adaptive_check.isChecked()) or self.atr_adaptive_check.isChecked())
        atr = price_snapshot.get("atr") or {}
        adaptive = price_snapshot.get("atr_adaptive_percentages") or {}
        ready = bool(price_snapshot.get("atr_ready") or atr.get("ready"))
        if enabled and adaptive:
            self._applying_snapshot_to_inputs = True
            try:
                self.initial_drop_spin.setValue(float(adaptive.get("initial_drop_pct", self.initial_drop_spin.value())))
                self.buy_rebound_spin.setValue(float(adaptive.get("buy_rebound_trail_pct", self.buy_rebound_spin.value())))
                if bool(adaptive.get("atr_adapt_minimum_profit_enabled", self.atr_min_profit_adaptive_check.isChecked())):
                    self.rise_trigger_spin.setValue(float(adaptive.get("rise_trigger_pct", self.rise_trigger_spin.value())))
                self.sell_trail_spin.setValue(float(adaptive.get("sell_trailing_stop_pct", self.sell_trail_spin.value())))
                if bool(adaptive.get("atr_adapt_protective_sell_enabled", self.atr_protective_sell_adaptive_check.isChecked())):
                    self.protective_sell_trail_spin.setValue(float(adaptive.get("protective_sell_trailing_stop_pct", self.protective_sell_trail_spin.value())))
            finally:
                self._applying_snapshot_to_inputs = False
        if enabled:
            if ready:
                atr_pct = price_snapshot.get("atr_pct") or atr.get("atr_pct")
                bars = price_snapshot.get("atr_bars_available") or atr.get("bars_available")
                profit_mode = "Minimum profit is ATR-adaptive." if self.atr_min_profit_adaptive_check.isChecked() else "Minimum profit remains manually set."
                protective_mode = "Protective SELL is ATR-adaptive." if self.atr_protective_sell_adaptive_check.isChecked() else "Protective SELL remains manually set."
                self.atr_status_label.setText(f"ATR adaptive ON: ATR {_format_field_value('atr_pct', atr_pct)} from {bars or '-'} RTH-only app-observed bars. {profit_mode} {protective_mode}")
            else:
                reason = atr.get("reason") or "waiting for enough price observations"
                bars = price_snapshot.get("atr_bars_available") or atr.get("bars_available") or 0
                required = price_snapshot.get("atr_bars_required") or atr.get("bars_required") or "-"
                self.atr_status_label.setText(f"ATR adaptive ON but pending: {reason} ({bars}/{required} RTH-only bars). Existing percentage fields remain in use until ATR is ready during RTH.")
        else:
            bars = price_snapshot.get("atr_bars_available")
            if bars is None:
                bars = atr.get("bars_available")
            required = price_snapshot.get("atr_bars_required")
            if required is None:
                required = atr.get("bars_required")
            collection_state = "collecting during RTH" if bool(atr.get("rth_open")) else "paused outside RTH"
            readiness = "ATR data ready" if bool(atr.get("data_ready")) else "warming up"
            self.atr_status_label.setText(
                f"ATR adaptive OFF; RTH data collection is {collection_state}: "
                f"{bars or 0}/{required or '-'} bars ({readiness}). No strategy percentages are changed."
            )
        self._set_atr_percentage_field_state(enabled)

    def _atr_config_widgets(self) -> list[Any]:
        """ATR controls that participate in the top-bar accidental-edit lock."""
        return [
            self.atr_adaptive_check,
            self.atr_block_until_ready_check,
            self.atr_min_profit_adaptive_check,
            self.atr_protective_sell_adaptive_check,
            self.atr_period_spin,
            self.atr_bar_seconds_spin,
            self.atr_initial_drop_mult_spin,
            self.atr_buy_rebound_mult_spin,
            self.atr_min_profit_mult_spin,
            self.atr_sell_trail_mult_spin,
            self.atr_protective_sell_mult_spin,
            self.atr_min_pct_spin,
            self.atr_max_pct_spin,
        ]

    def _set_atr_percentage_field_state(self, atr_enabled: bool) -> None:
        # These four fields still carry the active values used by the strategy.
        # When ATR mode is enabled, the app can selectively leave Minimum profit %
        # manual while adapting the other three percentage fields. Use the central
        # enable helper so the top-bar input lock cannot be bypassed by ATR mode.
        stage = ((self.current_snapshot or {}).get("active_cycle") or {}).get("stage")
        active = self._is_active_stage(stage)
        if not active or stage == Stage.WAIT_INITIAL_DROP.value:
            manual_states = (True, True, True, True)
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            manual_states = (False, False, True, True)
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            manual_states = (False, False, True, True)
        else:
            manual_states = (False, False, False, False)
        adapt_min_profit = bool(getattr(self, "atr_min_profit_adaptive_check", None) and self.atr_min_profit_adaptive_check.isChecked())
        adapt_protective = bool(getattr(self, "atr_protective_sell_adaptive_check", None) and self.atr_protective_sell_adaptive_check.isChecked())
        if atr_enabled:
            states = (False, False, manual_states[2] and not adapt_min_profit, False)
            self._set_widgets_enabled([self.atr_min_profit_mult_spin], adapt_min_profit)
            self._set_widgets_enabled([self.atr_protective_sell_mult_spin], adapt_protective)
        else:
            states = manual_states
            self._set_widgets_enabled([self.atr_min_profit_mult_spin, self.atr_protective_sell_mult_spin], True)
        for widget, enabled in zip([self.initial_drop_spin, self.buy_rebound_spin, self.rise_trigger_spin, self.sell_trail_spin], states):
            self._set_widgets_enabled([widget], enabled)
        if hasattr(self, "protective_sell_trail_spin"):
            protective_manual_enabled = (not atr_enabled or not adapt_protective) and stage in {None, Stage.WAIT_INITIAL_DROP.value, Stage.BUY_TRAIL_ACTIVE.value}
            self._set_widgets_enabled([self.protective_sell_trail_spin], protective_manual_enabled)

    def _update_recovery_panel(self, snapshot: dict[str, Any]) -> None:
        if not hasattr(self, "recovery_details"):
            return
        cycle = snapshot.get("active_cycle") or {}
        events = snapshot.get("events") or []
        broker = snapshot.get("broker_recovery") or {}
        refresh = _recovery_refresh_status(snapshot)
        refresh_current = bool(refresh.get("is_current"))
        recovery_confidence = str(snapshot.get("recovery_confidence") or broker.get("recovery_confidence") or "local_state_only")
        open_orders, superseded_orders = _reconciled_open_app_orders(snapshot)
        stale_active_cycle = bool(snapshot.get("stale_active_cycle"))
        stale_age_seconds = _float_or_none(snapshot.get("stale_active_cycle_age_seconds"))
        recovery_required = bool(snapshot.get("recovery_required") or stale_active_cycle or cycle.get("recovery_required") or cycle.get("stage") == Stage.MANUAL_REVIEW.value)
        has_cycle = bool(cycle)
        stage = str(cycle.get("stage") or "No active cycle")

        def order_line(label: str, ref_key: str, id_key: str, perm_key: str, status_key: str, filled_key: str = "") -> str:
            ref = cycle.get(ref_key)
            order_id = cycle.get(id_key)
            perm_id = cycle.get(perm_key)
            status = cycle.get(status_key)
            filled = cycle.get(filled_key) if filled_key else None
            if not (ref or order_id or perm_id or status):
                return f"{label}: no local order recorded"
            parts = [f"{label}"]
            if order_id:
                parts.append(f"id {order_id}")
            if perm_id:
                parts.append(f"permId {perm_id}")
            if status:
                parts.append(f"status {status}")
            if ref:
                parts.append(f"ref {ref}")
            if filled is not None and str(filled) != "":
                parts.append(f"filled {filled}")
            return "; ".join(parts)

        def broker_order_line(order: dict[str, Any]) -> str:
            raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
            action = raw.get("action") or raw.get("side") or "ORDER"
            order_type = raw.get("orderType") or raw.get("order_type") or ""
            trail = raw.get("trailingPercent") or raw.get("trailing_percent")
            stop = raw.get("trailStopPrice") or raw.get("stop_price")
            pieces = [f"{action} {order_type}".strip()]
            if order.get("order_id"):
                pieces.append(f"id {order.get('order_id')}")
            if order.get("perm_id"):
                pieces.append(f"permId {order.get('perm_id')}")
            if order.get("status"):
                pieces.append(f"status {order.get('status')}")
            if order.get("filled") is not None:
                pieces.append(f"filled {order.get('filled')}")
            if order.get("remaining") is not None:
                pieces.append(f"remaining {order.get('remaining')}")
            if trail is not None:
                pieces.append(f"trail {trail}%")
            if stop is not None:
                pieces.append(f"stop {_format_currency(stop)}")
            if order.get("order_ref"):
                pieces.append(f"ref {order.get('order_ref')}")
            return "; ".join(str(piece) for piece in pieces if str(piece).strip())

        def first_matching_order(ref: Any) -> Optional[dict[str, Any]]:
            ref_text = str(ref or "")
            if not ref_text:
                return None
            for order in open_orders:
                if isinstance(order, dict) and str(order.get("order_ref") or "") == ref_text:
                    return order
            return None

        def status_working(ref: Any, status: Any, filled_qty: Any = 0) -> bool:
            if not ref:
                return False
            try:
                if int(float(filled_qty or 0)) > 0:
                    return False
            except Exception:
                pass
            return str(status or "").strip() not in {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}

        def broker_probe_stale_for(*timestamps: Any) -> bool:
            checked_at = broker.get("checked_at")
            if not checked_at:
                return True
            for value in timestamps:
                if _timestamp_after(checked_at, value, tolerance_seconds=2.0):
                    return True
            return False

        bought = _float_or_none(cycle.get("buy_filled_qty")) or 0.0
        final_sold = _float_or_none(cycle.get("sell_filled_qty")) or 0.0
        protective_sold = _float_or_none(cycle.get("protective_sell_filled_qty")) or 0.0
        open_qty = self._persisted_app_unsold_quantity(cycle) if has_cycle else 0.0
        broker_position = _float_or_none(broker.get("position_size"))
        broker_position_text = "Not available from current broker snapshot"
        if broker_position is not None:
            account = broker.get("position_account") or cycle.get("account") or "account not specified"
            broker_position_text = f"TWS position {broker_position:g} shares for {account}"
        elif broker.get("position_error"):
            broker_position_text = f"Position check error: {broker.get('position_error')}"
        elif broker.get("checked_at") and not broker.get("connected"):
            broker_position_text = "Not checked because API is disconnected"

        local_cycle_text = (
            f"Stage {_stage_index(stage) or '-'} {stage}; ticker {cycle.get('ticker') or '-'}; cycle {cycle.get('cycle_number') or '-'}; account {cycle.get('account') or '-'}"
            if has_cycle
            else "No active SQLite cycle"
        )
        local_position_text = (
            f"App-estimated unsold position {open_qty:g} shares (BUY filled {bought:g}; final SELL filled {final_sold:g}; protective SELL filled {protective_sold:g})"
            if has_cycle
            else "No active app-owned position in SQLite"
        )

        expected_orders: list[str] = []
        if has_cycle:
            if stage == Stage.BUY_TRAIL_ACTIVE.value:
                expected_orders.append(order_line("Expected BUY TRAIL", "buy_order_ref", "buy_order_id", "buy_perm_id", "buy_status", "buy_filled_qty"))
            elif stage == Stage.WAIT_RISE_TRIGGER.value:
                if cycle.get("protective_sell_order_ref") or cycle.get("protective_sell_status"):
                    expected_orders.append(order_line("Expected protective SELL", "protective_sell_order_ref", "protective_sell_order_id", "protective_sell_perm_id", "protective_sell_status", "protective_sell_filled_qty"))
                else:
                    expected_orders.append("No active final SELL order expected until the minimum-profit trigger passes")
            elif stage == Stage.SELL_TRAIL_ACTIVE.value:
                expected_orders.append(order_line("Expected SELL TRAIL", "sell_order_ref", "sell_order_id", "sell_perm_id", "sell_status", "sell_filled_qty"))
            elif stage == Stage.WAIT_INITIAL_DROP.value:
                expected_orders.append("No broker order expected yet; the app is waiting for initial drop")
            else:
                for args in [
                    ("BUY TRAIL", "buy_order_ref", "buy_order_id", "buy_perm_id", "buy_status", "buy_filled_qty"),
                    ("PROTECTIVE SELL", "protective_sell_order_ref", "protective_sell_order_id", "protective_sell_perm_id", "protective_sell_status", "protective_sell_filled_qty"),
                    ("SELL TRAIL", "sell_order_ref", "sell_order_id", "sell_perm_id", "sell_status", "sell_filled_qty"),
                ]:
                    line = order_line(*args)
                    if "no local order recorded" not in line:
                        expected_orders.append(line)
        expected_order_text = "\n".join(expected_orders) if expected_orders else "No local broker order expected"
        broker_order_text = "No app-owned open order reported by latest broker probe"
        if broker.get("error"):
            broker_order_text = f"Broker probe unavailable: {broker.get('error')}"
        elif open_orders:
            broker_order_text = "\n".join(broker_order_line(order) for order in open_orders if isinstance(order, dict))
        elif superseded_orders:
            broker_order_text = (
                f"Latest probe listed {len(superseded_orders)} app-owned order(s), but a newer local broker poll recorded "
                "their terminal state. They are not treated as open; refresh broker state to re-confirm TWS."
            )
        elif broker.get("checked_at"):
            broker_order_text = f"Checked {broker.get('checked_at')}: no app-owned open orders reported"
        else:
            broker_order_text = "No broker refresh has been run in this session yet"

        stored_execution = "No local fill recorded yet"
        if bought > 0:
            stored_execution = f"BUY fill {bought:g} @ {_format_currency(cycle.get('avg_buy_price'))}; {cycle.get('buy_filled_at') or 'time not recorded'}"
        if final_sold > 0:
            stored_execution += f"\nFinal SELL fill {final_sold:g} @ {_format_currency(cycle.get('avg_sell_price'))}; {cycle.get('sell_filled_at') or 'time not recorded'}"
        if protective_sold > 0:
            stored_execution += f"\nProtective SELL fill {protective_sold:g} @ {_format_currency(cycle.get('protective_avg_sell_price'))}; {cycle.get('protective_sell_filled_at') or 'time not recorded'}"
        broker_execution_text = "No recent broker execution visible in latest recovery probe"
        recent_execs = [row for row in (broker.get("recent_executions") or []) if isinstance(row, dict)]
        if recent_execs:
            broker_execution_text = "\n".join(
                f"{row.get('time') or '-'} {row.get('side') or '-'} {row.get('shares') or '-'} @ {row.get('price') or '-'} ref {row.get('order_ref') or row.get('order_id') or '-'}"
                for row in recent_execs[:5]
            )
        else:
            for event in reversed(events):
                message = str(event.get("message") or "")
                if any(token in message.lower() for token in ["fill", "execution", "executed"]):
                    broker_execution_text = f"Visible audit event: {_format_utc_timestamp(event.get('created_at'))} | {message}"
                    break

        inconsistent = "No inconsistency visible from the current GUI snapshot."
        recommendation = "No active recovery action."
        action_state = "inactive"
        matched_order: Optional[dict[str, Any]] = None
        guard_blocker = _blocking_cycle_message(cycle) if has_cycle else ""
        expected_guard_pause = _is_expected_guard_or_timing_blocker(guard_blocker)
        expected_strategy_wait = _is_expected_strategy_wait_message(stage, cycle.get("error_message")) if has_cycle else False
        startup_resume_required = bool(snapshot.get("startup_resume_required"))
        if stale_active_cycle and has_cycle:
            age_text = f"{stale_age_seconds / 3600.0:.1f} hours" if stale_age_seconds is not None else "an unknown age"
            inconsistent = f"Startup detected a stale active cycle; SQLite was last updated {age_text} ago."
            recommendation = "Refresh from IBKR/TWS on this Reconciliation screen, compare orders/position/executions, then use Reconcile and resume only after the local and broker state match."
            action_state = "waiting"
        elif broker.get("error"):
            inconsistent = "IBKR/TWS state has not been refreshed, so SQLite cannot be compared with broker state."
            recommendation = "Connect to IBKR/TWS, then press Refresh from IBKR/TWS."
            action_state = "waiting"
        elif not has_cycle and open_orders:
            inconsistent = "TWS reports app-owned open order(s), but SQLite has no active cycle."
            recommendation = "Do not start a new strategy. Cancel the app-owned order in TWS or use app recovery cancellation after verifying the OrderRef."
            action_state = "risk"
        elif not has_cycle:
            inconsistent = "No active SQLite cycle and no app-owned broker order is visible."
            recommendation = "No recovery action is needed."
            action_state = "success"
        elif _is_expected_operator_stop_message(cycle.get("error_message")):
            if open_orders:
                inconsistent = "Strategy was stopped locally, and TWS still reports app-owned open order(s)."
                recommendation = "This is a caution state, not an automatic error. Verify the OrderRef, then deliberately cancel, leave, or reconcile the app-owned order."
                action_state = "waiting"
            elif open_qty > 0:
                inconsistent = "Strategy was stopped locally and SQLite still shows an app-bought unsold position."
                recommendation = "Use Stop strategy to market-close only if you want the app to sell the remaining app-owned quantity."
                action_state = "waiting"
            else:
                inconsistent = "Strategy was intentionally stopped locally. No app-owned order or unsold app position is visible."
                recommendation = "No recovery action is required. Start a new strategy only when intended."
                action_state = "success"
        elif stage == Stage.STOPPED.value:
            message = str(cycle.get("error_message") or "").strip()
            if open_orders:
                inconsistent = "The local cycle is stopped, and TWS still reports app-owned open order(s)."
                recommendation = "This is a caution state, not an automatic error. Verify the OrderRef, then deliberately cancel, leave, or reconcile the app-owned order."
                action_state = "waiting"
            elif open_qty > 0:
                inconsistent = f"The local cycle is stopped, and SQLite still shows {open_qty:g} app-bought unsold share(s)."
                recommendation = "This is a caution state, not an automatic error. Use Stop strategy / market-close only if you want the app to sell the remaining app-owned quantity."
                action_state = "waiting"
            elif recovery_required and not (_is_expected_operator_stop_message(message) or _is_handled_recovery_stop_message(message)):
                inconsistent = message or "Manual recovery flag is set on the stopped cycle."
                recommendation = "Refresh from IBKR/TWS and reconcile the stopped cycle before starting a new cycle."
                action_state = "risk"
            elif _is_expected_operator_stop_message(message):
                inconsistent = "Strategy was stopped locally by the operator before any app-owned broker order or position needed recovery."
                recommendation = "No recovery action is needed. You can start a new cycle when the normal guards allow trading."
                action_state = "success"
            elif _is_handled_recovery_stop_message(message):
                inconsistent = "The stopped cycle was marked manually handled by the operator."
                recommendation = "No app-owned order or unsold app position is visible in SQLite/TWS. No further recovery action is shown."
                action_state = "success"
            elif message and _is_real_recovery_error_message(message):
                inconsistent = message
                recommendation = "Refresh from IBKR/TWS and reconcile the stopped cycle before starting a new cycle."
                action_state = "risk"
            else:
                inconsistent = "Stopped cycle is locally safe: no app-owned order or unsold app position is visible."
                recommendation = "No recovery action is needed unless the stopped state is unexpected."
                action_state = "success"
        elif stage == Stage.CYCLE_COMPLETE.value:
            if open_orders:
                inconsistent = "Cycle is complete, but TWS still reports app-owned open order(s)."
                recommendation = "Verify TWS and cancel or leave the app-owned order deliberately before starting again."
                action_state = "risk"
            elif open_qty > 0:
                inconsistent = "Cycle is complete, but SQLite still shows app-bought unsold quantity."
                recommendation = "Reconcile executions/position before starting another cycle."
                action_state = "risk"
            else:
                inconsistent = "Completed cycle is locally safe: no app-owned order or unsold app position is visible."
                recommendation = "No recovery action is required."
                action_state = "success"
        elif cycle.get("error_message"):
            inconsistent = str(cycle.get("error_message"))
            if expected_guard_pause:
                recommendation = (
                    "Trading is paused by a configured guard/session condition. "
                    "No manual recovery action is required unless the setting or RTH state is unexpected."
                )
                action_state = "waiting"
            elif expected_strategy_wait:
                recommendation = (
                    "This is normal strategy wait/status text, not a recovery error. "
                    "Continue monitoring; refresh broker state only if TWS does not match the active stage."
                )
                action_state = "waiting"
            else:
                recommendation = "Refresh from IBKR/TWS, compare orders/position, then choose cancel, stop, market-close, or mark manually handled."
                action_state = "risk"
        elif recovery_required:
            inconsistent = "Manual recovery flag is set."
            recommendation = "Refresh from IBKR/TWS and reconcile TWS orders, executions, and position before resuming."
            action_state = "risk"
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            matched_order = first_matching_order(cycle.get("buy_order_ref"))
            if matched_order:
                inconsistent = "No inconsistency shown; broker BUY trailing-stop order is open and waiting."
                recommendation = "Continue monitoring the BUY order. The next transition is Stage 3 after TWS reports a BUY fill."
                action_state = "active"
            elif bought > 0:
                inconsistent = "Local BUY fill exists, but the cycle has not advanced past BUY_TRAIL_ACTIVE."
                recommendation = "Refresh from IBKR/TWS. If recovered execution is confirmed, resume monitoring; otherwise use manual review."
                action_state = "waiting"
            else:
                if broker_probe_stale_for(cycle.get("buy_filled_at"), cycle.get("updated_at")):
                    inconsistent = "SQLite expects a BUY order, but the broker probe is missing or older than the local order state."
                    recommendation = "Refresh from IBKR/TWS before treating this as a recovery error."
                    action_state = "waiting"
                else:
                    inconsistent = "SQLite expects a BUY order, but no matching app-owned BUY order is visible in TWS."
                    recommendation = "Do not start a new cycle. Verify TWS; cancel any stray app order or mark manually handled only after reconciliation."
                    action_state = "risk"
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            protective_ref = cycle.get("protective_sell_order_ref")
            if protective_ref and not first_matching_order(protective_ref) and status_working(protective_ref, cycle.get("protective_sell_status"), cycle.get("protective_sell_filled_qty")):
                if broker_probe_stale_for(cycle.get("protective_sell_filled_at"), cycle.get("updated_at")):
                    inconsistent = "SQLite expects a protective SELL order, but the broker probe is missing or older than the local protective-order state."
                    recommendation = "Refresh from IBKR/TWS before treating this as a recovery error."
                    action_state = "waiting"
                else:
                    inconsistent = "SQLite expects a protective SELL order, but TWS does not show that app-owned order open."
                    recommendation = "Refresh from IBKR/TWS; if no protective execution/order exists, manually decide whether to market-close or mark handled."
                    action_state = "risk"
            elif broker_position is not None and open_qty > 0 and broker_position < min(1.0, open_qty):
                inconsistent = "SQLite expects an app-owned long position, but the broker position is lower."
                recommendation = "Do not submit new SELL orders. Reconcile executions/position manually, then mark handled if already closed."
                action_state = "risk"
            else:
                inconsistent = "No inconsistency shown; app is waiting for the minimum-profit trigger and/or monitoring protective SELL."
                recommendation = "Continue monitoring. The next transition is final SELL trailing-stop submission after the minimum-profit trigger passes."
                action_state = "active"
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            matched_order = first_matching_order(cycle.get("sell_order_ref"))
            if matched_order:
                inconsistent = "No inconsistency shown; final SELL trailing-stop order is open."
                recommendation = "Continue monitoring the SELL order until TWS reports fill/cancel/error."
                action_state = "active"
            elif open_qty <= 0:
                inconsistent = "SQLite shows no remaining app-owned quantity, but the cycle is still in SELL_TRAIL_ACTIVE."
                recommendation = "Refresh broker executions. If the sell was handled, mark manually handled or wait for recovery to record the fill."
                action_state = "waiting"
            else:
                if broker_probe_stale_for(cycle.get("sell_filled_at"), cycle.get("updated_at")):
                    inconsistent = "SQLite expects a SELL order for an open position, but the broker probe is missing or older than the local SELL-order state."
                    recommendation = "Refresh from IBKR/TWS before treating this as a recovery error. The running app can continue monitoring the submitted order."
                    action_state = "waiting"
                else:
                    inconsistent = "SQLite expects a SELL order for an open position, but no matching app-owned SELL order is visible in TWS."
                    recommendation = "Do not start a new cycle. Verify position and consider market-close or manual handling."
                    action_state = "risk"
        elif stage == Stage.WAIT_INITIAL_DROP.value:
            if guard_blocker and expected_guard_pause:
                inconsistent = guard_blocker
                recommendation = (
                    "Trading is intentionally paused by a guard/session setting. "
                    "The app can continue monitoring; no recovery action is required unless this blocker is unexpected."
                )
                action_state = "waiting"
            else:
                inconsistent = "No inconsistency shown; no broker order should exist before initial-drop trigger."
                recommendation = "Continue monitoring price data. Cancel any unexpected app-owned TWS order before continuing."
                action_state = "active" if not open_orders else "risk"
        else:
            inconsistent = "No active trading-stage inconsistency is visible."
            recommendation = "Refresh from IBKR/TWS if this does not match TWS."
            action_state = "inactive"

        startup_resume_only = bool(
            startup_resume_required
            and has_cycle
            and not recovery_required
            and not open_orders
            and open_qty <= 0
            and action_state in {"active", "waiting"}
        )
        if startup_resume_only:
            recommendation = (
                "Stored cycle is paused after a previous exit/resume-later path. "
                "Click 4. Start strategy on the Live strategy tab to resume monitoring/recovery. "
                "Recovery buttons remain disabled because no broker mismatch, app-owned open order, or app-bought unsold position is visible."
            )
            action_state = "waiting"

        refresh_required_for_resolution = bool(
            not refresh_current
            and not startup_resume_only
            and not (expected_guard_pause or expected_strategy_wait)
            and (recovery_required or open_orders or open_qty > 0 or action_state == "risk")
        )
        if refresh_required_for_resolution:
            recommendation = (
                "Step 1: Refresh from IBKR/TWS. Then compare SQLite with the current broker orders, "
                "position, and executions before choosing a resolution action."
            )

        if hasattr(self, "recovery_refresh_status_label"):
            refresh_state = str(refresh.get("state") or "not_refreshed")
            checked_at = refresh.get("checked_at")
            last_successful = refresh.get("last_successful_checked_at")
            reason = str(refresh.get("reason") or "")
            if refresh_state == "current":
                refresh_text = f"Broker state: Current - refreshed {_format_utc_timestamp(checked_at)}."
            elif refresh_state == "not_refreshed":
                refresh_text = "Broker state: Not refreshed - press Refresh from IBKR/TWS before resolving a problem."
            else:
                prefix = "Refresh failed" if refresh_state == "failed" else "Stale"
                attempted = f" at {_format_utc_timestamp(checked_at)}" if checked_at else ""
                refresh_text = f"Broker state: {prefix}{attempted} - {reason}"
                if last_successful:
                    refresh_text += f" Last successful refresh: {_format_utc_timestamp(last_successful)}."
            if self.recovery_refresh_status_label.text() != refresh_text:
                self.recovery_refresh_status_label.setText(refresh_text)
            if str(self.recovery_refresh_status_label.property("state") or "") != refresh_state:
                self.recovery_refresh_status_label.setProperty("state", refresh_state)
                self.recovery_refresh_status_label.style().unpolish(self.recovery_refresh_status_label)
                self.recovery_refresh_status_label.style().polish(self.recovery_refresh_status_label)
            self.recovery_refresh_status_label.setToolTip(reason)

        if action_state == "risk":
            status_state = "PriceStatusBad"
        elif action_state == "waiting":
            status_state = "PriceStatusWarning"
        else:
            status_state = "PriceStatusGood"
        self.recovery_status_label.setObjectName(status_state)
        if action_state == "risk":
            status_text = "Recovery required - trading is paused until manually reviewed."
        elif stale_active_cycle:
            status_text = "Stale active cycle detected at startup; broker/local reconciliation is required before monitoring resumes."
        elif startup_resume_only:
            status_text = "Stored cycle is paused until 4. Start strategy is clicked; no recovery button action is required."
        elif action_state == "waiting" and stage == Stage.STOPPED.value:
            status_text = "Strategy is stopped; Recovery shows a caution state, not a red manual-review error."
        elif action_state == "waiting" and (expected_guard_pause or expected_strategy_wait):
            status_text = "Trading is in a configured guard or normal wait state; no recovery action is available."
        elif action_state == "waiting":
            status_text = "Recovery needs a broker refresh or operator confirmation before action."
        elif action_state in {"active", "success"}:
            status_text = "Recovery state is consistent with the current snapshot."
        else:
            status_text = "No recovery issue detected."
        self.recovery_status_label.setText(status_text)
        self.recovery_status_label.style().unpolish(self.recovery_status_label)
        self.recovery_status_label.style().polish(self.recovery_status_label)

        if hasattr(self, "recovery_recommendation_label"):
            self.recovery_recommendation_label.setText(f"Recommended action: {recommendation}")
            self.recovery_recommendation_label.setProperty("state", action_state)
            self.recovery_recommendation_label.style().unpolish(self.recovery_recommendation_label)
            self.recovery_recommendation_label.style().polish(self.recovery_recommendation_label)

        refresh_table_text = str(refresh.get("state") or "not_refreshed").replace("_", " ").title()
        if refresh.get("checked_at"):
            refresh_table_text += f" at {_format_utc_timestamp(refresh.get('checked_at'))}"
        rows = [
            ("Cycle", local_cycle_text, f"Probe cycle: {broker.get('cycle_id') or 'none'}", "The probe must match the active local cycle before broker-dependent actions are enabled."),
            ("Broker refresh", "Local reconciliation signature captured" if broker.get("local_cycle_signature") is not None else "No local signature captured", refresh_table_text, refresh.get("reason") or "Refresh from IBKR/TWS before resolving a problem."),
            ("Recovery confidence", recovery_confidence, "fully_reconciled / broker_partially_checked / local_state_only / manual_review_required", "Use lower-confidence states as a prompt to refresh from IBKR/TWS or review manually."),
            ("Stale startup cycle", "yes" if stale_active_cycle else "no", f"age seconds: {snapshot.get('stale_active_cycle_age_seconds') or '-'}", "If yes, refresh from IBKR/TWS and use Reconcile and resume only after comparison."),
            ("Orders", expected_order_text, broker_order_text, "Only app-owned OrderRefs should be cancelled or reconciled by this app."),
            ("Position", local_position_text, broker_position_text, "If broker position is lower than SQLite, stop and reconcile manually."),
            ("Executions", stored_execution, broker_execution_text, "Recent broker executions can explain a missing open order."),
            ("Inconsistency", inconsistent, broker.get("error") or "Latest broker probe loaded" if broker.get("checked_at") else "No broker probe yet", recommendation),
        ]
        if hasattr(self, "recovery_compare_table"):
            self.recovery_compare_table.setRowCount(len(rows))
            self.recovery_compare_table.setColumnCount(4)
            for row_idx, row_values in enumerate(rows):
                for col_idx, value in enumerate(row_values):
                    item = QTableWidgetItem(str(value))
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if row_values[0] == "Inconsistency":
                        if action_state == "risk":
                            item.setBackground(QColor("#fee2e2"))
                        elif action_state == "waiting":
                            item.setBackground(QColor("#fffbeb"))
                        elif action_state in {"success", "active"}:
                            item.setBackground(QColor("#ecfdf5"))
                    self.recovery_compare_table.setItem(row_idx, col_idx, item)
            _auto_size_table_columns(self.recovery_compare_table, minimum=72, maximum=460, last_maximum=620)
            _fit_table_height_to_rows(self.recovery_compare_table, min_rows=5, max_visible_rows=7, min_height=220, max_fit_height=360)

        has_working_local_order = False
        if has_cycle:
            has_working_local_order = any([
                status_working(cycle.get("buy_order_ref"), cycle.get("buy_status"), 0),
                status_working(cycle.get("protective_sell_order_ref"), cycle.get("protective_sell_status"), cycle.get("protective_sell_filled_qty")),
                status_working(cycle.get("sell_order_ref"), cycle.get("sell_status"), cycle.get("sell_filled_qty")),
            ])
        terminal_safe_stage = stage in {"", Stage.IDLE.value, Stage.STOPPED.value, Stage.CYCLE_COMPLETE.value, "No active cycle"}
        permissions = _recovery_action_permissions(
            has_cycle=has_cycle,
            startup_resume_required=startup_resume_required,
            startup_resume_only=startup_resume_only,
            recovery_required=recovery_required,
            action_state=action_state,
            expected_non_recovery_wait=expected_guard_pause or expected_strategy_wait,
            open_order_count=len(open_orders),
            has_working_local_order=has_working_local_order,
            open_qty=open_qty,
            terminal_safe_stage=terminal_safe_stage,
            broker_refresh_current=refresh_current,
        )
        no_recovery_action_needed = permissions["no_recovery_action_needed"]
        can_resume = permissions["can_resume"]
        self.recovery_resume_btn.setEnabled(can_resume)
        if can_resume:
            resume_tooltip = "Reruns the existing startup/reconnect recovery path and resumes monitoring after the current broker facts have been reviewed."
        elif permissions["resume_supported"] and not refresh_current:
            resume_tooltip = "Refresh from IBKR/TWS before reconciling and resuming."
        elif startup_resume_required:
            resume_tooltip = "Use 4. Start strategy on the Live strategy tab to resume this stored cycle."
        elif permissions["ordinary_wait_only"]:
            resume_tooltip = "A configured trading guard or normal strategy wait is not a recovery state; Reconcile and resume is intentionally unavailable."
        else:
            resume_tooltip = "No reconciliation resume action is needed for the current snapshot."
        self.recovery_resume_btn.setToolTip(resume_tooltip)

        can_stop_cycle = permissions["can_stop_cycle"]
        self.recovery_stop_cycle_btn.setEnabled(can_stop_cycle)
        self.recovery_stop_cycle_btn.setToolTip(
            "Sets the existing stop-after-current-cycle flag for the active SQLite cycle; it does not cancel working broker orders."
            if can_stop_cycle
            else "Stop-after-current-cycle is not a recovery action for the current safe snapshot."
        )

        can_cancel_recovery_order = permissions["can_cancel_order"]
        self.recovery_cancel_app_order_btn.setEnabled(can_cancel_recovery_order)
        if can_cancel_recovery_order:
            cancel_tooltip = "Cancels app-owned open orders visible in the active cycle and current broker probe. It will not touch manual TWS orders."
        elif permissions["cancel_supported"] and not refresh_current:
            cancel_tooltip = "Refresh from IBKR/TWS before cancelling visible app-owned orders."
        else:
            cancel_tooltip = "No app-owned open order is visible to cancel."
        self.recovery_cancel_app_order_btn.setToolTip(cancel_tooltip)

        can_market_close = permissions["can_market_close"]
        if hasattr(self, "recovery_sell_market_btn"):
            self.recovery_sell_market_btn.setEnabled(can_market_close)
            if can_market_close:
                market_close_tooltip = f"Submits a SELL market order for the {open_qty:g} app-bought unsold share(s) after app-owned SELL orders are no longer working."
            elif permissions["market_close_supported"] and not refresh_current:
                market_close_tooltip = "Refresh from IBKR/TWS before submitting a market SELL for the app-owned position."
            else:
                market_close_tooltip = "No app-bought unsold quantity is visible in SQLite for the active cycle."
            self.recovery_sell_market_btn.setToolTip(market_close_tooltip)

        can_mark_manual = permissions["can_mark_manual"]
        self.recovery_mark_manual_btn.setEnabled(can_mark_manual)
        if can_mark_manual and refresh_current:
            manual_tooltip = "Stops the active SQLite cycle as manually handled after operator confirmation. It does not send broker orders."
        elif can_mark_manual:
            manual_tooltip = "Manual override remains available without a current refresh only after you independently verify TWS orders, executions, and position."
        else:
            manual_tooltip = "Manual handling is not needed for the current safe snapshot."
        self.recovery_mark_manual_btn.setToolTip(manual_tooltip)

        self.recovery_refresh_broker_btn.setEnabled(True)
        self.recovery_refresh_broker_btn.setToolTip(
            "Reads current app-owned TWS orders, account position, and recent executions. It does not submit, modify, or cancel broker orders."
        )
        if hasattr(self, "recovery_export_bundle_btn"):
            self.recovery_export_bundle_btn.setEnabled(True)
            self.recovery_export_bundle_btn.setToolTip("Creates a ZIP with SQLite backup, readable logs, recent events, broker-event records, and the current reconciliation snapshot.")

        if hasattr(self, "recovery_leave_orders_btn"):
            can_leave_orders = permissions["can_leave_orders"]
            self.recovery_leave_orders_btn.setEnabled(can_leave_orders)
            if can_leave_orders:
                leave_tooltip = "Leaves visible app-owned TWS orders working so this app can recover them later."
            elif permissions["cancel_supported"] and not refresh_current:
                leave_tooltip = "Refresh from IBKR/TWS before choosing to leave app-owned orders working."
            else:
                leave_tooltip = "No visible app-owned open order needs a leave-working decision."
            self.recovery_leave_orders_btn.setToolTip(leave_tooltip)

        if hasattr(self, "recovery_button_hint_label"):
            if permissions["ordinary_wait_only"]:
                hint_text = (
                    "The strategy is paused by a configured trading guard or a normal wait condition, not by a recovery fault. "
                    "Resolution actions are disabled; Refresh from IBKR/TWS and audit export remain available."
                )
            elif no_recovery_action_needed:
                hint_text = (
                    "No recovery action is available because the current snapshot is safe. "
                    "Refresh from IBKR/TWS remains available after any external TWS change."
                )
            elif not refresh_current:
                hint_text = (
                    "Step 1 is required: refresh current IBKR/TWS facts before Reconcile and resume, cancellation, market SELL, or Leave orders working. "
                    "Mark manually handled remains an explicit manual override after independent TWS verification."
                )
            else:
                hint_text = (
                    "The broker refresh is current. Compare the rows above before acting. Cancel/close actions only operate on app-owned OrderRefs; "
                    "Mark manually handled sends no broker order."
                )
            if self.recovery_button_hint_label.text() != hint_text:
                self.recovery_button_hint_label.setText(hint_text)

        lines = [
            f"Status: {snapshot.get('status', '-')}",
            f"Connected: {snapshot.get('connected')}",
            f"Recovery required: {recovery_required}",
            f"Recovery confidence: {recovery_confidence}",
            f"Stale active cycle: {stale_active_cycle}",
            f"Stale active cycle age seconds: {snapshot.get('stale_active_cycle_age_seconds') or '-'}",
            f"DB: {snapshot.get('db_path', '-')}",
            f"Broker refresh state: {refresh.get('state') or 'not_refreshed'}",
            f"Broker refresh current: {refresh_current}",
            f"Broker refresh reason: {refresh.get('reason') or '-'}",
            f"Broker probe checked_at: {broker.get('checked_at') or 'not refreshed'}",
            f"Last successful broker refresh: {broker.get('last_successful_checked_at') or 'none'}",
            f"Broker probe error: {broker.get('error') or '-'}",
            "",
            "Active cycle:",
        ]
        if cycle:
            for key in [
                "ticker", "stage", "cycle_number", "account", "error_message",
                "buy_order_ref", "buy_order_id", "buy_perm_id", "buy_status",
                "protective_sell_order_ref", "protective_sell_order_id", "protective_sell_perm_id", "protective_sell_status",
                "sell_order_ref", "sell_order_id", "sell_perm_id", "sell_status",
                "quantity", "buy_filled_qty", "avg_buy_price", "sell_filled_qty", "avg_sell_price",
                "protective_sell_filled_qty", "protective_avg_sell_price", "last_price", "updated_at",
            ]:
                lines.append(f"  {key}: {cycle.get(key)}")
        else:
            lines.append("  none")
        lines.append("")
        lines.append("Broker recovery probe:")
        if broker:
            for key in [
                "checked_at",
                "last_successful_checked_at",
                "connected",
                "upstream_connected",
                "cycle_id",
                "cycle_ticker",
                "position_size",
                "position_account",
                "position_error",
                "recent_executions_error",
                "order_state_updated_at",
                "invalidated_at",
                "invalidation_reason",
            ]:
                if key in broker:
                    lines.append(f"  {key}: {broker.get(key)}")
            lines.append("  open_app_orders:")
            if open_orders:
                for order in open_orders:
                    lines.append(f"    - {broker_order_line(order)}")
            else:
                lines.append("    none")
            if superseded_orders:
                lines.append("  probe_orders_superseded_by_newer_local_terminal_poll:")
                for order in superseded_orders:
                    lines.append(f"    - {broker_order_line(order)}")
            lines.append("  recent_executions:")
            if recent_execs:
                for row in recent_execs[:10]:
                    lines.append(f"    - {row}")
            else:
                lines.append("    none")
        else:
            lines.append("  no broker recovery probe yet")
        lines.append("")
        lines.append("Recent audit events:")
        for event in events[:25]:
            lines.append(f"  {_format_utc_timestamp(event.get('created_at'))} [{event.get('level')}] {event.get('ticker') or ''} {event.get('message')}")
        self._set_recovery_details_text_preserve_scroll("\n".join(lines))

    def _set_recovery_details_text_preserve_scroll(self, text: str) -> None:
        """Update the black Recovery audit/details log without snapping to top."""
        bar = self.recovery_details.verticalScrollBar()
        old_max = bar.maximum() if bar is not None else 0
        old_value = bar.value() if bar is not None else 0
        at_bottom = old_max > 0 and old_value >= old_max - 2
        self.recovery_details.setPlainText(text)
        if bar is None:
            return
        new_max = bar.maximum()
        if old_max <= 0 and old_value <= 0:
            return
        if at_bottom:
            target_value = new_max
        elif old_max > 0:
            ratio = old_value / float(old_max)
            target_value = int(round(ratio * new_max))
        else:
            target_value = min(old_value, new_max)
        bar.setValue(target_value)
        # QTextEdit can recalculate its scroll range after the current event
        # returns. Reapply once so a periodic snapshot does not snap an operator
        # who is reading the lower part of the recovery log back to the top.
        QTimer.singleShot(0, lambda b=bar, v=target_value: b.setValue(min(v, b.maximum())))

    def _update_history_summary(self, summary: dict[str, Any]) -> None:
        if not hasattr(self, "history_summary_cards"):
            return
        mapping = {
            "Total cycles": summary.get("cycles"),
            "Win rate": summary.get("win_rate_pct"),
            "Average net %": summary.get("avg_net_pct"),
            "Median net %": summary.get("median_net_pct"),
            "Best net P/L": summary.get("best_net_pnl"),
            "Worst net P/L": summary.get("worst_net_pnl"),
            "Total net P/L": summary.get("total_net_pnl"),
            "Total commissions": summary.get("total_commissions"),
            "Max losing streak": summary.get("max_consecutive_losses"),
            "Avg hold": summary.get("avg_holding_minutes"),
            "Max drawdown": summary.get("max_completed_drawdown"),
        }
        for title, value in mapping.items():
            card = self.history_summary_cards.get(title)
            if card is None:
                continue
            if value is None:
                card.value.setText("-")
            elif "%" in title:
                card.value.setText(f"{float(value):.2f}%")
            elif "P/L" in title or "commissions" in title.lower() or "drawdown" in title.lower():
                card.value.setText(_format_currency(value))
            elif "hold" in title.lower():
                minutes = float(value)
                card.value.setText(f"{minutes:.1f} min" if minutes < 120 else f"{minutes/60.0:.1f} h")
            else:
                card.value.setText(str(int(value)) if isinstance(value, (int, float)) else str(value))

    def _apply_snapshot_to_inputs(self, connection: dict[str, Any], strategy: dict[str, Any]) -> None:
        # Initial hydration only. After this, editable fields remain controlled
        # by the user. The flag also prevents signal-driven autosave while the
        # app is applying values loaded from SQLite.
        self._applying_snapshot_to_inputs = True
        try:
            platform = str(connection.get("platform", GATEWAY_PLATFORM) or GATEWAY_PLATFORM).lower()
            for i in range(self.platform_combo.count()):
                if self.platform_combo.itemData(i) == platform:
                    self.platform_combo.setCurrentIndex(i)
                    break
            self.host_edit.setText(str(connection.get("host", self.host_edit.text())))
            self.port_spin.setValue(int(connection.get("port", self.port_spin.value())))
            self.client_spin.setValue(int(connection.get("client_id", self.client_spin.value())))
            self.account_edit.setText(str(connection.get("account", self.account_edit.text()) or ""))
            self.platform_path_edit.setText(str(connection.get("platform_path", self.platform_path_edit.text()) or ""))
            mode = str(connection.get("trading_mode", self._manual_trading_mode) or "live").lower()
            if mode not in {"paper", "live"}:
                mode = "live"
            self._manual_trading_mode = mode
            key = profile_key_for(platform, mode, self.host_edit.text().strip(), self.port_spin.value())
            for i in range(self.profile_combo.count()):
                data = self.profile_combo.itemData(i) or {}
                if isinstance(data, dict) and data.get("key") == key:
                    self.profile_combo.setCurrentIndex(i)
                    break
            # Default every app session to Auto best available. Users can still
            # change the selector manually after startup.
            mdt = 0
            for i in range(self.market_data_combo.count()):
                if int(self.market_data_combo.itemData(i)) == mdt:
                    self.market_data_combo.setCurrentIndex(i)
                    break

            self.ticker_edit.setText(str(strategy.get("ticker", self.ticker_edit.text()) or ""))
            self.primary_exchange_edit.setText(str(strategy.get("primary_exchange", self.primary_exchange_edit.text()) or ""))
            self._set_selected_contract_con_id(strategy.get("contract_con_id"))
            self.investment_spin.setValue(float(strategy.get("investment_amount", self.investment_spin.value())))
            self.initial_drop_spin.setValue(float(strategy.get("initial_drop_pct", self.initial_drop_spin.value())))
            self.buy_rebound_spin.setValue(float(strategy.get("buy_rebound_trail_pct", self.buy_rebound_spin.value())))
            self.rise_trigger_spin.setValue(float(strategy.get("rise_trigger_pct", self.rise_trigger_spin.value())))
            self.sell_trail_spin.setValue(float(strategy.get("sell_trailing_stop_pct", self.sell_trail_spin.value())))
            self.atr_adaptive_check.setChecked(bool(strategy.get("atr_adaptive_enabled", self.atr_adaptive_check.isChecked())))
            self.atr_min_profit_adaptive_check.setChecked(bool(strategy.get("atr_adapt_minimum_profit_enabled", self.atr_min_profit_adaptive_check.isChecked())))
            self.atr_block_until_ready_check.setChecked(bool(strategy.get("atr_block_new_buy_until_ready", self.atr_block_until_ready_check.isChecked())))
            self.atr_protective_sell_adaptive_check.setChecked(bool(strategy.get("atr_adapt_protective_sell_enabled", self.atr_protective_sell_adaptive_check.isChecked())))
            self.atr_protective_sell_mult_spin.setValue(float(strategy.get("atr_protective_sell_multiplier", self.atr_protective_sell_mult_spin.value())))
            self.atr_period_spin.setValue(int(strategy.get("atr_period", self.atr_period_spin.value())))
            self.atr_bar_seconds_spin.setValue(int(strategy.get("atr_bar_seconds", self.atr_bar_seconds_spin.value())))
            self.atr_initial_drop_mult_spin.setValue(float(strategy.get("atr_initial_drop_multiplier", self.atr_initial_drop_mult_spin.value())))
            self.atr_buy_rebound_mult_spin.setValue(float(strategy.get("atr_buy_rebound_multiplier", self.atr_buy_rebound_mult_spin.value())))
            self.atr_min_profit_mult_spin.setValue(float(strategy.get("atr_minimum_profit_multiplier", self.atr_min_profit_mult_spin.value())))
            self.atr_sell_trail_mult_spin.setValue(float(strategy.get("atr_sell_trail_multiplier", self.atr_sell_trail_mult_spin.value())))
            self.atr_min_pct_spin.setValue(float(strategy.get("atr_min_pct", self.atr_min_pct_spin.value())))
            self.atr_max_pct_spin.setValue(float(strategy.get("atr_max_pct", self.atr_max_pct_spin.value())))
            self.protective_sell_check.setChecked(bool(strategy.get("protective_sell_enabled", self.protective_sell_check.isChecked())))
            self.protective_sell_trail_spin.setValue(float(strategy.get("protective_sell_trailing_stop_pct", self.protective_sell_trail_spin.value())))
            self.slippage_buffer_check.setChecked(bool(strategy.get("slippage_buffer_enabled", self.slippage_buffer_check.isChecked())))
            self.slippage_buffer_spin.setValue(float(strategy.get("slippage_buffer_pct", self.slippage_buffer_spin.value())))
            self.hard_risk_limits_check.setChecked(bool(strategy.get("hard_risk_limits_enabled", self.hard_risk_limits_check.isChecked())))
            self.max_daily_loss_ticker_spin.setValue(float(strategy.get("max_daily_loss_ticker", self.max_daily_loss_ticker_spin.value())))
            self.max_daily_loss_total_spin.setValue(float(strategy.get("max_daily_loss_total", self.max_daily_loss_total_spin.value())))
            self.max_cycles_ticker_day_spin.setValue(int(strategy.get("max_cycles_per_ticker_day", self.max_cycles_ticker_day_spin.value())))
            self.max_consecutive_losses_spin.setValue(int(strategy.get("max_consecutive_losses", self.max_consecutive_losses_spin.value())))
            self.max_spread_pct_spin.setValue(float(strategy.get("max_spread_pct", self.max_spread_pct_spin.value())))
            self.min_trade_price_spin.setValue(float(strategy.get("min_trade_price", self.min_trade_price_spin.value())))
            self.max_gap_pct_spin.setValue(float(strategy.get("max_gap_from_prev_close_pct", self.max_gap_pct_spin.value())))
            self.block_delayed_live_check.setChecked(bool(strategy.get("block_delayed_data_in_live", self.block_delayed_live_check.isChecked())))
            self.what_if_check.setChecked(bool(strategy.get("what_if_check_enabled", self.what_if_check.isChecked())))
            self.stale_data_guard_check.setChecked(bool(strategy.get("stale_data_guard_enabled", self.stale_data_guard_check.isChecked())))
            self.max_price_age_spin.setValue(float(strategy.get("max_selected_price_age_seconds", self.max_price_age_spin.value())))
            self.max_bidask_age_spin.setValue(float(strategy.get("max_bid_ask_age_seconds", self.max_bidask_age_spin.value())))
            self.max_rth_age_spin.setValue(float(strategy.get("max_rth_status_age_seconds", self.max_rth_age_spin.value())))
            self.volatility_filter_check.setChecked(bool(strategy.get("volatility_filter_enabled", self.volatility_filter_check.isChecked())))
            self.volatility_window_spin.setValue(int(strategy.get("volatility_window_seconds", self.volatility_window_spin.value())))
            self.max_recent_move_spin.setValue(float(strategy.get("max_recent_price_move_pct", self.max_recent_move_spin.value())))
            self.session_timing_guard_check.setChecked(bool(strategy.get("session_timing_guard_enabled", self.session_timing_guard_check.isChecked())))
            self.no_new_buy_first_spin.setValue(int(strategy.get("no_new_buy_first_minutes", self.no_new_buy_first_spin.value())))
            self.no_new_buy_last_spin.setValue(int(strategy.get("no_new_buy_last_minutes", self.no_new_buy_last_spin.value())))
            self.cancel_buy_before_close_spin.setValue(int(strategy.get("cancel_buy_before_close_minutes", self.cancel_buy_before_close_spin.value())))
            self.reinvest_check.setChecked(bool(strategy.get("reinvest_profits", self.reinvest_check.isChecked())))
            self.auto_repeat_check.setChecked(bool(strategy.get("auto_repeat", self.auto_repeat_check.isChecked())))
        finally:
            self._applying_snapshot_to_inputs = False
        self._apply_profit_guard_bounds()
        self._update_zero_disabled_indicators()
        self._update_dynamic_graphs()


    def _is_active_stage(self, stage: Optional[str]) -> bool:
        return stage in {
            Stage.WAIT_INITIAL_DROP.value,
            Stage.BUY_TRAIL_ACTIVE.value,
            Stage.WAIT_RISE_TRIGGER.value,
            Stage.SELL_TRAIL_ACTIVE.value,
        }

    def _set_widgets_enabled(self, widgets: list[Any], enabled: bool, reason: Optional[str] = None) -> None:
        lock_reason = reason or getattr(self, "_current_lock_tooltip", "Locked for current stage.")
        manual_ids = getattr(self, "_manual_input_lock_widget_ids", set())
        manual_lock = bool(getattr(self, "_manual_input_lock_enabled", False))
        for widget in widgets:
            try:
                if widget.property("baseToolTip") is None:
                    widget.setProperty("baseToolTip", widget.toolTip())
                locked_by_operator = bool(manual_lock and id(widget) in manual_ids)
                effective_enabled = bool(enabled) and not locked_by_operator
                stage_locked = (not bool(enabled)) and not locked_by_operator
                enabled_changed = widget.isEnabled() != effective_enabled
                stage_lock_changed = bool(widget.property("lockedForStage")) != stage_locked
                manual_lock_changed = bool(widget.property("manualInputLocked")) != locked_by_operator
                if enabled_changed:
                    widget.setEnabled(effective_enabled)
                if stage_lock_changed:
                    widget.setProperty("lockedForStage", stage_locked)
                if manual_lock_changed:
                    widget.setProperty("manualInputLocked", locked_by_operator)
                if locked_by_operator:
                    tooltip = "Input lock is on. Toggle the lock in the top status bar to edit configuration values."
                elif enabled:
                    tooltip = str(widget.property("baseToolTip") or "")
                else:
                    tooltip = f"Locked for current stage. {lock_reason}"
                if widget.toolTip() != tooltip:
                    widget.setToolTip(tooltip)
                if enabled_changed or stage_lock_changed or manual_lock_changed:
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
            except Exception:
                pass

    def _update_input_locks(self, stage: Optional[str]) -> None:
        if not hasattr(self, "ticker_edit"):
            return
        active = self._is_active_stage(stage)
        lock_reasons = {
            Stage.WAIT_INITIAL_DROP.value: "The cycle is active; ticker and connection identity are fixed for recovery and order ownership.",
            Stage.BUY_TRAIL_ACTIVE.value: "The BUY trailing-stop order has already been submitted to IBKR/TWS. Locked values will apply to the next cycle or next eligible stage.",
            Stage.WAIT_RISE_TRIGGER.value: "A BUY fill has been reported. Locked values would require replacing submitted or position-dependent order state.",
            Stage.SELL_TRAIL_ACTIVE.value: "The SELL trailing-stop order is already working in IBKR/TWS. Locked values will apply to the next cycle.",
        }
        self._current_lock_tooltip = lock_reasons.get(stage, "Fields lock automatically when changing them would require replacing an active native order.")

        # Connection identity is locked while a cycle is active. Market-data mode
        # remains editable because it only changes the quote feed used by the app.
        # The Start-platform helper stays enabled so the user can relaunch TWS/IB
        # Gateway after a daily restart or Windows restart.
        self._set_widgets_enabled([
            self.profile_combo,
            self.platform_combo,
            self.host_edit,
            self.port_spin,
            self.client_spin,
            self.account_edit,
        ], not active)
        self._set_widgets_enabled([self.market_data_combo, self.platform_path_edit, self.browse_platform_btn], True)
        self.start_platform_btn.setEnabled(True)

        # Contract identity is fixed after Start. Changing ticker/conId mid-cycle
        # would break recovery and order ownership matching.
        contract_locked = active
        self._set_widgets_enabled([
            self.ticker_edit,
            self.primary_exchange_edit,
            self.ticker_matches_combo,
            self.ticker_search_btn,
            self.ticker_use_match_btn,
            self.ticker_confirm_btn,
        ], not contract_locked)

        if stage == Stage.WAIT_INITIAL_DROP.value:
            # No native order has been submitted yet; entry and exit parameters
            # can be safely applied to the current cycle.
            self._set_widgets_enabled([self.investment_spin, self.initial_drop_spin, self.buy_rebound_spin, self.rise_trigger_spin, self.sell_trail_spin, self.protective_sell_check, self.protective_sell_trail_spin, self.slippage_buffer_check, self.slippage_buffer_spin, self.hard_risk_limits_check, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_filter_check, self.volatility_window_spin, self.max_recent_move_spin, self.session_timing_guard_check, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin, self.reinvest_check], True)
            message = "Running edit mode: ticker/connection locked. Investment, entry, exit, protective, slippage, risk, and reinvest settings update this waiting cycle."
        elif stage == Stage.BUY_TRAIL_ACTIVE.value:
            # The BUY order is already working in TWS. Exit settings are still
            # safe because no SELL order has been submitted yet.
            self._set_widgets_enabled([self.investment_spin, self.initial_drop_spin, self.buy_rebound_spin, self.hard_risk_limits_check, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_filter_check, self.volatility_window_spin, self.max_recent_move_spin, self.session_timing_guard_check, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin, self.reinvest_check], False)
            self._set_widgets_enabled([self.rise_trigger_spin, self.sell_trail_spin, self.protective_sell_check, self.protective_sell_trail_spin, self.slippage_buffer_check, self.slippage_buffer_spin], True)
            message = "Running edit mode: BUY order is active, so entry/risk sizing fields are locked. Protective, minimum profit, SELL trailing-stop, and slippage still update the later exit stage."
        elif stage == Stage.WAIT_RISE_TRIGGER.value:
            self._set_widgets_enabled([self.investment_spin, self.initial_drop_spin, self.buy_rebound_spin, self.protective_sell_check, self.protective_sell_trail_spin, self.hard_risk_limits_check, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_filter_check, self.volatility_window_spin, self.max_recent_move_spin, self.session_timing_guard_check, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin, self.reinvest_check], False)
            self._set_widgets_enabled([self.rise_trigger_spin, self.sell_trail_spin, self.slippage_buffer_check, self.slippage_buffer_spin], True)
            message = "Running edit mode: position is open. Minimum profit, final SELL trailing-stop, and slippage buffer update the waiting exit trigger; protective/risk fields are locked once a position exists."
        elif stage == Stage.SELL_TRAIL_ACTIVE.value:
            self._set_widgets_enabled([self.investment_spin, self.initial_drop_spin, self.buy_rebound_spin, self.rise_trigger_spin, self.sell_trail_spin, self.protective_sell_check, self.protective_sell_trail_spin, self.slippage_buffer_check, self.slippage_buffer_spin, self.hard_risk_limits_check, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_filter_check, self.volatility_window_spin, self.max_recent_move_spin, self.session_timing_guard_check, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin, self.reinvest_check], False)
            message = "Running edit mode: SELL trailing-stop is active in TWS. Current-cycle trading inputs are locked; Auto-repeat can still be changed."
        else:
            self._set_widgets_enabled([self.investment_spin, self.initial_drop_spin, self.buy_rebound_spin, self.rise_trigger_spin, self.sell_trail_spin, self.protective_sell_check, self.protective_sell_trail_spin, self.slippage_buffer_check, self.slippage_buffer_spin, self.hard_risk_limits_check, self.max_daily_loss_ticker_spin, self.max_daily_loss_total_spin, self.max_cycles_ticker_day_spin, self.max_consecutive_losses_spin, self.max_spread_pct_spin, self.min_trade_price_spin, self.max_gap_pct_spin, self.block_delayed_live_check, self.what_if_check, self.stale_data_guard_check, self.max_price_age_spin, self.max_bidask_age_spin, self.max_rth_age_spin, self.volatility_filter_check, self.volatility_window_spin, self.max_recent_move_spin, self.session_timing_guard_check, self.no_new_buy_first_spin, self.no_new_buy_last_spin, self.cancel_buy_before_close_spin, self.reinvest_check], True)
            message = "Fields lock automatically when changing them would require replacing an active native order."

        self._set_widgets_enabled(self._atr_config_widgets(), True)
        self._set_atr_percentage_field_state(bool(self.atr_adaptive_check.isChecked()))
        self._set_widgets_enabled([self.auto_repeat_check], True)
        # Workflow button state is owned exclusively by
        # ``_update_command_bar_states``. Re-enabling Start/Stop here would
        # overwrite active-cycle, guard, recovery, and manual-lock gating that
        # was calculated immediately before this input-lock update.
        if hasattr(self, "edit_lock_label"):
            if self.edit_lock_label.text() != message:
                self.edit_lock_label.setText(message)

    def _update_metrics(self, cycle: Optional[dict[str, Any]]) -> None:
        mapping: dict[str, Any] = {title: None for title in self.metrics}
        if not cycle:
            mapping["Stage"] = "Idle"
        else:
            mapping.update({
                "Current last price": cycle.get("last_price"),
                "Anchor price": cycle.get("anchor_price"),
                "Initial drop trigger": cycle.get("drop_trigger_price"),
                "BUY initial trailing-stop": cycle.get("buy_initial_trail_stop_price"),
                "Average buy fill": cycle.get("avg_buy_price"),
                "Minimum-profit trigger price": cycle.get("rise_trigger_price"),
                "Protective SELL stop": cycle.get("protective_sell_initial_stop_price"),
                "SELL initial trailing-stop": cycle.get("sell_initial_trail_stop_price"),
                "Stage": cycle.get("stage"),
                "Quantity": cycle.get("quantity"),
                "Buy filled qty": cycle.get("buy_filled_qty"),
                "Sell filled qty": cycle.get("sell_filled_qty"),
                "Buy order ID": cycle.get("buy_order_id"),
                "Buy permId": cycle.get("buy_perm_id"),
                "Protective order ID": cycle.get("protective_sell_order_id"),
                "Protective status": cycle.get("protective_sell_status"),
                "Sell order ID": cycle.get("sell_order_id"),
                "Sell permId": cycle.get("sell_perm_id"),
                "OrderRef": cycle.get("sell_order_ref") or cycle.get("protective_sell_order_ref") or cycle.get("buy_order_ref"),
                "Investment amount": cycle.get("investment_amount"),
                "Cycle budget": cycle.get("budget"),
                "Reinvested profit": cycle.get("reinvested_profit"),
                "Gross P/L": cycle.get("gross_pnl"),
                "Net P/L": cycle.get("net_pnl"),
            })
        for title, card in self.metrics.items():
            card.set_value(mapping.get(title))

    def _update_price_feed(self, snapshot: Optional[dict[str, Any]], poll_seconds: Any = None) -> None:
        if hasattr(self, "price_panel"):
            self.price_panel.update_data((self.current_snapshot or {}).get("active_cycle"), snapshot)
        if not hasattr(self, "price_cards"):
            return
        if not snapshot:
            for card in self.price_cards.values():
                card.set_value(None)
            if self.price_big_value.text() != "-":
                self.price_big_value.setText("-")
            if self.price_status_label.text() != "No price yet":
                self.price_status_label.setText("No price yet")
            if self.price_status_label.objectName() != "PriceStatusBad":
                self.price_status_label.setObjectName("PriceStatusBad")
                self.price_status_label.style().unpolish(self.price_status_label)
                self.price_status_label.style().polish(self.price_status_label)
            if self.price_source_label.text() != "Source: -":
                self.price_source_label.setText("Source: -")
            if self.price_updated_label.text() != "Last update: -":
                self.price_updated_label.setText("Last update: -")
            if self.price_mode_label.text() != "Requested mode: -":
                self.price_mode_label.setText("Requested mode: -")
            return

        price = snapshot.get("price")
        ok = price is not None
        price_text = _format_price(price)
        if self.price_big_value.text() != price_text:
            self.price_big_value.setText(price_text)
        status = str(snapshot.get("status") or ("Usable price" if ok else "No usable price"))
        if self.price_status_label.text() != status:
            self.price_status_label.setText(status)
        status_object = "PriceStatusGood" if ok else "PriceStatusBad"
        if self.price_status_label.objectName() != status_object:
            self.price_status_label.setObjectName(status_object)
            self.price_status_label.style().unpolish(self.price_status_label)
            self.price_status_label.style().polish(self.price_status_label)
        source_text = f"Source: {snapshot.get('source') or '-'}"
        if self.price_source_label.text() != source_text:
            self.price_source_label.setText(source_text)
        updated_at = snapshot.get("timestamp") or snapshot.get("updated_at") or snapshot.get("received_at") or snapshot.get("requested_at") or "-"
        updated_text = f"Last update ({APP_TIMEZONE_LABEL}): {_format_utc_timestamp(updated_at)}"
        if self.price_updated_label.text() != updated_text:
            self.price_updated_label.setText(updated_text)
        data_mode_labels = {0: "Auto best", 1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed frozen"}
        mode_raw = snapshot.get("requested_market_data_type") if snapshot.get("requested_market_data_type") is not None else snapshot.get("market_data_type")
        selected_raw = snapshot.get("selected_market_data_type")
        actual_raw = snapshot.get("subscription_market_data_type") or snapshot.get("actual_market_data_type")
        mode = data_mode_labels.get(mode_raw, str(mode_raw if mode_raw is not None else "-"))
        selected = data_mode_labels.get(selected_raw, str(selected_raw)) if selected_raw is not None else "not selected"
        actual = data_mode_labels.get(actual_raw, str(actual_raw)) if actual_raw is not None else "not reported"
        age = snapshot.get("age_seconds")
        next_refresh = snapshot.get("next_refresh_seconds")
        age_text = f"age {float(age):.1f}s" if isinstance(age, (int, float)) else "age -"
        if isinstance(next_refresh, (int, float)) and float(next_refresh) > 0:
            next_text = f"next {float(next_refresh):.1f}s"
        else:
            next_text = "streaming/cache read each worker tick"
        interval = f" / interval {poll_seconds}s" if poll_seconds else ""
        mode_text = f"Requested: {mode} / selected {selected} / actual {actual} / {age_text} / {next_text}{interval}"
        if self.price_mode_label.text() != mode_text:
            self.price_mode_label.setText(mode_text)
        contract = snapshot.get("contract") or {}
        if contract and hasattr(self, "contract_label"):
            primary = contract.get("primary_exchange") or "-"
            con_id = contract.get("con_id") or "-"
            local_symbol = contract.get("local_symbol") or "-"
            trading_class = contract.get("trading_class") or "-"
            contract_text = (
                f"Confirmed contract: {contract.get('ticker') or '-'} / STK / SMART / primary {primary} / USD | "
                f"conId {con_id} | local {local_symbol} | class {trading_class}"
            )
            if self.contract_label.text() != contract_text:
                self.contract_label.setText(contract_text)

        fields = snapshot.get("fields") or {}
        mapping = {
            "Last": fields.get("last"),
            "Delayed last": fields.get("delayedLast"),
            "Bid": fields.get("bid"),
            "Ask": fields.get("ask"),
            "Midpoint": fields.get("midpoint"),
            "Delayed midpoint": fields.get("delayedMidpoint"),
            "Mark": fields.get("markPrice"),
            "Delayed mark": fields.get("delayedMarkPrice"),
            "Close": fields.get("close"),
            "Delayed close": fields.get("delayedClose"),
            "Generic ticks": snapshot.get("generic_ticks"),
        }
        for title, value in mapping.items():
            self.price_cards[title].set_value(value)
        errors = snapshot.get("recent_errors") or snapshot.get("errors") or []
        error_text = str(snapshot.get("error") or "")
        if errors:
            last_error = errors[-1]
            if isinstance(last_error, dict):
                text = f"{last_error.get('code')}: {last_error.get('message')}"
            else:
                text = str(last_error)
            self.price_cards["Recent API error"].set_value(text[:160])
        elif error_text:
            self.price_cards["Recent API error"].set_value(error_text[:160])
        else:
            self.price_cards["Recent API error"].set_value(None)

    def _update_event_log(self, events: list[dict[str, Any]]) -> None:
        lines = []
        for event in events:
            ticker = event.get("ticker") or ""
            event_time = _format_utc_timestamp(event.get("created_at"))
            lines.append(f"{event_time} [{event.get('level')}] {ticker} {event.get('message')}")
        self.event_log.setPlainText("\n".join(lines))
        self.event_log.verticalScrollBar().setValue(self.event_log.verticalScrollBar().maximum())

    @staticmethod
    def _format_history_value(key: str, value: Any) -> str:
        if value is None or value == "":
            return _empty_display_for_label(key)
        pct_keys = {
            "sell_vs_buy_pct", "gross_pnl_pct", "net_pnl_pct", "buy_vs_anchor_pct",
            "initial_sell_stop_vs_buy_pct", "configured_min_profit_pct",
            "configured_initial_drop_pct", "configured_buy_rebound_pct", "configured_sell_trail_pct",
            "configured_protective_sell_trail_pct", "configured_slippage_buffer_pct",
        }
        if key in pct_keys:
            try:
                plain_pct_keys = {"configured_min_profit_pct", "configured_initial_drop_pct", "configured_buy_rebound_pct", "configured_sell_trail_pct", "configured_protective_sell_trail_pct", "configured_slippage_buffer_pct"}
                return f"{float(value):+.2f}%" if key not in plain_pct_keys else f"{float(value):.2f}%"
            except Exception:
                return str(value)
        money_keys = {"avg_buy_price", "avg_sell_price", "gross_pnl", "net_pnl", "budget", "reinvested_profit", "buy_commission", "sell_commission"}
        if key in money_keys:
            try:
                decimals = 4 if key.startswith("avg_") else 2
                return _format_currency(value, decimals=decimals)
            except Exception:
                return str(value)
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    @staticmethod
    def _history_hover_graph(row: dict[str, Any]) -> str:
        def f(name: str) -> Optional[float]:
            try:
                value = row.get(name)
                if value in (None, ""):
                    return None
                value = float(value)
                return value if value > 0 else None
            except Exception:
                return None

        anchor = f("anchor_price")
        buy = f("avg_buy_price")
        sell = f("avg_sell_price")
        drop = f("drop_trigger_price")
        buy_stop = f("buy_initial_trail_stop_price")
        min_stop = f("sell_initial_trail_stop_price")
        values = [("A", anchor), ("D", drop), ("B", buy), ("T", buy_stop), ("S", sell), ("M", min_stop)]
        valid = [(label, value) for label, value in values if value is not None]
        if len(valid) < 2:
            return ""
        min_v = min(v for _, v in valid)
        max_v = max(v for _, v in valid)
        if max_v <= min_v:
            max_v = min_v + 1.0
        width = 44
        bar = ["─"] * (width + 1)
        for label, value in valid:
            pos = int(round((value - min_v) / (max_v - min_v) * width))
            pos = max(0, min(width, pos))
            bar[pos] = label
        legend = []
        for label, name, value in [
            ("A", "anchor", anchor),
            ("D", "drop", drop),
            ("B", "buy", buy),
            ("T", "BUY stop", buy_stop),
            ("S", "sell", sell),
            ("M", "initial SELL stop", min_stop),
        ]:
            if value is not None:
                legend.append(f"{label}={name} {_format_currency(value)}")
        return "<br><span style='font-family:Consolas,monospace'>" + "".join(bar) + "</span><br>" + "<br>".join(legend)

    def _history_tooltip(self, row: dict[str, Any]) -> str:
        ticker = row.get("ticker") or ""
        cycle = row.get("cycle_number") or ""
        graph = self._history_hover_graph(row)
        return (
            f"<b>{ticker} cycle {cycle}</b><br>"
            f"Buy: {self._format_history_value('buy_filled_qty', row.get('buy_filled_qty'))} @ {self._format_history_value('avg_buy_price', row.get('avg_buy_price'))}<br>"
            f"Sell: @ {self._format_history_value('avg_sell_price', row.get('avg_sell_price'))}<br>"
            f"Sell vs buy: {self._format_history_value('sell_vs_buy_pct', row.get('sell_vs_buy_pct'))}<br>"
            f"Gross: {self._format_history_value('gross_pnl', row.get('gross_pnl'))} / {self._format_history_value('gross_pnl_pct', row.get('gross_pnl_pct'))}<br>"
            f"Net: {self._format_history_value('net_pnl', row.get('net_pnl'))} / {self._format_history_value('net_pnl_pct', row.get('net_pnl_pct'))}<br>"
            f"Configured: drop {self._format_history_value('configured_initial_drop_pct', row.get('configured_initial_drop_pct'))}, "
            f"BUY rebound/trail {self._format_history_value('configured_buy_rebound_pct', row.get('configured_buy_rebound_pct'))}, "
            f"min profit {self._format_history_value('configured_min_profit_pct', row.get('configured_min_profit_pct'))}, "
            f"SELL trailing-stop {self._format_history_value('configured_sell_trail_pct', row.get('configured_sell_trail_pct'))}<br>"
            f"Protections: protective {row.get('protective_sell_enabled_display') or 'no'} "
            f"({self._format_history_value('configured_protective_sell_trail_pct', row.get('configured_protective_sell_trail_pct'))}), "
            f"slippage {row.get('slippage_buffer_enabled_display') or 'no'} "
            f"({self._format_history_value('configured_slippage_buffer_pct', row.get('configured_slippage_buffer_pct'))})"
            + graph
        )

    @staticmethod
    def _example_history_row() -> dict[str, Any]:
        """One UI-only row that verifies history/flowchart/detail behavior.

        It is never stored in SQLite and therefore cannot affect Completed trade
        summary metrics or risk-limit calculations.
        """
        return {
            "__example": True,
            "id": "EXAMPLE-HISTORY-ROW",
            "ticker": "EXAMPLE",
            "cycle_number": "sample",
            "stage": Stage.CYCLE_COMPLETE.value,
            "created_at": "2026-07-07T14:30:00+00:00",
            "buy_filled_at": "2026-07-07T14:41:18+00:00",
            "sell_filled_at": "2026-07-07T15:39:42+00:00",
            "buy_filled_qty": 96,
            "sell_filled_qty": 96,
            "avg_buy_price": 104.42,
            "avg_sell_price": 110.18,
            "gross_pnl": 552.96,
            "net_pnl": 550.96,
            "budget": 10000.0,
            "reinvested_profit": 0.0,
            "anchor_price": 106.20,
            "drop_trigger_price": 104.076,
            "buy_initial_trail_stop_price": 104.215,
            "sell_initial_trail_stop_price": 109.486,
            "rise_trigger_pct": 4.85,
            "initial_drop_pct": 2.00,
            "buy_rebound_trail_pct": 0.75,
            "sell_trailing_stop_pct": 0.80,
            "protective_sell_enabled": False,
            "protective_sell_enabled_display": "no",
            "protective_sell_trailing_stop_pct": 3.00,
            "slippage_buffer_enabled": False,
            "slippage_buffer_enabled_display": "no",
            "slippage_buffer_pct": 0.25,
            "trading_mode": "paper",
            "market_data_mode": "Live / account permissions",
            "rth_status_display": "RTH open - checked 2026-07-07 14:36:00 UTC",
            "atr_adaptive_enabled": True,
            "atr_adaptive_display": "yes",
            "buy_order_type": "BUY TRAIL",
            "sell_order_type": "SELL TRAIL",
            "buy_order_ref": "IBKRBOT|EXAMPLE|BUY_TRAIL",
            "sell_order_ref": "IBKRBOT|EXAMPLE|SELL_TRAIL",
            "updated_at": "2026-07-07T15:39:42+00:00",
            "sell_vs_buy_pct": ((110.18 / 104.42) - 1.0) * 100.0,
            "gross_pnl_pct": 552.96 / (104.42 * 96) * 100.0,
            "net_pnl_pct": 550.96 / (104.42 * 96) * 100.0,
            "buy_vs_anchor_pct": ((104.42 / 106.20) - 1.0) * 100.0,
            "initial_sell_stop_vs_buy_pct": ((109.486 / 104.42) - 1.0) * 100.0,
            "configured_min_profit_pct": 4.85,
            "configured_initial_drop_pct": 2.00,
            "configured_buy_rebound_pct": 0.75,
            "configured_sell_trail_pct": 0.80,
            "configured_protective_sell_trail_pct": 3.00,
            "configured_slippage_buffer_pct": 0.25,
        }

    def _history_row_clicked(self, row_index: int, column_index: int) -> None:
        rows = getattr(self, "_visible_history_rows", []) or []
        item = self.history_table.item(row_index, 0) if row_index >= 0 else None
        stored_index = item.data(Qt.UserRole) if item is not None else row_index
        try:
            actual_index = int(stored_index)
        except Exception:
            actual_index = row_index
        if actual_index < 0 or actual_index >= len(rows):
            return
        row = rows[actual_index]
        cycle_id = str(row.get("id") or "")
        if row.get("__example"):
            details = {"cycle": row, "orders": [], "executions": [], "events": [], "decision_events": []}
        else:
            details = self.controller.get_cycle_audit_details(cycle_id)
        dialog = CycleAuditDialog(row, details, self)
        dialog.exec()

    @staticmethod
    def _history_outcome_badge(row: dict[str, Any]) -> str:
        return CycleAuditDialog._outcome_badge(row, {"cycle": row})

    @staticmethod
    def _history_row_date(row: dict[str, Any]) -> str:
        for key in ["sell_filled_at", "buy_filled_at", "updated_at", "created_at"]:
            value = row.get(key)
            if value:
                return str(value)[:10]
        return ""

    def _history_row_matches_filters(self, row: dict[str, Any]) -> bool:
        ticker_text = self.history_ticker_filter.text().strip().upper() if hasattr(self, "history_ticker_filter") else ""
        if ticker_text and ticker_text not in str(row.get("ticker") or "").upper():
            return False
        row_date = self._history_row_date(row)
        from_text = self.history_from_filter.text().strip() if hasattr(self, "history_from_filter") else ""
        to_text = self.history_to_filter.text().strip() if hasattr(self, "history_to_filter") else ""
        if from_text and row_date and row_date < from_text:
            return False
        if to_text and row_date and row_date > to_text:
            return False
        outcome = self._history_outcome_badge(row)
        outcome_filter = self.history_outcome_filter.currentText() if hasattr(self, "history_outcome_filter") else "All outcomes"
        net = _float_or_none(row.get("net_pnl"))
        if outcome_filter == "Profitable" and not (net is not None and net >= 0):
            return False
        if outcome_filter == "Losing" and not (net is not None and net < 0):
            return False
        if outcome_filter == "Profit exit" and outcome != "PROFIT EXIT":
            return False
        if outcome_filter == "Protective exit" and outcome != "PROTECTIVE EXIT":
            return False
        if outcome_filter == "Manual/error" and outcome not in {"ERROR STOP", "MANUAL"}:
            return False
        if outcome_filter == "Cancelled" and outcome != "CANCELLED":
            return False
        atr_filter = self.history_atr_filter.currentText() if hasattr(self, "history_atr_filter") else "ATR all"
        atr_value = row.get("atr_adaptive_enabled")
        if atr_value is None:
            atr_value = row.get("configured_atr_adaptive_enabled")
        atr_text = str(row.get("atr_adaptive_display") or row.get("atr_display") or "").lower()
        atr_on = bool(atr_value) or atr_text in {"yes", "on", "true", "1"}
        if atr_filter == "ATR on" and not atr_on:
            return False
        if atr_filter == "ATR off" and atr_on:
            return False
        mode_filter = self.history_mode_filter.currentText() if hasattr(self, "history_mode_filter") else "Paper/live all"
        mode_text = str(row.get("trading_mode") or row.get("profile") or row.get("account") or "").lower()
        if mode_filter == "Paper" and mode_text and "paper" not in mode_text and not mode_text.startswith("du"):
            return False
        return not (mode_filter == "Live" and mode_text and ("paper" in mode_text or mode_text.startswith("du")))

    def _apply_history_filters(
        self,
        *args: Any,
        force_table: bool = False,
        force_flowchart: bool = False,
    ) -> None:
        if not hasattr(self, "history_table"):
            return
        source_rows = list(getattr(self, "_all_history_rows", []) or [])
        display_rows = [row for row in source_rows if self._history_row_matches_filters(row)]
        rows_changed = display_rows != self._visible_history_rows
        self._visible_history_rows = display_rows
        active_index = self.tabs.currentIndex() if hasattr(self, "tabs") else 0
        update_table = bool(force_table or active_index == 2)
        update_flowchart = bool(force_flowchart or active_index == 1)

        if update_table:
            columns = [
                "__outcome",
                "ticker",
                "cycle_number",
                "buy_filled_at",
                "sell_filled_at",
                "buy_filled_qty",
                "avg_buy_price",
                "avg_sell_price",
                "sell_vs_buy_pct",
                "gross_pnl",
                "gross_pnl_pct",
                "net_pnl",
                "net_pnl_pct",
                "budget",
                "reinvested_profit",
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
                "buy_order_ref",
                "sell_order_ref",
                "updated_at",
            ]
            headers = [
                "Outcome",
                "Ticker",
                "Cycle",
                "Buy time",
                "Sell time",
                "Qty",
                "Avg buy",
                "Avg sell",
                "Sell vs buy %",
                "Gross P/L",
                "Gross %",
                "Net P/L",
                "Net %",
                "Budget",
                "Reinvested",
                "Buy vs anchor %",
                "Initial stop vs buy %",
                "Min profit %",
                "Initial drop %",
                "BUY rebound/trail %",
                "SELL trailing-stop %",
                "Protective",
                "Protective SELL trailing-stop %",
                "Slippage",
                "Slippage %",
                "Buy order ref",
                "Sell order ref",
                "Updated",
            ]
            right_keys = {
                "buy_filled_qty", "avg_buy_price", "avg_sell_price", "sell_vs_buy_pct",
                "gross_pnl", "gross_pnl_pct", "net_pnl", "net_pnl_pct", "budget",
                "reinvested_profit", "buy_vs_anchor_pct", "initial_sell_stop_vs_buy_pct",
                "configured_min_profit_pct", "configured_initial_drop_pct",
                "configured_buy_rebound_pct", "configured_sell_trail_pct",
                "configured_protective_sell_trail_pct", "configured_slippage_buffer_pct",
            }
            badge_states = {
                "PROFIT EXIT": "success",
                "PROTECTIVE EXIT": "waiting",
                "ERROR STOP": "risk",
                "CANCELLED": "inactive",
                "MANUAL": "waiting",
                "LOSS EXIT": "risk",
            }
            self.history_table.setUpdatesEnabled(False)
            self.history_table.setSortingEnabled(False)
            try:
                self.history_table.setColumnCount(len(columns))
                self.history_table.setHorizontalHeaderLabels(headers)
                self.history_table.setRowCount(len(display_rows))
                for r, row in enumerate(display_rows):
                    tooltip = self._history_tooltip(row)
                    for c, key in enumerate(columns):
                        raw_value = row.get(key)
                        value = self._history_outcome_badge(row) if key == "__outcome" else self._format_history_value(key, raw_value)
                        if key == "cycle_number":
                            # Keep the DisplayRole numeric so Qt sorts cycle 2 before cycle 10.
                            try:
                                item = QTableWidgetItem()
                                item.setData(Qt.DisplayRole, int(raw_value))
                            except (TypeError, ValueError):
                                item = QTableWidgetItem(value)
                        else:
                            item = QTableWidgetItem(value)
                        item.setData(Qt.UserRole, r)
                        if key == "__outcome":
                            state = badge_states.get(value, "inactive")
                            item.setForeground(QBrush(QColor(SEMANTIC_COLORS.get(state, SEMANTIC_COLORS["inactive"]))))
                            item.setTextAlignment(Qt.AlignCenter)
                        elif key in right_keys:
                            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                        item.setToolTip(tooltip)
                        self.history_table.setItem(r, c, item)
                if not self._history_columns_sized:
                    _cap_table_columns_for_horizontal_scroll(self.history_table, minimum=72, maximum=220)
                    self._history_columns_sized = True
                _fit_table_height_to_rows(self.history_table, min_rows=8, max_visible_rows=18, min_height=320, max_fit_height=620)
            finally:
                self.history_table.setSortingEnabled(True)
                self.history_table.setUpdatesEnabled(True)
                self.history_table.viewport().update()
            self._history_table_refresh_pending = False
        elif rows_changed:
            self._history_table_refresh_pending = True

        if update_flowchart and hasattr(self, "flowchart_panel"):
            self.flowchart_panel.set_history_rows(display_rows)
            self._flowchart_history_refresh_pending = False
        elif rows_changed:
            self._flowchart_history_refresh_pending = True

    def _on_history(self, rows: list[dict[str, Any]]) -> None:
        display_rows = list(rows or [])
        if not display_rows:
            display_rows = [self._example_history_row()]
        self._all_history_rows = display_rows
        self._history_columns_sized = False
        self._apply_history_filters()

    def _export_history(self) -> None:
        target = self.controller.export_history(self.history_ticker_filter.text())
        QMessageBox.information(self, "Export complete", f"Trade history exported to:\n{target}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._system_shutdown_in_progress:
            instance = getattr(QApplication, "instance", None)
            try:
                application = instance() if callable(instance) else None
                is_saving_session = getattr(application, "isSavingSession", None)
                shutdown_is_active = application is None or not callable(is_saving_session) or bool(is_saving_session())
            except Exception:
                shutdown_is_active = True
            if shutdown_is_active:
                event.accept()
                return
            # A prior Windows shutdown request was cancelled. Re-arm the normal
            # close dialog instead of silently treating later manual closes as
            # session termination.
            self._system_shutdown_in_progress = False

        if self._stop_dialog_exit_requested:
            if not self._save_resume_checkpoint("operator_exit"):
                QMessageBox.warning(
                    self,
                    "Exit app",
                    "The resume checkpoint could not be written. The app will remain open so the strategy state is not intentionally abandoned.",
                )
                event.ignore()
                return
            self.controller.shutdown()
            event.accept()
            return

        # The window close button uses the same controlled-exit path as the
        # visible 5. Stop strategy button. This avoids an accidental top-right X
        # closing the GUI without asking the operator how to handle local cycle
        # state and visible app-owned TWS orders.
        open_orders = self._visible_tws_open_app_orders()
        cycle = (self.current_snapshot or {}).get("active_cycle") or {}
        stage = str(cycle.get("stage") or "")
        safe_no_running_strategy = stage in {"", Stage.IDLE.value, Stage.CYCLE_COMPLETE.value, Stage.STOPPED.value}
        # Use the persisted app-owned fill ledger for the exit decision. The
        # account-wide IBKR position can include manual or third-party shares,
        # and the snapshot cycle fields may lag the final terminal poll briefly.
        unsold_qty = self._persisted_app_unsold_quantity(cycle)
        show_position_close = bool(unsold_qty > 0)
        dialog = StopDialog(
            self,
            show_tws_order_actions=bool(open_orders),
            open_order_count=len(open_orders),
            show_position_close_action=show_position_close,
            unsold_quantity=unsold_qty,
            exit_context=True,
            safe_to_exit=(not open_orders and not show_position_close and safe_no_running_strategy),
            show_resume_later_exit_action=bool(cycle and not safe_no_running_strategy),
        )
        if dialog.exec() != QDialog.Accepted:
            event.ignore()
            return
        if dialog.selected_action is not None:
            wait_for_local_state = bool(dialog.exit_app_after_action and dialog.selected_action == StopAction.STOP_NOW_NO_BROKER_ACTION)
            if not self._request_stop_action(dialog.selected_action, wait_for_local_state=wait_for_local_state):
                event.ignore()
                return
            if not dialog.exit_app_after_action:
                # Broker-affecting stop actions such as market-close or cancel
                # need the GUI/worker to remain alive so the queued command can
                # run and the operator can verify the result. Only explicit
                # Exit app paths close the process here.
                event.ignore()
                return
        checkpoint_reason = (
            "operator_exit_resume_later"
            if dialog.selected_action is None and bool(cycle and not safe_no_running_strategy)
            else "operator_exit"
        )
        if not self._save_resume_checkpoint(checkpoint_reason):
            QMessageBox.warning(
                self,
                "Exit app",
                "The resume checkpoint could not be written. The app will remain open so the strategy state is not intentionally abandoned.",
            )
            event.ignore()
            return
        self.controller.shutdown()
        event.accept()

    def _apply_styles(self) -> None:
        # Explicit light palette. Without these generic widget rules, Windows
        # dark mode can make Qt labels/inputs inherit white text on white cards.
        self.setStyleSheet(
            """
            QWidget {
                color: #111827;
                background-color: #f6f7f9;
                font-size: 12px;
            }
            QMainWindow {
                background-color: #f6f7f9;
            }
            QMenuBar {
                background-color: #f6f7f9;
                color: #111827;
                border-bottom: 1px solid #d7dae0;
            }
            QMenuBar::item {
                background: transparent;
                padding: 5px 10px;
            }
            QMenuBar::item:selected {
                background-color: #e7e9ee;
            }
            QMenu {
                background-color: #ffffff;
                color: #111827;
                border: 1px solid #c7cbd1;
            }
            QMenu::item:selected {
                background-color: #e7e9ee;
            }
            QTabWidget::pane {
                border: 1px solid #c7cbd1;
                background-color: #f6f7f9;
            }
            QTabBar::tab {
                background-color: #e7e9ee;
                color: #3d4552;
                padding: 8px 14px;
                border: 1px solid #c7cbd1;
                border-bottom: none;
                min-width: 110px;
            }
            QTabBar::tab:selected {
                background-color: #ffffff;
                color: #111827;
                font-weight: 600;
            }
            QGroupBox {
                color: #111827;
                font-weight: 600;
                border: 1px solid #c7cbd1;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px;
                background-color: #ffffff;
            }
            QGroupBox::title {
                color: #111827;
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                background-color: #ffffff;
            }
            QGroupBox#StrategyInputsBox::title,
            QGroupBox#EntryBox::title,
            QGroupBox#ExitBox::title {
                font-weight: 900;
                font-size: 13px;
                letter-spacing: 0.4px;
            }
            QLabel {
                color: #111827;
                background: transparent;
            }
            QCheckBox {
                color: #111827;
                background: transparent;
                spacing: 6px;
            }
            QLineEdit,
            QSpinBox,
            QDoubleSpinBox,
            QComboBox {
                color: #111827;
                background-color: #ffffff;
                border: 2px solid #94a3b8;
                border-radius: 6px;
                padding: 5px 8px;
                min-height: 26px;
                selection-background-color: #2563eb;
                selection-color: #ffffff;
            }
            QComboBox {
                padding-right: 26px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #94a3b8;
                background-color: #e0f2fe;
                border-top-right-radius: 4px;
                border-bottom-right-radius: 4px;
            }
            QComboBox::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid #1f2937;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 2px solid #64748b;
                border-radius: 3px;
                background-color: #ffffff;
            }
            QCheckBox::indicator:checked {
                background-color: #2563eb;
                border-color: #1d4ed8;
            }
            QLineEdit:focus,
            QSpinBox:focus,
            QDoubleSpinBox:focus,
            QComboBox:focus {
                border: 2px solid #2563eb;
            }
            QLineEdit:disabled,
            QSpinBox:disabled,
            QDoubleSpinBox:disabled,
            QComboBox:disabled {
                color: #6b7280;
                background-color: #eef0f4;
            }
            QLineEdit[lockedForStage="true"],
            QSpinBox[lockedForStage="true"],
            QDoubleSpinBox[lockedForStage="true"],
            QComboBox[lockedForStage="true"],
            QCheckBox[lockedForStage="true"] {
                color: #6b7280;
                background-color: #eef0f4;
                border-color: #cbd5e1;
            }
            QLineEdit[manualInputLocked="true"],
            QSpinBox[manualInputLocked="true"],
            QDoubleSpinBox[manualInputLocked="true"],
            QComboBox[manualInputLocked="true"],
            QCheckBox[manualInputLocked="true"] {
                color: #4b5563;
                background-color: #f3f4f6;
                border-color: #94a3b8;
            }
            QFrame#LiveStatusBar,
            QFrame#CommandBar,
            QFrame#ViewModeBox {
                background-color: #ffffff;
                border: 1px solid #c7cbd1;
                border-radius: 8px;
            }
            QPushButton#CommandStepButton {
                font-size: 13px;
                font-weight: 800;
                min-height: 34px;
            }
            QPushButton#InputLockButton {
                color: #1e3a8a;
                background-color: #eff6ff;
                border: 1px solid #2563eb;
                border-radius: 8px;
                font-size: 20px;
                font-weight: 900;
                padding: 5px 6px;
            }
            QPushButton#InputLockButton[locked="true"] {
                color: #78350f;
                background-color: #fffbeb;
                border: 2px solid #d97706;
            }
            QComboBox QAbstractItemView {
                color: #111827;
                background-color: #ffffff;
                selection-background-color: #2563eb;
                selection-color: #ffffff;
            }
            QPushButton {
                color: #111827;
                background-color: #ffffff;
                border: 1px solid #9ca3af;
                border-radius: 5px;
                padding: 7px 12px;
                min-height: 26px;
            }
            QPushButton:hover {
                background-color: #f1f3f6;
            }
            QPushButton:pressed {
                background-color: #e7e9ee;
            }
            QPushButton:disabled {
                color: #6b7280;
                background-color: #eef0f4;
            }
            QPushButton#RecoveryPrimaryButton {
                color: #ffffff;
                background-color: #2563eb;
                border: 1px solid #1d4ed8;
                font-weight: 800;
            }
            QPushButton#RecoveryPrimaryButton:hover {
                background-color: #1d4ed8;
            }
            QPushButton#RecoveryCautionButton {
                color: #78350f;
                background-color: #fffbeb;
                border: 1px solid #d97706;
                font-weight: 700;
            }
            QPushButton#RecoveryDangerButton {
                color: #7f1d1d;
                background-color: #fef2f2;
                border: 1px solid #dc2626;
                font-weight: 800;
            }
            QPushButton#RecoveryPrimaryButton:disabled,
            QPushButton#RecoveryCautionButton:disabled,
            QPushButton#RecoveryDangerButton:disabled {
                color: #6b7280;
                background-color: #eef0f4;
                border: 1px solid #cbd5e1;
            }
            QLabel#RecoveryStepTitle {
                color: #111827;
                font-size: 13px;
                font-weight: 900;
                padding-top: 2px;
            }
            QLabel#RecoveryRefreshStatus {
                color: #4b5563;
                background-color: #f3f4f6;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 7px 10px;
                font-weight: 800;
            }
            QLabel#RecoveryRefreshStatus[state="current"] {
                color: #064e3b;
                background-color: #ecfdf5;
                border: 1px solid #16a34a;
            }
            QLabel#RecoveryRefreshStatus[state="stale"],
            QLabel#RecoveryRefreshStatus[state="not_refreshed"] {
                color: #78350f;
                background-color: #fffbeb;
                border: 1px solid #d97706;
            }
            QLabel#RecoveryRefreshStatus[state="failed"] {
                color: #7f1d1d;
                background-color: #fef2f2;
                border: 1px solid #dc2626;
            }
            QLabel#RecoveryRecommendation {
                color: #374151;
                background-color: #f3f4f6;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 10px;
                font-weight: 800;
            }
            QLabel#RecoveryRecommendation[state="active"] {
                color: #1e3a8a;
                background-color: #eff6ff;
                border: 1px solid #2563eb;
            }
            QLabel#RecoveryRecommendation[state="success"] {
                color: #064e3b;
                background-color: #ecfdf5;
                border: 1px solid #16a34a;
            }
            QLabel#RecoveryRecommendation[state="waiting"] {
                color: #78350f;
                background-color: #fffbeb;
                border: 1px solid #d97706;
            }
            QLabel#RecoveryRecommendation[state="risk"] {
                color: #7f1d1d;
                background-color: #fef2f2;
                border: 1px solid #dc2626;
            }
            QLabel#StatusLabel {
                color: #111827;
                font-weight: 600;
            }
            QLabel#Muted {
                color: #5b6270;
            }
            QLabel#ZeroDisabledLabel {
                color: #92400e;
                background-color: #fffbeb;
                border: 1px solid #fcd34d;
                border-radius: 8px;
                padding: 3px 6px;
                font-size: 10px;
                font-weight: 800;
            }
            QLabel#WarningLabel {
                color: #8a4b00;
                font-weight: 600;
            }
            QLabel#InfoBadge {
                color: #1f2937;
                background-color: #eef2ff;
                border: 1px solid #a5b4fc;
                border-radius: 10px;
                font-weight: 700;
                padding: 2px;
            }
            QLabel#ApplicabilityBadge {
                color: #374151;
                background-color: #f3f4f6;
                border: 1px solid #cbd5e1;
                border-radius: 9px;
                padding: 3px 6px;
                font-size: 10px;
                font-weight: 800;
            }
            QLabel#ApplicabilityBadge[state="current"] {
                color: #064e3b;
                background-color: #ecfdf5;
                border: 1px solid #16a34a;
            }
            QLabel#ApplicabilityBadge[state="next_order"] {
                color: #1e3a8a;
                background-color: #eff6ff;
                border: 1px solid #2563eb;
            }
            QLabel#ApplicabilityBadge[state="next_cycle"] {
                color: #78350f;
                background-color: #fffbeb;
                border: 1px solid #d97706;
            }
            QLabel#ApplicabilityBadge[state="not_applicable"] {
                color: #7f1d1d;
                background-color: #fef2f2;
                border: 1px solid #dc2626;
            }
            QLabel#ApplicabilityBadge[state="changed"] {
                color: #1e3a8a;
                background-color: #dbeafe;
                border: 2px solid #2563eb;
            }
            QLabel#ProfitGuardGood {
                color: #064e3b;
                background-color: #d1fae5;
                border: 1px solid #6ee7b7;
                border-radius: 6px;
                padding: 7px 9px;
                font-weight: 600;
            }
            QLabel#ProfitGuardBad {
                color: #7f1d1d;
                background-color: #fee2e2;
                border: 1px solid #fecaca;
                border-radius: 6px;
                padding: 7px 9px;
                font-weight: 600;
            }
            QLabel#BigPrice {
                color: #111827;
                font-size: 34px;
                font-weight: 800;
                padding: 4px 0px;
            }
            QLabel#PriceStatus {
                color: #111827;
                font-weight: 700;
            }
            QLabel#ApiIndicatorGood {
                color: #059669;
                font-size: 24px;
                font-weight: 900;
            }
            QLabel#ApiIndicatorWarn {
                color: #d97706;
                font-size: 24px;
                font-weight: 900;
            }
            QLabel#ApiIndicatorBad {
                color: #dc2626;
                font-size: 24px;
                font-weight: 900;
            }
            QLabel#ApiIndicatorText {
                color: #111827;
                font-weight: 700;
            }
            QProgressBar {
                color: #111827;
                background-color: #eef0f4;
                border: 1px solid #c7cbd1;
                border-radius: 5px;
                text-align: center;
                min-height: 18px;
            }
            QProgressBar::chunk {
                background-color: #9ca3af;
                border-radius: 4px;
            }
            QFrame#MetricCard {
                background-color: #ffffff;
                border: 1px solid #d7dae0;
                border-radius: 8px;
            }
            QLabel#MetricTitle {
                color: #5b6270;
                font-size: 11px;
                font-weight: 400;
            }
            QLabel#MetricValue {
                color: #111827;
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#StageInactive {
                background-color: #e7e9ee;
                color: #2f3744;
                border: 2px solid #c7cbd1;
                border-radius: 10px;
                padding: 10px;
                font-size: 14px;
                font-weight: 800;
            }
            QLabel#StageActive {
                background-color: #0f172a;
                color: #ffffff;
                border: 4px solid #2563eb;
                border-radius: 10px;
                padding: 10px;
                font-size: 15px;
                font-weight: 900;
            }
            QLabel#PriceBig {
                color: #111827;
                background-color: #f9fafb;
                border: 1px solid #d7dae0;
                border-radius: 8px;
                padding: 10px 14px;
                font-size: 30px;
                font-weight: 800;
            }
            QLabel#PriceStatusGood {
                color: #064e3b;
                background-color: #d1fae5;
                border: 1px solid #6ee7b7;
                border-radius: 8px;
                padding: 8px 10px;
                font-weight: 700;
            }
            QLabel#PriceStatusBad {
                color: #7f1d1d;
                background-color: #fee2e2;
                border: 1px solid #fecaca;
                border-radius: 8px;
                padding: 8px 10px;
                font-weight: 700;
            }
            QLabel#PriceStatusWarning {
                color: #78350f;
                background-color: #fffbeb;
                border: 1px solid #f59e0b;
                border-radius: 8px;
                padding: 8px 10px;
                font-weight: 700;
            }
            QTextEdit {
                color: #f9fafb;
                background-color: #202124;
                border: 1px solid #111827;
                border-radius: 5px;
                padding: 6px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }
            QTableWidget {
                color: #111827;
                background-color: #ffffff;
                font-size: 11px;
                gridline-color: #d7dae0;
                border: 1px solid #c7cbd1;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QHeaderView::section {
                color: #111827;
                background-color: #e7e9ee;
                border: 1px solid #c7cbd1;
                padding: 5px;
                font-weight: 600;
            }
            QScrollBar:vertical {
                width: 14px;
                background: #f3f4f6;
                border-left: 1px solid #d1d5db;
                margin: 0px;
            }
            QScrollBar:horizontal {
                height: 14px;
                background: #f3f4f6;
                border-top: 1px solid #d1d5db;
                margin: 0px;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #9ca3af;
                border: 2px solid #f3f4f6;
                border-radius: 6px;
                min-height: 28px;
                min-width: 28px;
            }
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
                background: #6b7280;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
                height: 0px;
                border: none;
                background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            """
        )
