"""Plain-file diagnostics and full-process watchdog restart handoff.

The trading worker and SQLite repository share one process.  If that worker is
blocked or exits unexpectedly, diagnostics and restart coordination must not
rely on the same SQLite connection path.  This module therefore uses only small
best-effort files beside the portable application.

A watchdog restart request is authenticated with a random one-time token passed
only to the replacement process.  The replacement may automatically resume a
stored cycle only when the token, request age, and exact persisted cycle ID all
match.  Restart history is also kept outside SQLite to prevent rapid restart
loops while still allowing unattended recovery after a cooldown.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import recovery_cycle_signature
from .paths import app_dir

WATCHDOG_RESTART_EXIT_CODE = 75
WATCHDOG_REQUEST_SCHEMA_VERSION = 2
WATCHDOG_REQUEST_MAX_AGE_SECONDS = 10 * 60
WATCHDOG_RESTART_WINDOW_SECONDS = 15 * 60
WATCHDOG_FAST_RESTART_LIMIT = 3
WATCHDOG_RESTART_BACKOFF_SECONDS = 5 * 60

WATCHDOG_WARNING_SECONDS = 3.0
WATCHDOG_UNRESPONSIVE_SECONDS = 15.0
WATCHDOG_AUTO_RESTART_SECONDS = 30.0

AUTO_RESUMABLE_STAGES = {
    "1_WAIT_INITIAL_DROP",
    "2_BUY_TRAIL_ACTIVE",
    "3_WAIT_RISE_TRIGGER",
    "4_SELL_TRAIL_ACTIVE",
}


def _portable_root(base_dir: Optional[Path] = None) -> Path:
    return Path(base_dir) if base_dir is not None else app_dir()


def watchdog_reports_dir(base_dir: Optional[Path] = None) -> Path:
    target = _portable_root(base_dir) / "debug_reports"
    target.mkdir(parents=True, exist_ok=True)
    return target


def emergency_log_path(base_dir: Optional[Path] = None) -> Path:
    return watchdog_reports_dir(base_dir) / "worker_emergency.log"


def watchdog_request_path(base_dir: Optional[Path] = None) -> Path:
    return watchdog_reports_dir(base_dir) / "watchdog_restart_request.json"


def watchdog_history_path(base_dir: Optional[Path] = None) -> Path:
    return watchdog_reports_dir(base_dir) / "watchdog_restart_history.json"


def _utc_text(epoch: Optional[float] = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return enum_value
    return str(value)


def _redacted_restart_request(data: Any) -> Any:
    """Return diagnostics-safe request data without the one-time token."""
    if not isinstance(data, dict):
        return data
    redacted = dict(data)
    if "token" in redacted:
        redacted["token"] = "[redacted]"
    return redacted


def append_emergency_log(
    message: str,
    *,
    exc: Optional[BaseException] = None,
    context: Optional[dict[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> None:
    """Append a diagnostic record without ever propagating an exception."""
    try:
        thread = threading.current_thread()
        lines = [
            "=" * 96,
            (
                f"{_utc_text()} pid={os.getpid()} thread={thread.name} "
                f"thread_id={thread.ident}"
            ),
            str(message or "Emergency diagnostic"),
        ]
        if context:
            lines.append(
                "context="
                + json.dumps(
                    context,
                    sort_keys=True,
                    default=_json_default,
                    ensure_ascii=False,
                )
            )
        if exc is not None:
            lines.append(
                "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).rstrip()
            )
        payload = ("\n".join(lines).rstrip() + "\n").encode(
            "utf-8", errors="replace"
        )
        path = emergency_log_path(base_dir)
        descriptor = os.open(
            str(path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    break
                remaining = remaining[written:]
        finally:
            os.close(descriptor)
    except BaseException:
        # This is the final diagnostics path.  It must never terminate the
        # worker, GUI, shutdown handler, or replacement-process launch.
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    serialized = (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            default=_json_default,
            ensure_ascii=False,
        )
        + "\n"
    )
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def create_watchdog_restart_request(
    token: str,
    snapshot: Optional[dict[str, Any]],
    reason: str,
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> dict[str, Any]:
    """Persist the one-time handoff used by the replacement process."""
    clean_token = str(token or "").strip()
    if not clean_token:
        raise ValueError("A watchdog restart token is required.")
    now = time.time() if now_epoch is None else float(now_epoch)
    payload = dict(snapshot or {})
    cycle = payload.get("active_cycle")
    cycle = dict(cycle) if isinstance(cycle, dict) else {}
    stage = str(cycle.get("stage") or "")
    cycle_id = str(cycle.get("id") or "")
    startup_resume_required = bool(payload.get("startup_resume_required"))
    recovery_required = bool(
        payload.get("recovery_required") or cycle.get("recovery_required")
    )
    was_monitoring_active = bool(
        cycle_id
        and stage in AUTO_RESUMABLE_STAGES
        and not startup_resume_required
        and not recovery_required
    )
    request: dict[str, Any] = {
        "schema_version": WATCHDOG_REQUEST_SCHEMA_VERSION,
        "token": clean_token,
        "created_at": _utc_text(now),
        "created_epoch": now,
        "source_pid": os.getpid(),
        "reason": str(reason or "worker watchdog requested a restart"),
        # Ordinary application startup deliberately requires the operator to
        # press Start for a stored cycle.  Automatic recovery is narrower: it
        # is permitted only when the final healthy worker snapshot proves that
        # this exact cycle was already being supervised immediately before the
        # watchdog incident and was not awaiting reconciliation.
        "auto_resume": was_monitoring_active,
        "was_monitoring_active": was_monitoring_active,
        "startup_resume_required": startup_resume_required,
        "recovery_required": recovery_required,
        "expected_cycle_id": cycle_id or None,
        "expected_cycle_stage": stage or None,
        "expected_cycle_updated_at": cycle.get("updated_at"),
        "expected_cycle_signature": recovery_cycle_signature(cycle),
        "ticker": cycle.get("ticker"),
        "con_id": cycle.get("con_id"),
        "order_refs": {
            "buy": cycle.get("buy_order_ref"),
            "protective_sell": cycle.get("protective_sell_order_ref"),
            "sell": cycle.get("sell_order_ref"),
        },
        "last_snapshot": {
            "status": payload.get("status"),
            "connected": payload.get("connected"),
            "worker_health": payload.get("worker_health"),
            "storage_fault": payload.get("storage_fault"),
            "price_snapshot": {
                key: (payload.get("price_snapshot") or {}).get(key)
                for key in (
                    "price",
                    "source",
                    "api_data_state",
                    "api_last_data_received_at",
                    "api_data_age_seconds",
                    "rth_status",
                )
            },
        },
    }
    _atomic_write_json(watchdog_request_path(base_dir), request)
    return request


def consume_watchdog_restart_request(
    token: str,
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
    max_age_seconds: float = WATCHDOG_REQUEST_MAX_AGE_SECONDS,
) -> Optional[dict[str, Any]]:
    """Return and remove a matching, recent one-time restart request."""
    clean_token = str(token or "").strip()
    if not clean_token:
        return None
    path = watchdog_request_path(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        append_emergency_log(
            "Could not read watchdog restart request.",
            exc=exc,
            base_dir=base_dir,
        )
        return None
    if not isinstance(data, dict) or str(data.get("token") or "") != clean_token:
        return None

    # A matching token is one-time.  Remove the request even when it is expired
    # or malformed so a later manual launch cannot accidentally reuse it.
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

    try:
        created = float(data.get("created_epoch"))
    except Exception:
        append_emergency_log(
            "Ignored watchdog restart request with an invalid timestamp.",
            context={"request": _redacted_restart_request(data)},
            base_dir=base_dir,
        )
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    age = now - created
    if age < -60.0 or age > max(1.0, float(max_age_seconds)):
        append_emergency_log(
            "Ignored expired watchdog restart request.",
            context={"age_seconds": age, "request": _redacted_restart_request(data)},
            base_dir=base_dir,
        )
        return None
    if int(data.get("schema_version") or 0) != WATCHDOG_REQUEST_SCHEMA_VERSION:
        append_emergency_log(
            "Ignored watchdog restart request with an unsupported schema.",
            context={"request": _redacted_restart_request(data)},
            base_dir=base_dir,
        )
        return None
    # The token authenticates only this one file consumption. It is no longer
    # needed after the matching request has been removed and must not enter
    # controller memory, snapshots, logs, or later audit output.
    data.pop("token", None)
    data["age_seconds"] = max(0.0, age)
    return data


def _load_restart_history(
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> list[float]:
    now = time.time() if now_epoch is None else float(now_epoch)
    path = watchdog_history_path(base_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        append_emergency_log(
            "Could not read watchdog restart history; automatic restart is blocked fail-closed.",
            exc=exc,
            base_dir=base_dir,
        )
        raise RuntimeError("watchdog restart history is unavailable") from exc
    values = data.get("attempts") if isinstance(data, dict) else data
    result: list[float] = []
    for value in values if isinstance(values, list) else []:
        try:
            timestamp = float(value)
        except Exception:
            continue
        if 0.0 <= now - timestamp <= WATCHDOG_RESTART_WINDOW_SECONDS:
            result.append(timestamp)
    return sorted(result)


def watchdog_restart_delay_seconds(
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> float:
    """Return the cooldown before another automatic restart is permitted."""
    now = time.time() if now_epoch is None else float(now_epoch)
    attempts = _load_restart_history(base_dir=base_dir, now_epoch=now)
    if len(attempts) < WATCHDOG_FAST_RESTART_LIMIT:
        return 0.0
    return max(0.0, WATCHDOG_RESTART_BACKOFF_SECONDS - (now - attempts[-1]))


def record_watchdog_restart_attempt(
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
    reason: str = "",
) -> bool:
    """Record one full-process restart attempt outside SQLite.

    Returning ``False`` is intentionally fail-closed.  The GUI must not exit
    for an automatic replacement when restart-loop history cannot be made
    durable, because an unwritable history file could otherwise permit an
    uncontrolled process-restart loop.
    """
    now = time.time() if now_epoch is None else float(now_epoch)
    try:
        attempts = _load_restart_history(base_dir=base_dir, now_epoch=now)
    except Exception:
        return False
    attempts.append(now)
    payload = {
        "updated_at": _utc_text(now),
        "last_reason": str(reason or ""),
        "attempts": attempts[-100:],
    }
    try:
        _atomic_write_json(watchdog_history_path(base_dir), payload)
    except Exception as exc:
        append_emergency_log(
            "Could not update watchdog restart history.",
            exc=exc,
            base_dir=base_dir,
        )
        return False
    return True


def discard_expired_watchdog_restart_request(
    *,
    base_dir: Optional[Path] = None,
    now_epoch: Optional[float] = None,
    max_age_seconds: float = WATCHDOG_REQUEST_MAX_AGE_SECONDS,
) -> bool:
    """Remove a leftover handoff file only when it is expired or malformed.

    An ordinary tokenless startup cannot consume a request, so an orphaned file
    (for example after a failed replacement launch) would otherwise persist
    forever. A fresh, well-formed request is deliberately preserved for the
    replacement process that holds its one-time token.
    """
    path = watchdog_request_path(base_dir)
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:
        data = None
        expired = True
    else:
        try:
            valid_request = bool(
                isinstance(data, dict)
                and int(data.get("schema_version") or 0) == WATCHDOG_REQUEST_SCHEMA_VERSION
                and str(data.get("token") or "").strip()
            )
            created = float(data.get("created_epoch")) if isinstance(data, dict) else float("nan")
            now = time.time() if now_epoch is None else float(now_epoch)
            age = now - created
            expired = (
                not valid_request
                or not (age == age)
                or age < -60.0
                or age > max(1.0, float(max_age_seconds))
            )
        except Exception:
            expired = True
    if not expired:
        return False
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        append_emergency_log(
            "Could not remove an expired watchdog restart request during startup.",
            exc=exc,
            base_dir=base_dir,
        )
        return False
    append_emergency_log(
        "Removed an expired or malformed watchdog restart request during ordinary startup.",
        context={"request": _redacted_restart_request(data) if isinstance(data, dict) else None},
        base_dir=base_dir,
    )
    return True


def discard_watchdog_restart_request(
    token: str = "",
    *,
    base_dir: Optional[Path] = None,
) -> bool:
    """Remove a pending handoff, optionally only when its token matches."""
    path = watchdog_request_path(base_dir)
    clean_token = str(token or "").strip()
    if clean_token:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except Exception as exc:
            append_emergency_log(
                "Could not validate watchdog restart request before cleanup.",
                exc=exc,
                base_dir=base_dir,
            )
            return False
        if not isinstance(data, dict) or str(data.get("token") or "") != clean_token:
            return False
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        append_emergency_log(
            "Could not remove watchdog restart request.",
            exc=exc,
            base_dir=base_dir,
        )
        return False
    return True
