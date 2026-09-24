"""Exact historical-cycle resolution must preserve live ownership and recorded fills."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.models import CycleState, Stage, StrategySettings
from app.storage import BotStorage


class HistoricalRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._historical_recovery_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = BotStorage(Path(self.folder.name) / "historical.sqlite")
        self.controller = self.controller_type(self.storage)
        self.controller.adapter = Mock()
        self.controller.emit_snapshot = Mock()
        self.controller.strategy = StrategySettings(ticker="AAA", contract_con_id=111)
        self.active = CycleState.new(self.controller.strategy, 9, "ACCOUNT_TEST", 100, 0)
        self.active.stage = Stage.WAIT_RISE_TRIGGER
        self.active.buy_filled_qty = 6
        self.active.buy_status = "Filled"
        self.storage.upsert_cycle(self.active)
        self.controller.active_cycle = self.active
        self.controller._startup_resume_required = True
        self.controller._recovery_required = True
        self.controller._stale_active_cycle_detected = True
        self.historical = CycleState.new(self.controller.strategy, 3, "ACCOUNT_TEST", 100, 0)
        self.historical.stage = Stage.CYCLE_COMPLETE
        self.historical.quantity = 10
        self.historical.buy_filled_qty = 5
        self.historical.sell_filled_qty = 5
        self.historical.buy_status = "CancelRequested"
        self.historical.buy_order_ref = "IBKRBOT|AAA|CYCLE-000003|history|BUY_TRAIL"
        self.storage.upsert_cycle(self.historical)
        self.storage.add_order(
            cycle=self.historical, action="BUY", order_type="TRAIL", order_id=103,
            perm_id=203, order_ref=self.historical.buy_order_ref, quantity=10,
            trailing_percent=1, initial_stop_price=100, status="CancelRequested",
        )
        self.storage.add_execution(
            cycle=self.historical, ticker="AAA", side="BUY", shares=5, price=100,
            execution_id="historical-fill", order_ref=self.historical.buy_order_ref,
        )

    def tearDown(self):
        self.controller._market_capture.shutdown()
        self.folder.cleanup()

    def recorded_rows(self):
        with self.storage.connect() as con:
            return {
                table: [tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("cycles", "orders", "executions", "app_settings")
            }

    def decisions(self):
        with self.storage.connect() as con:
            return [dict(row) for row in con.execute(
                "SELECT * FROM decision_events WHERE event_type='MANUALLY_HANDLED' ORDER BY id"
            )]

    def resolve(self, cycle_id=None, note="Broker orders and residual shares independently verified and handled."):
        self.controller._handle_command("MARK_HISTORICAL_CYCLE_MANUALLY_HANDLED", {
            "cycle_id": self.historical.id if cycle_id is None else cycle_id,
            "note": note,
            "expected_cycle": self.historical.to_dict(),
        })

    def test_public_request_only_queues_exact_target_and_note(self):
        expected = self.historical.to_dict()
        self.controller.mark_historical_cycle_manually_handled(
            self.historical.id, "Verified outside app", expected_cycle=expected,
        )
        expected["buy_filled_qty"] = 999
        self.assertEqual(self.controller._commands.get_nowait(), (
            "MARK_HISTORICAL_CYCLE_MANUALLY_HANDLED",
            {"cycle_id": self.historical.id, "note": "Verified outside app", "expected_cycle": self.historical.to_dict()},
        ))
        self.assertEqual(self.decisions(), [])

    def test_queued_resolution_rejects_changed_fills_or_order_identity(self):
        original = self.historical.to_dict()
        for field, value in (
            ("buy_filled_qty", 10),
            ("buy_order_ref", "IBKRBOT|AAA|CYCLE-000003|changed|BUY_TRAIL"),
            ("account", "OTHER_ACCOUNT"),
            ("updated_at", "2099-01-01T00:00:00+00:00"),
        ):
            with self.subTest(field=field):
                self.historical = CycleState.from_dict(original)
                self.storage.upsert_cycle(self.historical)
                self.controller.mark_historical_cycle_manually_handled(
                    self.historical.id, "Verified outside app", expected_cycle=original,
                )
                setattr(self.historical, field, value)
                self.storage.upsert_cycle(self.historical)
                name, payload = self.controller._commands.get_nowait()
                self.controller._handle_command(name, payload)
                self.assertEqual(self.decisions(), [])
                self.assertIn("selected cycle changed", self.controller.status)
        self.assertEqual(self.controller.adapter.mock_calls, [])

    def test_missing_reviewed_snapshot_is_rejected(self):
        self.controller._handle_command("MARK_HISTORICAL_CYCLE_MANUALLY_HANDLED", {
            "cycle_id": self.historical.id, "note": "Verified outside app",
        })
        self.assertEqual(self.decisions(), [])
        self.assertIn("reviewed state is missing", self.controller.status)

    def test_resolution_records_exact_decision_without_rewriting_history_or_resuming(self):
        before = self.recorded_rows()
        active_before = self.active.to_dict()
        with patch.object(self.controller, "_recover_after_connect") as recover:
            self.resolve()
        self.assertEqual(self.recorded_rows(), before)
        self.assertIs(self.controller.active_cycle, self.active)
        self.assertEqual(self.active.to_dict(), active_before)
        self.assertTrue(self.controller._startup_resume_required)
        self.assertTrue(self.controller._recovery_required)
        self.assertTrue(self.controller._stale_active_cycle_detected)
        self.assertEqual(self.controller.adapter.mock_calls, [])
        recover.assert_not_called()
        decisions = self.decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["cycle_id"], self.historical.id)
        self.assertEqual(decisions[0]["stage_before"], Stage.CYCLE_COMPLETE.value)
        self.assertEqual(decisions[0]["stage_after"], Stage.CYCLE_COMPLETE.value)
        self.assertEqual(json.loads(decisions[0]["raw_json"])["historical_cycle"], self.historical.to_dict())
        with self.storage.connect() as con:
            warning = con.execute("SELECT * FROM events WHERE cycle_id=?", (self.historical.id,)).fetchone()
        self.assertIsNotNone(warning)
        self.assertEqual(warning["level"], "WARN")
        self.assertIn("marked manually handled by operator", warning["message"])
        self.assertEqual([c.id for c in self.storage.get_unresolved_cycles()], [self.active.id])
        self.assertEqual(self.controller._snapshot_database_cache["historical_unresolved_cycles"], [])
        self.assertIn("click Start explicitly", self.controller.status)

    def test_stopped_historical_position_transfers_responsibility_only_on_explicit_resolution(self):
        self.historical.stage = Stage.STOPPED
        self.historical.sell_filled_qty = 0
        self.storage.upsert_cycle(self.historical)
        before = self.recorded_rows()
        quantity_before = self.storage.get_app_owned_unsold_position("AAA", con_id=111)["quantity"]
        self.resolve()
        self.assertEqual(self.recorded_rows(), before)
        self.assertEqual(
            self.storage.get_app_owned_unsold_position("AAA", con_id=111)["quantity"],
            quantity_before - self.historical.buy_filled_qty,
        )

    def test_repeat_resolution_cannot_append_a_second_handled_decision(self):
        self.resolve()
        self.resolve()
        self.assertEqual(len(self.decisions()), 1)
        self.assertIn("Historical recovery blocked", self.controller.status)

    def test_unknown_or_blank_id_is_rejected_without_changes(self):
        before = self.recorded_rows()
        for cycle_id in ("", "unknown-cycle"):
            with self.subTest(cycle_id=cycle_id):
                self.resolve(cycle_id)
                self.assertEqual(self.decisions(), [])
                self.assertEqual(self.recorded_rows(), before)
        self.assertEqual(self.controller.adapter.mock_calls, [])

    def test_empty_note_is_rejected(self):
        for note in ("", " \n "):
            with self.subTest(note=note):
                self.resolve(note=note)
                self.assertEqual(self.decisions(), [])

    def test_active_cycle_is_rejected(self):
        self.resolve(self.active.id)
        self.assertEqual(self.decisions(), [])
        self.assertIs(self.controller.active_cycle, self.active)

    def test_current_terminal_cycle_is_rejected(self):
        self.controller.active_cycle = self.historical
        self.resolve()
        self.assertEqual(self.decisions(), [])

    def test_latest_active_database_state_is_rechecked_after_dialog(self):
        self.historical.stage = Stage.WAIT_RISE_TRIGGER
        self.historical.updated_at = "2099-01-01T00:00:00+00:00"
        self.storage.upsert_cycle(self.historical)
        self.resolve()
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.storage.get_latest_active_cycle().id, self.historical.id)

    def test_terminal_target_that_is_no_longer_unresolved_is_rejected(self):
        self.historical.buy_status = "Filled"
        self.storage.upsert_cycle(self.historical)
        self.resolve()
        self.assertEqual(self.decisions(), [])

    def test_storage_fault_blocks_historical_resolution(self):
        self.controller._storage_fault_active = True
        self.resolve()
        self.assertEqual(self.decisions(), [])
        self.assertIn("SQLite storage is unavailable", self.controller.status)

    def test_failed_decision_write_cannot_remove_the_unresolved_target(self):
        with patch.object(self.storage, "add_decision_event", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.resolve()
        self.assertEqual(self.decisions(), [])
        self.assertIn(self.historical.id, [c.id for c in self.storage.get_unresolved_cycles()])
        self.assertEqual(self.controller.adapter.mock_calls, [])

    def test_snapshot_uses_database_cadence_and_retains_read_failure_diagnostics(self):
        with patch.object(self.storage, "get_unresolved_cycles", wraps=self.storage.get_unresolved_cycles) as read:
            cache = self.controller._refresh_snapshot_database_cache(force=True)
            self.assertEqual([row["id"] for row in cache["historical_unresolved_cycles"]], [self.historical.id])
            self.controller._refresh_snapshot_database_cache()
            self.assertEqual(read.call_count, 1)
        with patch.object(self.storage, "get_unresolved_cycles", side_effect=sqlite3.OperationalError("test read failure")):
            failed = self.controller._refresh_snapshot_database_cache(force=True)
        self.assertEqual(failed["historical_unresolved_cycles"], cache["historical_unresolved_cycles"])
        self.assertEqual(failed["errors"]["historical_unresolved_cycles"], "test read failure")
        self.controller_type.emit_snapshot(self.controller, force=True, refresh_database=False)
        self.assertEqual(self.controller._last_snapshot_payload["historical_unresolved_cycles"], cache["historical_unresolved_cycles"])

    def test_snapshot_excludes_the_current_terminal_cycle(self):
        self.controller.active_cycle = self.historical
        cache = self.controller._refresh_snapshot_database_cache(force=True)
        self.assertEqual(cache["historical_unresolved_cycles"], [])


if __name__ == "__main__":
    unittest.main()
