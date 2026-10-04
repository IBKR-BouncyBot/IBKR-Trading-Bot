"""Exercise real SQLite backups and restore validation without broker access."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from app.models import ConnectionSettings, Stage, StrategySettings
from app.storage import BotStorage, _ClosingSqliteConnection
from app.strategy import StrategyEngine


def _application_rows(path: Path) -> dict:
    """Read every table, including persisted order/execution IDs and timestamps."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            name: (
                tuple(row[1] for row in con.execute(f'PRAGMA table_info("{name}")')),
                sorted(con.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr),
            )
            for name in tables
        }


@contextmanager
def _tracked_backups(*, fail_on: int = 0):
    """Observe actual SQLite copies; do not replace their implementation."""
    calls = []
    original = _ClosingSqliteConnection.backup

    def tracked(source, target, *args, **kwargs):
        source_path = Path(source.execute("PRAGMA database_list").fetchone()[2])
        target_path = Path(target.execute("PRAGMA database_list").fetchone()[2])
        calls.append((source_path, target_path))
        if len(calls) == fail_on:
            raise sqlite3.OperationalError("injected backup I/O failure")
        return original(source, target, *args, **kwargs)

    with patch.object(_ClosingSqliteConnection, "backup", tracked):
        yield calls


class BackupValidationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        # Spaces and URI metacharacters must remain literal path characters.
        self.path = self.root / "active #state.sqlite"
        self.storage = BotStorage(self.path)
        self.settings = StrategySettings(ticker="TEST", hard_risk_limits_enabled=False)
        self.cycle = StrategyEngine.start_cycle(self.settings, 1, "SIM", 100.0, 0.0)
        self.cycle.stage = Stage.WAIT_RISE_TRIGGER
        self.cycle.buy_filled_qty = 7
        self.cycle.avg_buy_price = 99.5
        self.storage.upsert_cycle(self.cycle)
        self.storage.add_order(
            cycle=self.cycle, action="BUY", order_type="TRAIL", order_id=123,
            perm_id=456, order_ref="TEST-BUY", quantity=7, trailing_percent=0.5,
            initial_stop_price=99.5, status="Filled", raw={"synthetic": True},
        )
        self.storage.add_execution(
            cycle=self.cycle, ticker="TEST", side="BOT", shares=7, price=99.5,
            commission=0.75, execution_id="synthetic-fill", order_ref="TEST-BUY",
            order_id=123, perm_id=456,
        )
        self.storage.add_decision_event(event_type="BUY_FILLED", message="Synthetic fill", cycle=self.cycle)
        self.storage.save_resume_checkpoint(
            ConnectionSettings(), self.settings, self.cycle,
            reason="test_checkpoint", checkpoint_id="synthetic-checkpoint",
        )

    def test_backup_uses_two_copies_and_preserves_all_application_rows(self):
        before = _application_rows(self.path)
        copied_rows = []
        validate = BotStorage._validate_sqlite_database_file

        def observe_validation(path):
            if path.name == "restore_candidate.sqlite":
                copied_rows.append(_application_rows(path))
                self.assertFalse((path.parent / "backups").exists())
            return validate(path)

        with _tracked_backups() as calls, patch.object(
            BotStorage, "_validate_sqlite_database_file", staticmethod(observe_validation),
        ):
            backup = self.storage.backup_database("preserve_state")

        self.assertIsNotNone(backup)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], (self.path, backup))
        self.assertEqual(calls[1][0], backup)
        self.assertFalse(calls[1][1].parent.exists())
        self.assertEqual(_application_rows(self.path), before)
        self.assertEqual(_application_rows(backup), before)
        self.assertEqual(copied_rows, [before])
        report = json.loads((self.root / "backups" / "latest_restore_validation.json").read_text())
        self.assertTrue(report["ok"])
        self.assertTrue(report["restore_copy_validated"])
        self.assertEqual(report["path"], str(backup))

    def test_ordinary_existing_database_open_retains_pre_schema_backup(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
            # Legacy databases predate schema stamping.
            con.execute("PRAGMA user_version = 0")
        before = _application_rows(self.path)

        with _tracked_backups() as calls:
            reopened = BotStorage(self.path)

        self.assertEqual(len(calls), 1)
        source, backup = calls[0]
        self.assertEqual(source, self.path)
        self.assertTrue(backup.name.endswith("_before_schema_check.sqlite"))
        self.assertEqual(_application_rows(backup), before)
        self.assertIn("primary_exchange", _application_rows(reopened.db_path)["cycles"][0])
        self.assertEqual(reopened.get_execution("synthetic-fill")["shares"], 7)

    def test_failed_ordinary_migration_keeps_its_pre_schema_backup(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("PRAGMA user_version = 0")
        before = _application_rows(self.path)
        with _tracked_backups() as calls, patch.object(
            BotStorage, "_add_column_if_missing", side_effect=sqlite3.OperationalError("injected migration failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected migration failure"):
                BotStorage(self.path)

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].exists())
        self.assertEqual(_application_rows(calls[0][1]), before)
        self.assertEqual(_application_rows(self.path), before)

    def test_legacy_restore_migrates_only_disposable_copy_without_nested_backup(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
            con.execute("ALTER TABLE cycles DROP COLUMN protective_sell_enabled")
            con.execute("PRAGMA user_version = 0")
        before = self.path.read_bytes()
        rows_before = _application_rows(self.path)
        candidate_columns = []
        validate = BotStorage._validate_sqlite_database_file

        def observe_validation(path):
            if path != self.path:
                candidate_columns.append(_application_rows(path)["cycles"][0])
                self.assertFalse((path.parent / "backups").exists())
            return validate(path)

        with _tracked_backups() as calls, patch.object(
            BotStorage, "_validate_sqlite_database_file", staticmethod(observe_validation),
        ):
            result = self.storage.validate_restore_candidate(self.path)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["restore_copy_validated"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(candidate_columns), 1)
        self.assertIn("primary_exchange", candidate_columns[0])
        self.assertIn("protective_sell_enabled", candidate_columns[0])
        self.assertFalse(calls[0][1].parent.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(_application_rows(self.path), rows_before)

    def test_committed_wal_is_included_in_backup_and_restore_candidate(self):
        for action in ("validate", "backup"):
            with self.subTest(action=action), closing(sqlite3.connect(self.path)) as writer:
                writer.execute("PRAGMA wal_autocheckpoint=0")
                # Pin an older reader so backup_database's PASSIVE checkpoint
                # cannot move the newly committed trading state into the main file.
                with closing(sqlite3.connect(self.path)) as reader:
                    reader.execute("BEGIN")
                    reader.execute("SELECT * FROM app_settings").fetchall()
                    key = "wal_" + action
                    writer.execute(
                        "INSERT INTO app_settings VALUES (?, ?, ?)",
                        (key, json.dumps({"committed": action}), "2026-01-01T00:00:00+00:00"),
                    )
                    writer.commit()
                    self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
                    main_only = self.root / (action + "_main_only.sqlite")
                    main_only.write_bytes(self.path.read_bytes())
                    with closing(sqlite3.connect(main_only)) as con:
                        self.assertIsNone(con.execute("SELECT value_json FROM app_settings WHERE key=?", (key,)).fetchone())
                    rows_before = _application_rows(self.path)
                    candidate_rows = []
                    validate = BotStorage._validate_sqlite_database_file

                    def observe_validation(path):
                        if path.name == "restore_candidate.sqlite":
                            candidate_rows.append(_application_rows(path))
                        return validate(path)

                    with _tracked_backups() as calls, patch.object(
                        BotStorage, "_validate_sqlite_database_file", staticmethod(observe_validation),
                    ):
                        if action == "validate":
                            result = self.storage.validate_restore_candidate(self.path)
                            self.assertTrue(result["ok"], result)
                        else:
                            backup = self.storage.backup_database("committed_wal")
                            self.assertIsNotNone(backup)
                            self.assertEqual(_application_rows(backup), rows_before)
                    self.assertEqual(len(calls), 1 if action == "validate" else 2)
                    self.assertEqual(candidate_rows, [rows_before])
                    self.assertEqual(_application_rows(self.path), rows_before)
                    self.assertFalse(calls[-1][1].parent.exists())

    def test_copy_failures_remove_partial_backup_and_keep_existing_recovery_point(self):
        existing = self.storage.backup_database("existing")
        report = self.root / "backups" / "latest_restore_validation.json"
        existing_report = report.read_bytes()
        before = _application_rows(self.path)
        for failure_number in (1, 2):
            with self.subTest(copy=failure_number), _tracked_backups(fail_on=failure_number) as calls:
                result = self.storage.backup_database("injected_failure", keep=1)
            self.assertIsNone(result)
            self.assertEqual(len(calls), failure_number)
            self.assertFalse(calls[0][1].exists())
            if failure_number == 2:
                self.assertFalse(calls[1][1].parent.exists())
            self.assertEqual(self.storage.list_database_backups(), [existing])
            self.assertEqual(report.read_bytes(), existing_report)
            self.assertEqual(_application_rows(self.path), before)

    def test_candidate_migration_failure_is_reported_and_cleaned_up(self):
        before = _application_rows(self.path)
        with _tracked_backups() as calls, patch.object(
            BotStorage, "_add_column_if_missing", side_effect=sqlite3.OperationalError("injected candidate migration failure"),
        ):
            result = self.storage.validate_restore_candidate(self.path)
        self.assertFalse(result["ok"])
        self.assertFalse(result["restore_copy_validated"])
        self.assertIn("injected candidate migration failure", result["error"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][1].parent.exists())
        self.assertEqual(_application_rows(self.path), before)

    def test_post_migration_integrity_failure_rejects_backup_without_rotation(self):
        existing = self.storage.backup_database("existing")
        before = _application_rows(self.path)
        report = self.root / "backups" / "latest_restore_validation.json"
        existing_report = report.read_bytes()
        validate = BotStorage._validate_sqlite_database_file

        def reject_candidate(path):
            result = validate(path)
            if path.name == "restore_candidate.sqlite":
                result.update(ok=False, error="injected candidate integrity failure")
            return result

        with _tracked_backups() as calls, patch.object(
            BotStorage, "_validate_sqlite_database_file", staticmethod(reject_candidate),
        ):
            validation = self.storage.validate_restore_candidate(self.path)
            backup = self.storage.backup_database("invalid_candidate", keep=1)

        self.assertFalse(validation["ok"])
        self.assertFalse(validation["restore_copy_validated"])
        self.assertEqual(validation["error"], "injected candidate integrity failure")
        self.assertIsNone(backup)
        self.assertEqual(len(calls), 3)
        self.assertFalse(calls[0][1].parent.exists())
        self.assertFalse(calls[2][1].parent.exists())
        self.assertFalse(calls[1][1].exists())
        self.assertEqual(self.storage.list_database_backups(), [existing])
        self.assertEqual(report.read_bytes(), existing_report)
        self.assertEqual(_application_rows(self.path), before)

    def test_invalid_source_is_rejected_before_any_copy(self):
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"not a SQLite database")
        with _tracked_backups() as calls:
            result = self.storage.validate_restore_candidate(broken)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertEqual(calls, [])
        self.assertEqual(broken.read_bytes(), b"not a SQLite database")

    def test_rotation_retains_latest_validated_backups(self):
        paths = []
        # Controlled distinct mtimes avoid sleeps and filesystem clock granularity.
        with _tracked_backups() as calls:
            for number in range(5):
                backup = self.storage.backup_database(f"rotation_{number}", keep=3)
                self.assertIsNotNone(backup)
                paths.append(backup)
                os.utime(backup, (1_700_000_000 + number, 1_700_000_000 + number))
        self.assertEqual(len(calls), 10)
        self.assertEqual(set(self.storage.list_database_backups()), set(paths[-3:]))
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        latest = self.storage.validate_latest_backup()
        self.assertTrue(latest["ok"], latest)
        self.assertEqual(latest["path"], str(paths[-1]))
        self.assertTrue(all(not candidate.parent.exists() for _, candidate in calls if candidate.name == "restore_candidate.sqlite"))

    def test_audit_export_retains_validated_database_snapshot_and_state(self):
        before = _application_rows(self.path)
        snapshot = {"active_cycle": self.cycle.snapshot(), "connected": False}
        with _tracked_backups() as calls:
            archive = self.storage.create_audit_export_bundle(snapshot=snapshot)
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1][1].parent.exists())
        with zipfile.ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertTrue(manifest["backup_validation"]["ok"])
            self.assertEqual(json.loads(bundle.read("snapshot.json")), snapshot)
            self.assertIn("sqlite_exports/cycles.json", bundle.namelist())
            self.assertIn("sqlite_exports/executions.json", bundle.namelist())
            extracted = self.root / "exported_backup.sqlite"
            extracted.write_bytes(bundle.read("database/bot_state_backup.sqlite"))
        self.assertEqual(_application_rows(extracted), before)
        self.assertEqual(_application_rows(self.path), before)
        self.assertTrue(self.storage.validate_backup(extracted)["ok"])
