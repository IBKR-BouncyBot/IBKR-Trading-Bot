"""Tests for the privacy-reduced audit-bundle fixture preparation utility."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts.sanitize_audit_bundle import sanitize_audit_bundle

_EXPORT_NAMES = (
    "cycles",
    "orders",
    "executions",
    "events",
    "decision_events",
    "broker_events",
)


def _write_bundle(path: Path, exports: dict[str, list[dict[str, Any]]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({"kind": "synthetic test bundle"}))
        for name in _EXPORT_NAMES:
            archive.writestr(
                f"sqlite_exports/{name}.json",
                json.dumps(exports.get(name, [])),
            )


def _private_synthetic_exports() -> dict[str, list[dict[str, Any]]]:
    cycle_id = "private-cycle-uuid"
    order_ref = "IBKRBOT|IREN|CYCLE-000001|PRIVATE01|BUY_TRAIL"
    return {
        "cycles": [
            {
                "id": cycle_id,
                "cycle_number": 1,
                "account": "DU123456",
                "ticker": "IREN",
                "stage": "2_BUY_TRAIL_ACTIVE",
                "con_id": 526906130,
                "exchange": "SMART",
                "primary_exchange": "NASDAQ",
                "currency": "USD",
                "quantity": 234,
                "buy_filled_qty": 0,
                "sell_filled_qty": 0,
                "buy_order_ref": order_ref,
                "buy_order_id": 2444,
                "buy_perm_id": 52422824,
                "buy_status": "Inactive",
            }
        ],
        "orders": [
            {
                "cycle_id": cycle_id,
                "ticker": "IREN",
                "action": "BUY",
                "order_type": "TRAIL",
                "order_ref": order_ref,
                "order_id": 2444,
                "perm_id": 52422824,
                "quantity": 234,
                "trailing_percent": 0.14,
                "initial_stop_price": 42.5996,
                "status": "Inactive",
                "raw_json": '{"path":"C:\\\\Users\\\\Private\\\\file.txt"}',
            }
        ],
        "executions": [
            {
                "cycle_id": cycle_id,
                "ticker": "IREN",
                "side": "BUY",
                "order_ref": order_ref,
                "execution_id": "0001.private.execution",
                "shares": 1,
                "price": 42.6,
                "commission": 0.35,
                "currency": "USD",
                "account": "DU123456",
            }
        ],
        "events": [
            {"level": "WARN", "message": "C:\\Users\\Private\\secret.txt"},
            {"level": "INFO", "message": "normal"},
        ],
        "decision_events": [
            {"event_type": "ORDER_TERMINAL_WITHOUT_FILL", "account": "DU123456"}
        ],
        "broker_events": [
            {"event_type": "ORDER_ERROR", "order_id": 2444, "perm_id": 52422824}
        ],
    }


def test_sanitizer_is_deterministic_and_removes_private_identifiers(tmp_path: Path) -> None:
    bundle = tmp_path / "private-audit.zip"
    _write_bundle(bundle, _private_synthetic_exports())

    first = sanitize_audit_bundle(bundle)
    second = sanitize_audit_bundle(bundle)

    assert first == second
    assert first["sanitized"] is True
    assert first["counts"] == {
        "cycles": 1,
        "orders": 1,
        "executions": 1,
        "events": 2,
        "decision_events": 1,
        "broker_events": 1,
    }
    assert first["cycles"][0]["cycle"] == "CYCLE-0001"
    assert first["cycles"][0]["buy_order_ref"] == "ORDER-REF-0001"
    assert first["executions"][0]["execution"] == "EXECUTION-0001"

    serialized = json.dumps(first, sort_keys=True)
    for private_value in (
        "DU123456",
        "private-cycle-uuid",
        "PRIVATE01",
        "2444",
        "52422824",
        "0001.private.execution",
        "C:\\Users\\Private",
    ):
        assert private_value not in serialized


def test_sanitizer_handles_empty_exports(tmp_path: Path) -> None:
    bundle = tmp_path / "empty-audit.zip"
    _write_bundle(bundle, {})

    result = sanitize_audit_bundle(bundle)

    assert set(result["counts"].values()) == {0}
    assert result["cycles"] == []
    assert result["orders"] == []
    assert result["executions"] == []


def test_sanitizer_rejects_unsafe_zip_member_path(tmp_path: Path) -> None:
    bundle = tmp_path / "unsafe-audit.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escape.txt", "unsafe")

    with pytest.raises(ValueError, match="Unsafe ZIP member path"):
        sanitize_audit_bundle(bundle)


def test_sanitizer_requires_all_json_exports(tmp_path: Path) -> None:
    bundle = tmp_path / "incomplete-audit.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("sqlite_exports/cycles.json", "[]")

    with pytest.raises(ValueError, match="missing sqlite_exports/orders.json"):
        sanitize_audit_bundle(bundle)


def test_sanitizer_cli_writes_sorted_json(tmp_path: Path) -> None:
    bundle = tmp_path / "cli-audit.zip"
    output = tmp_path / "sanitized.json"
    _write_bundle(bundle, _private_synthetic_exports())
    script = Path(__file__).resolve().parents[1] / "scripts" / "sanitize_audit_bundle.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(bundle), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data == sanitize_audit_bundle(bundle)
    assert output.read_text(encoding="utf-8").endswith("\n")
