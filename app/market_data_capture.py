"""Bounded in-memory market-data capture around application fills.

The controller maintains a rolling API snapshot buffer. A BUY or SELL fill starts
a capture containing available pre-event rows and accumulating post-event rows
(default window: 15 minutes on each side). A ZIP is written to ``debug_captures``
only after the post window completes. Partial captures are intentionally not
flushed during shutdown, so an interrupted pending capture is lost.
"""

from __future__ import annotations

import csv
import io
import json
import queue
import re
import threading
import zipfile
from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _safe_name(value: Any, fallback: str = "unknown") -> str:
    text = str(value or fallback).strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[:80] or fallback


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    """Flatten nested dict-like values into CSV-safe scalar columns."""
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), child, out)
        return
    if isinstance(value, (list, tuple)):
        out[prefix] = json.dumps(value, default=_json_default, sort_keys=True)
        return
    out[prefix] = value


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        _flatten(str(key), value, out)
    return out


@dataclass(slots=True)
class PendingTradeCapture:
    event_id: str
    event_type: str
    event_monotonic: float
    post_window_seconds: float
    ticker: str
    cycle_id: str
    cycle_number: int | None
    order_ref: str
    perm_id: int | None
    pre_rows: list[dict[str, Any]]
    post_rows: list[dict[str, Any]] = field(default_factory=list)
    event_payload: dict[str, Any] = field(default_factory=dict)
    started_at_utc: str = field(default_factory=_utc_now_iso)

    @property
    def deadline_monotonic(self) -> float:
        return float(self.event_monotonic) + float(self.post_window_seconds)

    def all_rows(self) -> list[dict[str, Any]]:
        # Keep rows sorted and de-duplicate by monotonic timestamp + selected price.
        rows = list(self.pre_rows) + list(self.post_rows)
        rows.sort(key=lambda item: float(item.get("monotonic_ts") or 0.0))
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any]] = set()
        for row in rows:
            key = (row.get("monotonic_ts"), row.get("price"), row.get("source"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped


class MarketDataCaptureManager:
    """Bounded RAM buffer and post-trade ZIP capture writer.

    Parameters are intentionally plain seconds so tests can use short windows.
    The live controller uses 900 seconds for both pre- and post-trade windows.
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        pre_window_seconds: float = 15 * 60,
        post_window_seconds: float = 15 * 60,
        buffer_window_seconds: Optional[float] = None,
        max_rows: int = 100_000,
        enabled: bool = True,
        async_writes: bool = False,
    ):
        self.output_dir = Path(output_dir)
        self.pre_window_seconds = float(pre_window_seconds)
        self.post_window_seconds = float(post_window_seconds)
        self.buffer_window_seconds = float(buffer_window_seconds if buffer_window_seconds is not None else max(pre_window_seconds, post_window_seconds) + 60)
        self.enabled = bool(enabled)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_rows)))
        self._pending: dict[str, PendingTradeCapture] = {}
        self._completed_files: list[Path] = []
        self._async_writes = bool(async_writes)
        self._completed_lock = threading.Lock()
        self._write_queue: "queue.Queue[PendingTradeCapture | None]" = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def completed_files(self) -> list[Path]:
        with self._completed_lock:
            return list(self._completed_files)

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    def record_snapshot(self, snapshot: dict[str, Any], *, monotonic_ts: float, wall_time_utc: Optional[str] = None) -> None:
        if not self.enabled:
            return
        row = dict(snapshot or {})
        row["monotonic_ts"] = float(monotonic_ts)
        row["captured_at_utc"] = wall_time_utc or _utc_now_iso()
        self._buffer.append(row)
        self._prune(float(monotonic_ts))
        for pending in list(self._pending.values()):
            if float(monotonic_ts) >= pending.event_monotonic:
                pending.post_rows.append(row)
        self.finalize_ready(float(monotonic_ts))

    def _prune(self, now_monotonic: float) -> None:
        cutoff = float(now_monotonic) - self.buffer_window_seconds
        while self._buffer and float(self._buffer[0].get("monotonic_ts") or 0.0) < cutoff:
            self._buffer.popleft()

    def start_capture(
        self,
        *,
        event_type: str,
        event_monotonic: float,
        ticker: str,
        cycle_id: str,
        cycle_number: int | None = None,
        order_ref: str = "",
        perm_id: int | None = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> str:
        """Start a RAM-only capture and return the capture event id."""
        if not self.enabled:
            return ""
        event_id = f"{_stamp()}_{_safe_name(event_type)}_{_safe_name(ticker)}_{_safe_name(order_ref or cycle_id)}"
        start = float(event_monotonic) - self.pre_window_seconds
        pre_rows = [dict(row) for row in self._buffer if start <= float(row.get("monotonic_ts") or 0.0) <= float(event_monotonic)]
        pending = PendingTradeCapture(
            event_id=event_id,
            event_type=str(event_type),
            event_monotonic=float(event_monotonic),
            post_window_seconds=self.post_window_seconds,
            ticker=str(ticker or ""),
            cycle_id=str(cycle_id or ""),
            cycle_number=cycle_number,
            order_ref=str(order_ref or ""),
            perm_id=perm_id,
            pre_rows=pre_rows,
            event_payload=dict(payload or {}),
        )
        self._pending[event_id] = pending
        self.finalize_ready(float(event_monotonic))
        return event_id

    def finalize_ready(self, now_monotonic: Optional[float] = None) -> list[Path]:
        if not self.enabled:
            return []
        if now_monotonic is None:
            # Without a monotonic timestamp from the worker loop, do not infer
            # completion from wall time; tests and controller pass monotonic time.
            return []
        completed: list[Path] = []
        for event_id, pending in list(self._pending.items()):
            if float(now_monotonic) < pending.deadline_monotonic:
                continue
            if self._async_writes:
                self._ensure_writer_thread()
                self._write_queue.put(pending)
            else:
                path = self._write_capture(pending)
                completed.append(path)
                with self._completed_lock:
                    self._completed_files.append(path)
            del self._pending[event_id]
        return completed

    def _ensure_writer_thread(self) -> None:
        if not self._async_writes:
            return
        if self._writer_thread is not None and self._writer_thread.is_alive():
            return
        self._writer_thread = threading.Thread(target=self._writer_loop, name="IBKRMarketCaptureWriter", daemon=True)
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        while True:
            pending = self._write_queue.get()
            try:
                if pending is None:
                    return
                path = self._write_capture(pending)
                with self._completed_lock:
                    self._completed_files.append(path)
            finally:
                self._write_queue.task_done()

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        if not self._async_writes or self._writer_thread is None:
            return
        if wait:
            try:
                self._write_queue.join()
            except Exception:
                pass
        self._write_queue.put(None)
        if wait:
            self._writer_thread.join(timeout=max(0.1, float(timeout or 0.0)))

    def _write_capture(self, pending: PendingTradeCapture) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ticker = _safe_name(pending.ticker)
        cycle = _safe_name(f"cycle_{pending.cycle_number}" if pending.cycle_number is not None else pending.cycle_id)
        folder = self.output_dir / ticker / cycle
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{pending.event_id}.zip"
        path = folder / filename
        rows = pending.all_rows()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(self._manifest(pending, rows), default=_json_default, indent=2, sort_keys=True))
            zf.writestr("event.json", json.dumps(pending.event_payload, default=_json_default, indent=2, sort_keys=True))
            zf.writestr("market_data.csv", self._csv_text(rows))
            zf.writestr("market_data.jsonl", "".join(json.dumps(row, default=_json_default, sort_keys=True) + "\n" for row in rows))
        return path

    def _manifest(self, pending: PendingTradeCapture, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "event_id": pending.event_id,
            "event_type": pending.event_type,
            "ticker": pending.ticker,
            "cycle_id": pending.cycle_id,
            "cycle_number": pending.cycle_number,
            "order_ref": pending.order_ref,
            "perm_id": pending.perm_id,
            "started_at_utc": pending.started_at_utc,
            "finalized_at_utc": _utc_now_iso(),
            "pre_window_seconds": self.pre_window_seconds,
            "post_window_seconds": self.post_window_seconds,
            "rows": len(rows),
            "first_row_utc": rows[0].get("captured_at_utc") if rows else None,
            "last_row_utc": rows[-1].get("captured_at_utc") if rows else None,
            "disk_write_policy": "full_capture_only_no_partial_flush",
        }

    @staticmethod
    def _csv_text(rows: Iterable[dict[str, Any]]) -> str:
        flattened = [_flatten_row(row) for row in rows]
        columns: list[str] = []
        seen: set[str] = set()
        priority = [
            "captured_at_utc",
            "monotonic_ts",
            "ticker",
            "cycle_id",
            "cycle_number",
            "stage",
            "price",
            "source",
            "selected_market_data_type",
            "subscription_market_data_type",
            "rth_open",
            "api_data_state",
        ]
        for name in priority:
            if any(name in row for row in flattened):
                columns.append(name)
                seen.add(name)
        for row in flattened:
            for key in sorted(row):
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in flattened:
            writer.writerow({key: row.get(key, "") for key in columns})
        return out.getvalue()
