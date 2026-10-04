"""Schema-version and backup-retention regressions using synthetic SQLite files."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.models import ConnectionSettings, Stage, StrategySettings
from app.storage import BACKUP_KEEP_DEFAULT, SCHEMA_VERSION, BotStorage, DatabaseSchemaError
from app.strategy import StrategyEngine
from tests.test_v520_backup_validation import _application_rows, _tracked_backups


def _schema_version(path: Path) -> int:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        return int(con.execute("PRAGMA user_version").fetchone()[0])


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "active #state.sqlite"
        self.storage = BotStorage(self.path)
        settings = StrategySettings(ticker="TEST", hard_risk_limits_enabled=False)
        cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.buy_filled_qty = 7
        cycle.avg_buy_price = 99.5
        self.storage.upsert_cycle(cycle)
        self.storage.add_order(
            cycle=cycle, action="BUY", order_type="TRAIL", order_id=123,
            perm_id=456, order_ref="TEST-BUY", quantity=7, trailing_percent=0.5,
            initial_stop_price=99.5, status="Filled", raw={"synthetic": True},
        )
        self.storage.add_execution(
            cycle=cycle, ticker="TEST", side="BOT", shares=7, price=99.5,
            commission=0.75, execution_id="synthetic-fill", order_ref="TEST-BUY",
            order_id=123, perm_id=456,
        )
        self.storage.save_resume_checkpoint(
            ConnectionSettings(), settings, cycle,
            reason="test_checkpoint", checkpoint_id="synthetic-checkpoint",
        )

    def _backup_files(self) -> dict[str, str]:
        folder = self.root / "backups"
        if not folder.exists():
            return {}
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.iterdir()
            if path.is_file() and path.suffix in {".sqlite", ".json"}
        }

    def _seed_excess_backups(self) -> None:
        backup = self.storage.backup_database("known_good")
        self.assertIsNotNone(backup)
        for number in range(BACKUP_KEEP_DEFAULT + 2):
            target = backup.parent / f"bot_state_legacy_{number:02d}.sqlite"
            target.write_bytes(backup.read_bytes())

    def test_current_schema_reopens_without_copying_changing_rows_or_pruning(self):
        self.assertEqual(_schema_version(self.path), SCHEMA_VERSION)
        self._seed_excess_backups()
        before = _application_rows(self.path)
        backups_before = self._backup_files()

        with _tracked_backups() as copies:
            reopened = BotStorage(self.path)

        self.assertEqual(copies, [])
        self.assertEqual(_schema_version(self.path), SCHEMA_VERSION)
        self.assertEqual(_application_rows(self.path), before)
        self.assertEqual(self._backup_files(), backups_before)
        self.assertEqual(reopened.get_execution("synthetic-fill")["shares"], 7)

    def test_schema_lookup_does_not_create_a_missing_database(self):
        storage = object.__new__(BotStorage)
        storage.db_path = self.root / "missing #database.sqlite"

        self.assertIsNone(storage._stored_schema_version())
        self.assertFalse(storage.db_path.exists())

    def test_legacy_migration_backup_preserves_committed_wal_and_old_schema(self):
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            with closing(sqlite3.connect(self.path)) as reader:
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM app_settings").fetchall()
                writer.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
                writer.execute("PRAGMA user_version = 0")
                writer.execute(
                    "INSERT INTO events(created_at,level,message) VALUES (?,?,?)",
                    ("2026-09-27T10:00:00+00:00", "INFO", "committed in WAL before migration"),
                )
                writer.commit()
                self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
                before = _application_rows(self.path)
                main_only = self.root / "main_only.sqlite"
                main_only.write_bytes(self.path.read_bytes())
                self.assertNotEqual(_application_rows(main_only), before)

                with _tracked_backups() as copies:
                    BotStorage(self.path)

                self.assertEqual(len(copies), 1)
                backup = copies[0][1]
                self.assertTrue(backup.name.endswith("_before_schema_check.sqlite"))
                self.assertEqual(_application_rows(backup), before)
                self.assertEqual(_schema_version(backup), 0)
                self.assertEqual(_schema_version(self.path), SCHEMA_VERSION)
                after = _application_rows(self.path)
                self.assertIn("primary_exchange", after["cycles"][0])
                self.assertEqual(after["orders"], before["orders"])
                self.assertEqual(after["executions"], before["executions"])
                self.assertEqual(after["events"], before["events"])

    def test_failed_migration_keeps_old_stamp_and_original_recovery_copy(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
            con.execute("PRAGMA user_version = 0")
        before = _application_rows(self.path)
        add_column = BotStorage._add_column_if_missing

        def fail_last_column(con, table, column, definition):
            if column == "completed_at":
                raise sqlite3.OperationalError("injected final migration failure")
            return add_column(con, table, column, definition)

        with _tracked_backups() as copies, patch.object(
            BotStorage, "_add_column_if_missing", side_effect=fail_last_column,
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected final migration failure"):
                BotStorage(self.path)

        self.assertEqual(len(copies), 1)
        self.assertEqual(_application_rows(copies[0][1]), before)
        self.assertEqual(_schema_version(copies[0][1]), 0)
        self.assertEqual(_schema_version(self.path), 0)
        # A retry still recognizes the incomplete migration and takes a backup.
        with _tracked_backups() as retry_copies:
            BotStorage(self.path)
        self.assertEqual(len(retry_copies), 1)
        self.assertEqual(_schema_version(self.path), SCHEMA_VERSION)

    def test_future_schema_rejected_before_migrations_writes_or_backup_changes(self):
        self._seed_excess_backups()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            con.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
            con.execute("CREATE TABLE future_state (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO future_state VALUES (1, 'keep exactly')")
        before = _application_rows(self.path)
        bytes_before = self.path.read_bytes()
        backups_before = self._backup_files()

        for backup_before_schema in (True, False):
            with self.subTest(backup=backup_before_schema), _tracked_backups() as copies, patch.object(
                BotStorage, "_add_column_if_missing",
            ) as migration:
                with self.assertRaisesRegex(DatabaseSchemaError, "newer than.*supported"):
                    BotStorage(self.path, _backup_before_schema=backup_before_schema)
            migration.assert_not_called()
            self.assertEqual(copies, [])
            self.assertEqual(self.path.read_bytes(), bytes_before)
            self.assertEqual(_application_rows(self.path), before)
            self.assertEqual(self._backup_files(), backups_before)

    def test_future_schema_restore_candidate_is_rejected_without_modifying_source(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        before = self.path.read_bytes()

        result = self.storage.validate_restore_candidate(self.path)

        self.assertFalse(result["ok"])
        self.assertFalse(result["restore_copy_validated"])
        self.assertIn("newer than", result["error"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unreadable_schema_fails_closed_and_clean_retry_respects_version(self):
        self._seed_excess_backups()
        connect = sqlite3.connect
        for version in (0, SCHEMA_VERSION, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                with closing(connect(self.path)) as con, con:
                    con.execute(f"PRAGMA user_version = {version}")
                before = _application_rows(self.path)
                bytes_before = self.path.read_bytes()
                backups_before = self._backup_files()
                connections = []

                def fail_first_connection(*args, **kwargs):
                    connections.append((args, kwargs))
                    if len(connections) == 1:
                        raise sqlite3.OperationalError("injected transient schema read failure")
                    return connect(*args, **kwargs)

                with _tracked_backups() as copies, patch(
                    "app.storage.sqlite3.connect", side_effect=fail_first_connection,
                ), patch.object(BotStorage, "_add_column_if_missing") as migration:
                    with self.assertRaisesRegex(DatabaseSchemaError, "Could not read.*schema version"):
                        BotStorage(self.path)

                self.assertEqual(len(connections), 1)
                self.assertTrue(connections[0][0][0].endswith("?mode=ro"))
                migration.assert_not_called()
                self.assertEqual(copies, [])
                self.assertEqual(self.path.read_bytes(), bytes_before)
                self.assertEqual(_application_rows(self.path), before)
                self.assertEqual(self._backup_files(), backups_before)
                self.assertEqual(_schema_version(self.path), version)

                with _tracked_backups() as retry_copies:
                    if version > SCHEMA_VERSION:
                        with self.assertRaisesRegex(DatabaseSchemaError, "newer than"):
                            BotStorage(self.path)
                    else:
                        BotStorage(self.path)
                self.assertEqual(len(retry_copies), 1 if version == 0 else 0)
                self.assertEqual(_schema_version(self.path), max(version, SCHEMA_VERSION))
                self.assertEqual(_application_rows(self.path), before)
                if version > SCHEMA_VERSION:
                    self.assertEqual(self.path.read_bytes(), bytes_before)
                    self.assertEqual(self._backup_files(), backups_before)

    def test_default_rotation_keeps_twenty_fully_validated_backups(self):
        self.assertEqual(BACKUP_KEEP_DEFAULT, 20)
        before = _application_rows(self.path)
        backups = []
        with _tracked_backups() as copies:
            for number in range(23):
                backup = self.storage.backup_database(f"default_rotation_{number}")
                self.assertIsNotNone(backup)
                backups.append(backup)
                os.utime(backup, (1_700_000_000 + number, 1_700_000_000 + number))

        self.assertEqual(len(copies), 46)
        self.assertEqual(set(self.storage.list_database_backups()), set(backups[-20:]))
        self.assertEqual(_application_rows(self.path), before)
        for backup in backups[-20:]:
            self.assertEqual(_application_rows(backup), before)
        self.assertTrue(all(not destination.parent.exists() for _, destination in copies if destination.name == "restore_candidate.sqlite"))

    def test_copy_failure_never_prunes_excess_existing_recovery_points(self):
        self._seed_excess_backups()
        before = self._backup_files()

        for copy_number in (1, 2):
            with self.subTest(copy=copy_number), _tracked_backups(fail_on=copy_number):
                result = self.storage.backup_database("injected_failure")
            self.assertIsNone(result)
            self.assertEqual(self._backup_files(), before)

    def test_invalid_backup_never_prunes_excess_existing_recovery_points(self):
        self._seed_excess_backups()
        before = self._backup_files()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(
                "INSERT INTO orders(cycle_id,ticker,action,order_type,quantity,created_at,updated_at) "
                "VALUES ('missing','TEST','BUY','MKT',1,'now','now')"
            )

        with _tracked_backups() as copies:
            result = self.storage.backup_database("invalid_foreign_key")

        self.assertIsNone(result)
        self.assertEqual(len(copies), 1)
        self.assertEqual(self._backup_files(), before)


if __name__ == "__main__":
    unittest.main()
