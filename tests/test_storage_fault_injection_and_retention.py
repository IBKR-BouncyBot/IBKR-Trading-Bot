"""Failure-injection tests for SQLite backups, logs, exports, and captures."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import app.storage as storage_module
from app.market_data_capture import MarketDataCaptureManager
from app.storage import BotStorage


def test_human_log_failure_does_not_rollback_sqlite_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    original_open = Path.open

    def failing_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == "audit_events_readable.log":
            raise OSError("injected readable-log failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    storage.add_event("WARN", "must remain in SQLite", ticker="AAPL")

    events = storage.get_recent_events(10)
    assert len(events) == 1
    assert events[0]["message"] == "must remain in SQLite"


def test_restore_validation_copy_failure_is_reported_without_touching_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    backup = storage.backup_database("seed")
    assert backup is not None
    original_bytes = backup.read_bytes()

    def fail_copy(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("injected restore-copy failure")

    monkeypatch.setattr(storage_module.shutil, "copy2", fail_copy)
    result = storage.validate_restore_candidate(backup)

    assert result["ok"] is False
    assert result["restore_copy_validated"] is False
    assert "injected restore-copy failure" in result["error"]
    assert backup.read_bytes() == original_bytes


def test_new_backup_is_deleted_when_restore_candidate_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    monkeypatch.setattr(
        storage,
        "validate_restore_candidate",
        lambda path: {"path": str(path), "ok": False, "error": "injected validation failure"},
    )

    assert storage.backup_database("must_not_survive") is None
    assert list((tmp_path / "backups").glob("*must_not_survive.sqlite")) == []


def test_validation_report_write_failure_does_not_invalidate_good_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    original_write_text = Path.write_text

    def failing_write_text(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path.name == "latest_restore_validation.json":
            raise OSError("injected report failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    backup = storage.backup_database("valid_without_report")

    assert backup is not None and backup.exists()
    assert storage.validate_backup(backup)["ok"] is True
    assert not (tmp_path / "backups" / "latest_restore_validation.json").exists()


@pytest.mark.parametrize("keep", [1, 3, 5])
def test_rotation_retains_exactly_newest_requested_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep: int,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    counter = iter(range(20))
    monkeypatch.setattr(
        storage_module,
        "_backup_stamp",
        lambda: f"20260711T1200{next(counter):02d}000000Z",
    )

    created = [storage.backup_database(f"rotation_{index}", keep=keep) for index in range(8)]
    backups = storage.list_database_backups()

    assert all(path is not None for path in created)
    assert len(backups) == keep
    expected = {path.name for path in created[-keep:] if path is not None}
    assert {path.name for path in backups} == expected


def test_backup_reason_is_filename_sanitized_and_bounded(tmp_path: Path) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    backup = storage.backup_database("order submit: BUY / unsafe<>chars and an excessively long reason")

    assert backup is not None
    assert ":" not in backup.name
    assert "/" not in backup.name
    assert "<" not in backup.name
    assert ">" not in backup.name
    assert len(backup.stem.split("_")[-1]) <= 40


def test_missing_tables_and_corrupt_files_fail_restore_readiness(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete.sqlite"
    with closing(sqlite3.connect(incomplete)) as con, con:
        con.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY)")
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not sqlite")

    missing_result = BotStorage._validate_sqlite_database_file(incomplete)
    corrupt_result = BotStorage._validate_sqlite_database_file(corrupt)

    assert missing_result["ok"] is False
    assert "cycles" in missing_result["missing_tables"]
    assert corrupt_result["ok"] is False
    assert corrupt_result["error"]


def test_audit_export_falls_back_to_explicitly_unvalidated_active_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    storage.add_event("INFO", "fallback export")
    monkeypatch.setattr(storage, "backup_database", lambda *args, **kwargs: None)

    bundle = storage.create_audit_export_bundle(snapshot={"status": "fallback"})
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))

    assert "database/bot_state_unvalidated.sqlite" in names
    assert "database/bot_state_backup.sqlite" not in names
    assert manifest["backup_validation"]["ok"] is False
    assert manifest["snapshot_included"] is True


def test_synchronous_capture_write_failure_is_not_reported_as_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MarketDataCaptureManager(
        tmp_path / "captures",
        pre_window_seconds=1.0,
        post_window_seconds=1.0,
        max_rows=10,
    )
    manager.record_snapshot({"price": 100.0}, monotonic_ts=1.0)
    event_id = manager.start_capture(
        event_type="BUY_FILL",
        event_monotonic=1.0,
        ticker="AAPL",
        cycle_id="cycle-1",
    )

    def fail_write(*args: Any, **kwargs: Any) -> Path:
        del args, kwargs
        raise OSError("injected capture-write failure")

    monkeypatch.setattr(manager, "_write_capture", fail_write)
    with pytest.raises(OSError, match="capture-write failure"):
        manager.finalize_ready(2.1)

    assert manager.pending_count == 1
    assert manager.completed_files == []
    assert event_id
