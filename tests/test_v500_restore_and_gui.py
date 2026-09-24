"""Restore rejection and operator-status regressions; no broker or native Qt."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models import Stage
from app.storage import BotStorage
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs


class RestoreValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = BotStorage(self.root / "active.sqlite")

    def test_valid_database_restore_copy_passes_without_changing_source(self):
        backup = self.storage.backup_database("test")
        original = backup.read_bytes()
        result = self.storage.validate_restore_candidate(backup)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["restore_copy_validated"])
        self.assertEqual(backup.read_bytes(), original)

    def test_expected_table_names_with_dummy_columns_are_rejected(self):
        candidate = self.root / "dummy.sqlite"
        with closing(sqlite3.connect(candidate)) as con, con:
            for table in ("app_settings", "cycles", "orders", "executions", "events", "decision_events"):
                con.execute(f'CREATE TABLE "{table}" (dummy TEXT)')
        original = candidate.read_bytes()
        result = self.storage.validate_restore_candidate(candidate)
        self.assertFalse(result["ok"])
        self.assertIn("cycle_number", result["missing_columns"]["cycles"])
        self.assertTrue(result["schema_errors"])
        self.assertEqual(candidate.read_bytes(), original)

    def test_missing_core_column_rejected_even_when_table_and_primary_key_exist(self):
        candidate = self.storage.backup_database("missing_column")
        with closing(sqlite3.connect(candidate)) as con, con:
            con.execute("ALTER TABLE orders DROP COLUMN quantity")
        result = self.storage.validate_backup(candidate)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_columns"], {"orders": ["quantity"]})

    def test_missing_additive_columns_are_migrated_only_in_restore_copy(self):
        candidate = self.storage.backup_database("legacy")
        with closing(sqlite3.connect(candidate)) as con, con:
            con.execute("ALTER TABLE cycles DROP COLUMN protective_sell_enabled")
            con.execute("ALTER TABLE cycles DROP COLUMN primary_exchange")
        original = candidate.read_bytes()
        result = self.storage.validate_restore_candidate(candidate)
        self.assertTrue(result["ok"], result)
        self.assertEqual(candidate.read_bytes(), original)
        with closing(sqlite3.connect(candidate)) as con, con:
            self.assertNotIn("protective_sell_enabled", {row[1] for row in con.execute("PRAGMA table_info(cycles)")})

    def test_orphan_foreign_key_is_rejected(self):
        candidate = self.storage.backup_database("orphan")
        with closing(sqlite3.connect(candidate)) as con, con:
            con.execute(
                "INSERT INTO orders(cycle_id,ticker,action,order_type,quantity,created_at,updated_at) "
                "VALUES ('missing','AAPL','SELL','MKT',10,'now','now')"
            )
        result = self.storage.validate_restore_candidate(candidate)
        self.assertFalse(result["ok"])
        self.assertTrue(result["foreign_key_errors"])

    def test_filename_uri_characters_are_handled_literally(self):
        candidate = self.root / "backup#with space.sqlite"
        source = BotStorage(candidate)
        self.assertTrue(source.validate_restore_candidate(candidate)["ok"])


class GuiSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def protection_text(self, status, *, connected=True, cancel=False, ref="owned", shares=10):
        bar = self.gui.LiveStatusBar()
        bar.update_data({
            "connected": connected,
            "broker_connectivity": {"local_connected": connected, "upstream_connected": connected},
            "active_cycle": {
                "protective_sell_enabled": True,
                "protective_sell_order_ref": ref,
                "protective_sell_status": status,
                "protective_sell_cancel_requested": cancel,
                "buy_filled_qty": shares,
            },
        })
        return bar.pills["Protection"].value.text()

    def test_only_confirmed_working_protection_is_shown_on(self):
        for status in ("Submitted", "PreSubmitted"):
            with self.subTest(status=status):
                self.assertEqual(self.protection_text(status), "On")
        for status in ("Cancelled", "ApiCancelled", "Inactive", "Rejected", "Filled"):
            with self.subTest(status=status):
                self.assertEqual(self.protection_text(status), "Missing")
        for status in (None, "PendingSubmit", "SUBMISSION_UNKNOWN"):
            with self.subTest(status=status):
                self.assertEqual(self.protection_text(status), "Unconfirmed")
        self.assertEqual(self.protection_text("Submitted", connected=False), "Unconfirmed")
        self.assertEqual(self.protection_text("Submitted", cancel=True), "Cancelling")
        self.assertEqual(self.protection_text(None, ref=""), "Missing")
        self.assertEqual(self.protection_text(None, ref="", shares=0), "Armed")

    def test_stage_ribbon_reduces_padding_and_height_without_smaller_text(self):
        class LabelProbe(Dummy):
            def setMinimumHeight(self, value):
                self.minimum_height = value

            def setStyleSheet(self, value):
                self.applied_style = value

        with patch.object(self.gui, "QLabel", LabelProbe):
            ribbon = self.gui.StageRibbon()
            ribbon.set_stage(Stage.WAIT_RISE_TRIGGER.value)
            self.assertEqual(len(ribbon.cards), 5)
            for card in ribbon.cards.values():
                self.assertEqual(card.minimum_height, 60)
                self.assertIn("font-size: 14px", card.applied_style)
                self.assertIn("padding: 4px 10px", card.applied_style)
                self.assertIn("\n", card.text())

    def test_connect_handler_respects_manual_and_workflow_locks(self):
        for locked, enabled, expected in ((True, True, 0), (False, False, 0), (False, True, 1)):
            with self.subTest(locked=locked, enabled=enabled):
                controller = SimpleNamespace(connect_tws=Mock())
                window = SimpleNamespace(
                    _manual_input_lock_enabled=locked,
                    current_snapshot={},
                    command_step_buttons={"connect": SimpleNamespace(isEnabled=lambda: enabled)},
                    _update_command_bar_states=Mock(),
                    _connection_from_ui=lambda: "settings",
                    controller=controller,
                )
                self.gui.MainWindow._connect_clicked(window)
                self.assertEqual(controller.connect_tws.call_count, expected)


if __name__ == "__main__":
    unittest.main()
