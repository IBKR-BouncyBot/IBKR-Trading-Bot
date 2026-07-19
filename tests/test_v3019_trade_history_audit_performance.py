"""Regression tests for responsive Trade History audit-log opening."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import BotStorage
from tests.support.qt_stubs import imported_gui_with_stubs


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(Path.cwd()) as module:
        yield module


def _cycle_row() -> dict[str, object]:
    return {
        "id": "cycle-1",
        "ticker": "AAPL",
        "cycle_number": 1,
        "stage": "5_CYCLE_COMPLETE",
        "created_at": "2026-07-19T12:00:00+00:00",
        "updated_at": "2026-07-19T13:00:00+00:00",
        "buy_filled_at": "2026-07-19T12:15:00+00:00",
        "sell_filled_at": "2026-07-19T12:45:00+00:00",
        "anchor_price": 100.0,
        "drop_trigger_price": 98.0,
        "avg_buy_price": 99.0,
        "avg_sell_price": 103.0,
        "buy_filled_qty": 10,
        "sell_filled_qty": 10,
        "gross_pnl": 40.0,
        "net_pnl": 38.0,
    }


def _audit_details(row: dict[str, object]) -> dict[str, object]:
    return {
        "cycle": row,
        "orders": [],
        "executions": [],
        "events": [],
        "decision_events": [],
    }


def test_storage_creates_ordered_cycle_indexes_for_audit_events(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "bot.sqlite")
    with storage.connect() as con:
        event_plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM events WHERE cycle_id=? ORDER BY created_at ASC, id ASC",
            ("cycle-1",),
        ).fetchall()
        decision_plan = con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM decision_events WHERE cycle_id=? ORDER BY created_at ASC, id ASC",
            ("cycle-1",),
        ).fetchall()

    assert any("idx_events_cycle_created" in str(row[3]) for row in event_plan)
    assert any("idx_decision_events_cycle_created" in str(row[3]) for row in decision_plan)
    assert all("SCAN events" not in str(row[3]) for row in event_plan)
    assert all("SCAN decision_events" not in str(row[3]) for row in decision_plan)


def test_exact_capture_folder_avoids_recursive_archive_scan(
    gui_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _cycle_row()
    details = _audit_details(row)
    base = tmp_path / "debug_captures"
    exact_dir = base / "AAPL" / "cycle_1"
    exact_dir.mkdir(parents=True)
    expected = [exact_dir / "buy.zip", exact_dir / "sell.zip"]
    for path in expected:
        path.write_bytes(b"capture")

    unrelated = base / "MSFT" / "cycle_1"
    unrelated.mkdir(parents=True)
    (unrelated / "other.zip").write_bytes(b"other")

    monkeypatch.setattr(gui_module, "debug_captures_dir", lambda: base)

    def fail_rglob(self: Path, pattern: str):
        raise AssertionError(f"unexpected recursive scan of {self} with {pattern}")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    candidates = gui_module.CycleAuditDialog._candidate_capture_files(row, details)

    assert candidates == expected


def test_capture_cycle_token_matching_does_not_confuse_cycle_1_and_cycle_10(gui_module) -> None:
    assert gui_module.CycleAuditDialog._capture_path_matches_expected(
        Path("debug_captures/AAPL/cycle_1/capture.zip"),
        ticker="AAPL",
        cycle_number="1",
        cycle_id="",
        capture_ids=set(),
    ) is True
    assert gui_module.CycleAuditDialog._capture_path_matches_expected(
        Path("debug_captures/AAPL/cycle_10/capture.zip"),
        ticker="AAPL",
        cycle_number="1",
        cycle_id="",
        capture_ids=set(),
    ) is False


def test_dialog_constructor_defers_capture_zip_loading_until_requested(
    gui_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _cycle_row()
    details = _audit_details(row)
    calls: list[str] = []

    def fake_loader(cls, selected_row, selected_details):
        del cls, selected_details
        calls.append(str(selected_row.get("id")))
        return ([{"captured_at_utc": "2026-07-19T12:00:00+00:00", "price": 100.0}], ["capture.zip"])

    monkeypatch.setattr(
        gui_module.CycleAuditDialog,
        "_load_market_capture_rows",
        classmethod(fake_loader),
    )

    dialog = gui_module.CycleAuditDialog(row, details)
    assert calls == []
    dialog._queue_materialize_tab(dialog._orders_tab_index)
    dialog._build_orders_tab()
    dialog._build_executions_tab()
    dialog._build_decision_events_tab()
    dialog._build_raw_log_tab()

    dialog._materialize_tab(dialog._timeline_tab_index)
    assert calls == ["cycle-1"]
    assert dialog._market_capture_loaded is True

    dialog._materialize_tab(dialog._market_capture_tab_index)
    assert calls == ["cycle-1"]
