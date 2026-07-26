#!/usr/bin/env python3
"""Create a deterministic, privacy-reduced summary of a BouncyBot audit ZIP.

The output is intended as an intermediate artifact for manually authored replay
fixtures. It never copies the SQLite database, raw logs, account identifiers,
local paths, original broker order identifiers, permanent identifiers, or
original execution identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_MEMBERS = 128
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_EXPORT_PREFIX = "sqlite_exports/"
_REQUIRED_EXPORTS = (
    "cycles.json",
    "orders.json",
    "executions.json",
    "events.json",
    "decision_events.json",
    "broker_events.json",
)
_ACCOUNT_RE = re.compile(r"\b(?:DU|U)\d{5,}\b", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)")


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_zip_members(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > _MAX_MEMBERS:
        raise ValueError(f"Audit bundle contains too many members: {len(members)}")
    if sum(max(0, item.file_size) for item in members) > _MAX_UNCOMPRESSED_BYTES:
        raise ValueError("Audit bundle exceeds the uncompressed-size safety limit.")
    for item in members:
        path = PurePosixPath(item.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe ZIP member path: {item.filename}")


def _load_export(archive: zipfile.ZipFile, filename: str) -> list[dict[str, Any]]:
    member = f"{_EXPORT_PREFIX}{filename}"
    try:
        raw = archive.read(member)
    except KeyError as exc:
        raise ValueError(f"Audit bundle is missing {member}") from exc
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{member} must contain a JSON array of objects.")
    return value


def _aliaser(prefix: str):
    aliases: dict[str, str] = {}

    def alias(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text not in aliases:
            aliases[text] = f"{prefix}-{len(aliases) + 1:04d}"
        return aliases[text]

    return alias


def _number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _text(value: Any, *, maximum: int = 240) -> str:
    text = str(value or "").strip()
    text = _ACCOUNT_RE.sub("ACCOUNT-REDACTED", text)
    text = _WINDOWS_PATH_RE.sub("PATH-REDACTED", text)
    return text[:maximum]


def _safe_cycle(row: dict[str, Any], cycle_alias: Any, order_ref_alias: Any) -> dict[str, Any]:
    return {
        "cycle": cycle_alias(row.get("id")),
        "cycle_number": _number(row.get("cycle_number")),
        "ticker": _text(row.get("ticker"), maximum=24),
        "stage": _text(row.get("stage"), maximum=64),
        "con_id": _number(row.get("con_id")),
        "exchange": _text(row.get("exchange"), maximum=24),
        "primary_exchange": _text(row.get("primary_exchange"), maximum=24),
        "currency": _text(row.get("currency"), maximum=8),
        "quantity": _number(row.get("quantity")),
        "buy_filled_qty": _number(row.get("buy_filled_qty")),
        "sell_filled_qty": _number(row.get("sell_filled_qty")),
        "avg_buy_price": _number(row.get("avg_buy_price")),
        "avg_sell_price": _number(row.get("avg_sell_price")),
        "buy_commission": _number(row.get("buy_commission")),
        "sell_commission": _number(row.get("sell_commission")),
        "gross_pnl": _number(row.get("gross_pnl")),
        "net_pnl": _number(row.get("net_pnl")),
        "buy_order_ref": order_ref_alias(row.get("buy_order_ref")),
        "sell_order_ref": order_ref_alias(row.get("sell_order_ref")),
        "buy_status": _text(row.get("buy_status"), maximum=48),
        "sell_status": _text(row.get("sell_status"), maximum=48),
    }


def _safe_order(row: dict[str, Any], cycle_alias: Any, order_ref_alias: Any) -> dict[str, Any]:
    return {
        "cycle": cycle_alias(row.get("cycle_id")),
        "ticker": _text(row.get("ticker"), maximum=24),
        "action": _text(row.get("action"), maximum=16),
        "order_type": _text(row.get("order_type"), maximum=24),
        "order_ref": order_ref_alias(row.get("order_ref")),
        "quantity": _number(row.get("quantity")),
        "trailing_percent": _number(row.get("trailing_percent")),
        "initial_stop_price": _number(row.get("initial_stop_price")),
        "status": _text(row.get("status"), maximum=48),
    }


def _safe_execution(
    row: dict[str, Any],
    cycle_alias: Any,
    order_ref_alias: Any,
    execution_alias: Any,
) -> dict[str, Any]:
    return {
        "cycle": cycle_alias(row.get("cycle_id")),
        "ticker": _text(row.get("ticker"), maximum=24),
        "side": _text(row.get("side"), maximum=16),
        "order_ref": order_ref_alias(row.get("order_ref")),
        "execution": execution_alias(row.get("execution_id")),
        "shares": _number(row.get("shares")),
        "price": _number(row.get("price")),
        "commission": _number(row.get("commission")),
        "currency": _text(row.get("currency"), maximum=8),
    }


def sanitize_audit_bundle(path: Path) -> dict[str, Any]:
    """Return a privacy-reduced summary for one audit bundle ZIP."""
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    with zipfile.ZipFile(source_path) as archive:
        _validate_zip_members(archive)
        exports = {name: _load_export(archive, name) for name in _REQUIRED_EXPORTS}

    cycle_alias = _aliaser("CYCLE")
    order_ref_alias = _aliaser("ORDER-REF")
    execution_alias = _aliaser("EXECUTION")

    cycles = [
        _safe_cycle(row, cycle_alias, order_ref_alias)
        for row in exports["cycles.json"]
    ]
    orders = [
        _safe_order(row, cycle_alias, order_ref_alias)
        for row in exports["orders.json"]
    ]
    executions = [
        _safe_execution(row, cycle_alias, order_ref_alias, execution_alias)
        for row in exports["executions.json"]
    ]

    event_counts = Counter(_text(row.get("level"), maximum=24) for row in exports["events.json"])
    decision_counts = Counter(
        _text(row.get("event_type"), maximum=80) for row in exports["decision_events.json"]
    )
    broker_counts = Counter(
        _text(row.get("event_type"), maximum=80) for row in exports["broker_events.json"]
    )

    return {
        "schema_version": 1,
        "sanitized": True,
        "source": {
            "kind": "audit_bundle",
            "sha256": file_sha256(source_path),
        },
        "counts": {
            "cycles": len(cycles),
            "orders": len(orders),
            "executions": len(executions),
            "events": len(exports["events.json"]),
            "decision_events": len(exports["decision_events.json"]),
            "broker_events": len(exports["broker_events.json"]),
        },
        "cycles": cycles,
        "orders": orders,
        "executions": executions,
        "event_level_counts": dict(sorted(event_counts.items())),
        "decision_event_counts": dict(sorted(decision_counts.items())),
        "broker_event_counts": dict(sorted(broker_counts.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Input BouncyBot audit ZIP")
    parser.add_argument("output", type=Path, help="Output sanitized JSON summary")
    args = parser.parse_args()

    result = sanitize_audit_bundle(args.bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
