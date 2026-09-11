"""Validated, per-contract RTH ATR checkpoints, never synthetic market ticks.

A checkpoint supplies only an initial volatility estimate. Current-session bars
replace it as soon as their normal calculation is ready; quote freshness, RTH,
entry timing and order validation remain the controller's responsibility.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from math import isclose, isfinite
from typing import Any, Callable

ATR_SEED_VERSION = 1
ATR_SEED_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
ATR_SEED_WRITE_INTERVAL_SECONDS = 60.0
ATR_SEED_SAVE_WINDOW_SECONDS = 5 * 60.0


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ATR checkpoint timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def atr_seed_identity(contract: Any, settings: Any, profile: str) -> dict[str, Any] | None:
    """Do not share seeds merely because two listings use the same symbol."""
    try:
        con_id = int(getattr(contract, "con_id", 0) or 0)
        ticker = str(getattr(contract, "ticker", "") or "").strip().upper()
        currency = str(getattr(contract, "currency", "") or "").strip().upper()
        if con_id <= 0 or not ticker or not currency:
            return None
        return {
            "ticker": ticker,
            "con_id": con_id,
            "currency": currency,
            "exchange": str(getattr(contract, "exchange", "") or "").upper(),
            "primary_exchange": str(getattr(contract, "primary_exchange", "") or "").upper(),
            "sec_type": str(getattr(contract, "sec_type", "STK") or "STK").upper(),
            "profile": str(profile),
            "period": max(2, int(settings.atr_period)),
            "bar_seconds": max(5, int(settings.atr_bar_seconds)),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def valid_atr_seed(record: Any, identity: dict[str, Any], now: datetime) -> bool:
    """Reject malformed, future, expired or differently calibrated estimates."""
    try:
        if not isinstance(record, dict) or record.get("version") != ATR_SEED_VERSION or record.get("identity") != identity:
            return False
        observed = _utc(record["observed_at"])
        opened, closed = _utc(record["session_open"]), _utc(record["session_close"])
        if not opened <= observed < closed or not 0 <= (now - observed).total_seconds() <= ATR_SEED_MAX_AGE_SECONDS:
            return False
        snapshot = record["snapshot"]
        if snapshot.get("ready") is not True or snapshot.get("seeded"):
            return False
        if int(snapshot["period"]) != identity["period"] or int(snapshot["bar_seconds"]) != identity["bar_seconds"]:
            return False
        if int(snapshot["bars_available"]) < identity["period"] + 1:
            return False
        atr, pct, close = (float(snapshot[key]) for key in ("atr", "atr_pct", "latest_close"))
        return all(isfinite(value) and value > 0 for value in (atr, pct, close)) and isclose(pct, 100.0 * atr / close, rel_tol=1e-6)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


class AtrSessionMemory:
    """Worker-owned optional checkpoint; serialized in the existing settings DB."""

    def __init__(self, storage: Any, on_error: Callable[..., Any] | None = None) -> None:
        self.storage = storage
        self.on_error = on_error
        self.identity: dict[str, Any] | None = None
        self.key = ""
        self.seed: dict[str, Any] | None = None
        self.session: tuple[str, str] | None = None
        self.last_session: tuple[str, str] | None = None
        self.now: datetime | None = None
        self.observed_at: str | None = None
        self.dirty = False
        self.last_write_epoch = float("-inf")
        self._close_flush_attempted_session: tuple[str, str] | None = None
        self.error = ""
        self.live_only = True

    def _error(self, operation: str, exc: Exception) -> None:
        message = f"ATR checkpoint {operation} failed: {exc}"
        if message != self.error and self.on_error is not None:
            try:
                self.on_error(message, exc=exc)
            except Exception:
                pass
        self.error = message

    def flush(self, *, force: bool = True) -> None:
        """Save near the recorded RTH close, or explicitly at orderly shutdown.

        Automatic callers use force=False. They may save once per minute in
        the final five minutes of a verified session and make one final attempt
        on/after its recorded close, even within that minute. A lost/stale RTH
        status or an identity edit is not a session close. Failed final writes
        keep the dirty estimate and retry at the bounded interval.
        """
        if not self.dirty or not self.seed or not self.key or self.now is None or self.identity is None:
            return
        if not valid_atr_seed(self.seed, self.identity, self.now):
            return
        seed_session = (self.seed["session_open"], self.seed["session_close"])
        seconds_to_close = (_utc(seed_session[1]) - self.now).total_seconds()
        session_ended = seconds_to_close <= 0
        first_close_attempt = session_ended and self._close_flush_attempted_session != seed_session
        if not force:
            in_closing_window = (
                self.session == seed_session and 0 < seconds_to_close <= ATR_SEED_SAVE_WINDOW_SECONDS
            )
            if not in_closing_window and not session_ended:
                return
            if not first_close_attempt and self.now.timestamp() - self.last_write_epoch < ATR_SEED_WRITE_INTERVAL_SECONDS:
                return
        self.last_write_epoch = self.now.timestamp()
        if session_ended:
            # Record attempts, not just successful writes: a failing database
            # must not turn the one final flush into a retry on every callback.
            self._close_flush_attempted_session = seed_session
        try:
            self.storage.set_json(self.key, self.seed)
        except Exception as exc:
            self._error("write", exc)
        else:
            self.dirty = False
            self.error = ""

    def prepare(
        self,
        identity: dict[str, Any] | None,
        rth_status: dict[str, Any] | None,
        now_utc: str,
        *,
        max_status_age: float = 60.0,
    ) -> bool:
        """Return whether ATR bars must be reset for a different contract/session.

        Session boundaries come from the broker's existing RTH resolver. No
        guessed weekday/holiday calendar and no wall-clock-to-monotonic replay.
        """
        self.now = _utc(now_utc)
        reset = False
        if identity != self.identity:
            old_identity = self.identity
            self.flush(force=False)
            # Period changes can rebuild the current raw bars; a contract or
            # profile change must never mix samples from different instruments.
            reset = old_identity is not None and (
                identity is None
                or {k: v for k, v in old_identity.items() if k not in {"period", "bar_seconds"}}
                != {k: v for k, v in identity.items() if k not in {"period", "bar_seconds"}}
            )
            self.identity = dict(identity) if identity is not None else None
            self.key = ""
            self.seed = None
            self.dirty = False
            self.observed_at = None
            self.last_write_epoch = float("-inf")
            self._close_flush_attempted_session = None
            if reset:
                self.last_session = None
                self.live_only = True
            if identity is not None:
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
                self.key = f"atr_rth_seed_v{ATR_SEED_VERSION}:{digest}"
                try:
                    record = self.storage.get_json(self.key, None)
                except Exception as exc:
                    self._error("read", exc)
                else:
                    if valid_atr_seed(record, identity, self.now):
                        self.seed = record
        self.session = None
        status = rth_status or {}
        try:
            opened, closed = _utc(status["session_open"]), _utc(status["session_close"])
            checked = _utc(status["checked_at"])
            # App event timestamps use whole seconds; broker status may retain
            # sub-second precision. Allow that rounding difference only.
            age = (self.now - checked).total_seconds()
            if status.get("is_open") is True and opened <= self.now < closed and -1.0 < age <= max(0.1, float(max_status_age)):
                self.session = (opened.isoformat(), closed.isoformat())
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        if self.session is None:
            self.flush(force=False)
        elif self.last_session != self.session:
            self.flush(force=False)
            reset = True
            self.live_only = True
            self.observed_at = None
            self.last_session = self.session
        return reset

    def note_observation(self, *, live: bool) -> None:
        if not live:
            # Mixed/delayed observations are not reusable live-session seeds.
            self.live_only = False
        if self.live_only and self.identity is not None and self.session is not None and self.now is not None and live:
            self.observed_at = self.now.isoformat()

    def apply(self, result: dict[str, Any]) -> dict[str, Any]:
        """Use the seed only while the current RTH calculation lacks enough bars."""
        result = dict(result)
        result["seeded"] = False
        result["live_bars_available"] = result.get("bars_available", 0)
        if self.identity is None or self.session is None or self.now is None:
            return result
        if result.get("ready") and self.observed_at and self.live_only:
            record = {
                "version": ATR_SEED_VERSION,
                "identity": dict(self.identity),
                "session_open": self.session[0],
                "session_close": self.session[1],
                "observed_at": self.observed_at,
                "snapshot": dict(result),
            }
            if valid_atr_seed(record, self.identity, self.now):
                if record != self.seed:
                    self.seed = record
                    self.dirty = True
                # Keep the latest ready estimate in memory throughout RTH;
                # persistence is limited to the closing window or app shutdown.
                self.flush(force=False)
        elif int(result.get("bars_available", 0)) < self.identity["period"] + 1 and valid_atr_seed(self.seed, self.identity, self.now):
            assert self.seed is not None
            seed_snapshot = self.seed["snapshot"]
            for key in ("atr", "atr_pct", "latest_close", "latest_bar_high", "latest_bar_low", "true_ranges_used"):
                result[key] = seed_snapshot.get(key)
            result.update({
                "ready": True,
                "seeded": True,
                "source": "saved_rth_atr_seed",
                "seed_session_open": self.seed["session_open"],
                "seed_session_close": self.seed["session_close"],
                "seed_observed_at": self.seed["observed_at"],
                "seed_bars_available": seed_snapshot["bars_available"],
                "reason": (
                    f"Starting from saved RTH ATR ({self.seed['observed_at']}); "
                    f"current RTH warmup {result.get('bars_available', 0)}/{self.identity['period'] + 1} bars. "
                    "Fresh market data and all order guards are still required."
                ),
            })
        result["seed_storage_error"] = self.error
        return result
