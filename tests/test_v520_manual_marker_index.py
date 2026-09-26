"""Safety-query equivalence across the additive 5.2 manual-marker index.

All databases are synthetic and local to pytest's temporary directory. The
pre-upgrade fixture has the 5.1 schema (the new index is absent), allowing the
real schema-opening path to exercise migration rather than test-only DDL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models import CycleState, Stage
from app.storage import BotStorage

INDEX = "idx_decision_events_manually_handled_cycle"
STAMP = "2026-09-25T09:00:00+00:00"


def _cycle(name: str, number: int = 1, **fields: Any) -> CycleState:
    values: dict[str, Any] = {
        "id": name,
        "cycle_number": number,
        "ticker": "AAA",
        "stage": Stage.STOPPED,
        "created_at": STAMP,
        "updated_at": STAMP,
        "con_id": 101,
    }
    values.update(fields)
    return CycleState(**values)


@pytest.fixture
def legacy_storage(tmp_path: Path) -> BotStorage:
    storage = BotStorage(tmp_path / "state.sqlite")
    with storage.connect() as con:
        con.execute(f"DROP INDEX IF EXISTS {INDEX}")
    return storage


def _unresolved(storage: BotStorage) -> list[dict[str, Any]]:
    return [cycle.to_dict() for cycle in storage.get_unresolved_cycles()]


def _rows(storage: BotStorage) -> dict[str, list[tuple[Any, ...]]]:
    with storage.connect() as con:
        names = [
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            name: [tuple(row) for row in con.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in names
        }


def _assert_reopen_preserves_safety_results(storage: BotStorage) -> BotStorage:
    before = _unresolved(storage)
    positions = [storage.get_app_owned_unsold_position("AAA", con_id=con_id) for con_id in (None, 101, 202)]
    reopened = BotStorage(storage.db_path)
    assert _unresolved(reopened) == before
    assert [reopened.get_app_owned_unsold_position("AAA", con_id=con_id) for con_id in (None, 101, 202)] == positions
    with reopened.connect() as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    return reopened


def test_fresh_schema_installs_nonunique_partial_cycle_index(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "fresh.sqlite")
    with storage.connect() as con:
        entries = [row for row in con.execute("PRAGMA index_list(decision_events)") if row["name"] == INDEX]
        assert len(entries) == 1
        assert entries[0]["unique"] == 0
        assert entries[0]["partial"] == 1
        assert [row["name"] for row in con.execute(f"PRAGMA index_info({INDEX})")] == ["cycle_id"]
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone()[0]
        assert "WHEREEVENT_TYPE='MANUALLY_HANDLED'" in "".join(sql.upper().split())
    assert storage.get_unresolved_cycles() == []
    assert storage.get_app_owned_unsold_position("AAA") == {"quantity": 0, "cycles": []}


def test_upgrade_and_repeated_open_preserve_every_application_row(legacy_storage: BotStorage) -> None:
    exposed = _cycle("exposed", buy_filled_qty=7)
    complete = _cycle("complete", 2, stage=Stage.CYCLE_COMPLETE, buy_filled_qty=9, sell_filled_qty=9)
    acknowledged = _cycle("acknowledged", 3, stage=Stage.MANUAL_REVIEW, buy_filled_qty=5)
    for cycle in (exposed, complete, acknowledged):
        legacy_storage.upsert_cycle(cycle)
        legacy_storage.add_decision_event(event_type="PRICE_OBSERVATION", message="Unchanged diagnostic", cycle=cycle)
    legacy_storage.add_decision_event(event_type="MANUALLY_HANDLED", message="External reconciliation", cycle=acknowledged)
    before = _rows(legacy_storage)

    storage = _assert_reopen_preserves_safety_results(legacy_storage)
    assert _rows(storage) == before
    storage = _assert_reopen_preserves_safety_results(storage)
    assert _rows(storage) == before
    with storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)).fetchone()[0] == 1
    assert [cycle.id for cycle in storage.get_unresolved_cycles()] == ["exposed"]
    assert storage.get_app_owned_unsold_position("AAA")["quantity"] == 7


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (Stage.IDLE, False),
        (Stage.WAIT_INITIAL_DROP, True),
        (Stage.BUY_TRAIL_ACTIVE, True),
        (Stage.WAIT_RISE_TRIGGER, True),
        (Stage.SELL_TRAIL_ACTIVE, True),
        (Stage.CYCLE_COMPLETE, False),
        (Stage.STOPPED, False),
        (Stage.MANUAL_REVIEW, True),
        (Stage.ERROR, True),
    ],
)
def test_flat_cycle_stage_remains_a_safety_blocker_only_where_required(
    legacy_storage: BotStorage, stage: Stage, expected: bool,
) -> None:
    legacy_storage.upsert_cycle(_cycle("stage-case", stage=stage))
    before = legacy_storage.get_unresolved_cycles()
    assert [cycle.id for cycle in before] == (["stage-case"] if expected else [])
    _assert_reopen_preserves_safety_results(legacy_storage)


@pytest.mark.parametrize("role", ["buy", "protective_sell", "sell"])
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Filled", False), ("Cancelled", False), ("ApiCancelled", False),
        ("Inactive", False), ("Rejected", False),
        ("PendingSubmit", True), ("PreSubmitted", True), ("Submitted", True),
        ("PendingCancel", True), ("UnrecognisedBrokerStatus", True), ("", True), (None, True),
    ],
)
def test_stopped_flat_cycle_keeps_unknown_or_working_order_exposure(
    legacy_storage: BotStorage, role: str, status: str | None, expected: bool,
) -> None:
    cycle = _cycle("order-case")
    setattr(cycle, f"{role}_order_ref", f"IBKRBOT|AAA|order-case|{role.upper()}")
    setattr(cycle, f"{role}_status", status)
    legacy_storage.upsert_cycle(cycle)
    assert [item.id for item in legacy_storage.get_unresolved_cycles()] == ([cycle.id] if expected else [])
    _assert_reopen_preserves_safety_results(legacy_storage)


@pytest.mark.parametrize("role", ["buy", "protective_sell", "sell"])
@pytest.mark.parametrize("order_ref", [None, ""])
def test_status_without_order_reference_does_not_invent_order_exposure(
    legacy_storage: BotStorage, role: str, order_ref: str | None,
) -> None:
    cycle = _cycle("no-reference")
    setattr(cycle, f"{role}_order_ref", order_ref)
    setattr(cycle, f"{role}_status", "Submitted")
    legacy_storage.upsert_cycle(cycle)
    assert legacy_storage.get_unresolved_cycles() == []
    _assert_reopen_preserves_safety_results(legacy_storage)


def test_mixed_history_preserves_full_ordered_outputs_and_exact_contract_exposure(legacy_storage: BotStorage) -> None:
    cycles = [
        _cycle("stopped", 1, buy_filled_qty=10, sell_filled_qty=3),
        _cycle("completed-unsold", 2, stage=Stage.CYCLE_COMPLETE, buy_filled_qty=5, sell_filled_qty=2),
        _cycle("protective-partial", 3, buy_filled_qty=9, sell_filled_qty=2, protective_sell_filled_qty=4),
        _cycle("legacy-null", 4, con_id=None, buy_filled_qty=2),
        _cycle("legacy-zero", 5, con_id=0, buy_filled_qty=3),
        _cycle("other-contract", 6, con_id=202, buy_filled_qty=11),
        _cycle("other-symbol", 7, ticker="BBB", buy_filled_qty=13),
        _cycle("marker-handled", 8, stage=Stage.ERROR, buy_filled_qty=17),
        _cycle("message-handled", 9, stage=Stage.MANUAL_REVIEW, buy_filled_qty=19,
               error_message="Operator: MARKED MANUALLY HANDLED after external verification"),
        _cycle("flat", 10, stage=Stage.CYCLE_COMPLETE, buy_filled_qty=4, sell_filled_qty=4),
        _cycle("protective-flat", 11, buy_filled_qty=4, protective_sell_filled_qty=4),
        _cycle("oversold", 12, buy_filled_qty=4, sell_filled_qty=6),
        _cycle("negative-buy", 13, buy_filled_qty=-1),
        _cycle("error-active", 14, stage=Stage.ERROR),
        _cycle("review-active", 15, stage=Stage.MANUAL_REVIEW),
        _cycle("tie-a", 16, stage=Stage.WAIT_INITIAL_DROP),
        _cycle("tie-z", 16, stage=Stage.WAIT_INITIAL_DROP),
        _cycle("newer-timestamp", 0, stage=Stage.WAIT_INITIAL_DROP, updated_at="2026-09-25T09:00:01+00:00"),
    ]
    for cycle in cycles:
        legacy_storage.upsert_cycle(cycle)
        legacy_storage.add_decision_event(event_type="STAGE_CHANGE", message="Fixture audit", cycle=cycle)
    legacy_storage.add_decision_event(event_type="MANUALLY_HANDLED", message="Operator acknowledgement", cycle=cycles[7])

    expected_ids = [
        "newer-timestamp", "tie-z", "tie-a", "review-active", "error-active",
        "other-symbol", "other-contract", "legacy-zero", "legacy-null",
        "protective-partial", "completed-unsold", "stopped",
    ]
    assert [cycle.id for cycle in legacy_storage.get_unresolved_cycles()] == expected_ids
    expected_position = {
        "quantity": 20,
        "cycles": [
            {"cycle_id": "stopped", "cycle_number": 1, "stage": Stage.STOPPED.value, "con_id": 101, "quantity": 7},
            {"cycle_id": "completed-unsold", "cycle_number": 2, "stage": Stage.CYCLE_COMPLETE.value, "con_id": 101, "quantity": 3},
            {"cycle_id": "protective-partial", "cycle_number": 3, "stage": Stage.STOPPED.value, "con_id": 101, "quantity": 5},
            {"cycle_id": "legacy-null", "cycle_number": 4, "stage": Stage.STOPPED.value, "con_id": None, "quantity": 2},
            {"cycle_id": "legacy-zero", "cycle_number": 5, "stage": Stage.STOPPED.value, "con_id": None, "quantity": 3},
        ],
    }
    # These quantities are independent expectations, not derived by copying the SQL.
    assert legacy_storage.get_app_owned_unsold_position(" aaa ", con_id=101) == expected_position
    assert legacy_storage.get_app_owned_unsold_position("AAA", con_id=202)["quantity"] == 16
    assert legacy_storage.get_app_owned_unsold_position("AAA")["quantity"] == 31
    assert legacy_storage.get_app_owned_unsold_position("BBB", con_id=101)["quantity"] == 13
    assert legacy_storage.get_app_owned_unsold_position("") == {"quantity": 0, "cycles": []}
    before = _rows(legacy_storage)
    storage = _assert_reopen_preserves_safety_results(legacy_storage)
    assert _rows(storage) == before
    assert [cycle.id for cycle in storage.get_unresolved_cycles()] == expected_ids


def test_committed_manual_marker_changes_safety_queries_immediately_but_rollback_does_not(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "state.sqlite")
    cycle = _cycle("position", buy_filled_qty=8)
    storage.upsert_cycle(cycle)
    before = _unresolved(storage)
    assert len(before) == 1
    with storage.connect() as con:
        con.execute(
            "INSERT INTO decision_events(created_at, event_type, cycle_id, message) VALUES (?, 'MANUALLY_HANDLED', ?, ?)",
            (STAMP, cycle.id, "Uncommitted acknowledgement"),
        )
        con.rollback()
    assert _unresolved(storage) == before
    assert storage.get_app_owned_unsold_position("AAA")["quantity"] == 8

    storage.add_decision_event(event_type="MANUALLY_HANDLED", message="Committed acknowledgement", cycle=cycle)
    assert storage.get_unresolved_cycles() == []
    assert storage.get_app_owned_unsold_position("AAA") == {"quantity": 0, "cycles": []}
    with storage.connect() as con:
        con.execute("DELETE FROM decision_events WHERE event_type='MANUALLY_HANDLED' AND cycle_id=?", (cycle.id,))
    assert _unresolved(storage) == before
    assert storage.get_app_owned_unsold_position("AAA")["quantity"] == 8


def test_partial_index_contains_only_markers_and_accepts_duplicate_or_unassociated_events(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "state.sqlite")
    cycle = _cycle("marker-index", buy_filled_qty=2)
    storage.upsert_cycle(cycle)
    for event_type in ("PRICE_OBSERVATION", "STAGE_CHANGE", "MANUALLY_HANDLED", "MANUALLY_HANDLED"):
        storage.add_decision_event(event_type=event_type, message="Fixture event", cycle=cycle)
    storage.add_decision_event(event_type="MANUALLY_HANDLED", message="Unassociated marker")
    with storage.connect() as con:
        rows = con.execute(
            f"SELECT cycle_id FROM decision_events INDEXED BY {INDEX} WHERE event_type='MANUALLY_HANDLED' ORDER BY cycle_id"
        ).fetchall()
        assert [row[0] for row in rows] == [None, cycle.id, cycle.id]
        assert con.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 5
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert storage.get_unresolved_cycles() == []


def test_real_safety_queries_use_partial_index_without_changing_read_only_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "state.sqlite")
    cycle = _cycle("query-plan", buy_filled_qty=3)
    storage.upsert_cycle(cycle)
    with storage.connect() as con:
        con.executemany(
            "INSERT INTO decision_events(created_at, event_type, cycle_id, message) VALUES (?, 'PRICE_OBSERVATION', ?, ?)",
            [(STAMP, cycle.id, "Ordinary historical observation")] * 100,
        )
    trace: list[str] = []
    original_connect = storage.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(trace.append)
        return connection

    monkeypatch.setattr(storage, "connect", traced_connect)
    assert [item.id for item in storage.get_unresolved_cycles()] == [cycle.id]
    assert storage.get_app_owned_unsold_position("AAA", con_id=101)["quantity"] == 3
    queries = [sql for sql in trace if sql.lstrip().upper().startswith("SELECT")]
    assert len(queries) == 2
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT")) for sql in trace)
    with original_connect() as con:
        for sql in queries:
            plan = [row[3] for row in con.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any(INDEX in detail for detail in plan), plan
