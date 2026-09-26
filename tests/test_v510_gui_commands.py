"""Broker-session routing and exact-target operator confirmations; no sockets."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models import ConnectionSettings, CycleState, Stage
from app.storage import BotStorage
from tests.support.controller_harness import permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub


class SessionBroker(DeterministicBrokerAdapter):
    requires_exact_contract_selection = True

    def __init__(self):
        super().__init__()
        self.local_connected = False
        self.endpoint = None
        self.connections = []
        self.disconnect_count = 0
        self.accounts = ["LIVE_TEST"]

    def connect(self, host, port, client_id, market_data_type=1):
        # Model ib_async: connect does not replace an existing socket.
        if self.local_connected:
            return
        super().connect(host, port, client_id, market_data_type)
        self.endpoint = (host, port, client_id)
        self.connections.append(self.endpoint)
        self.accounts = ["PAPER_TEST" if port == 4002 else "LIVE_TEST"]

    def disconnect(self):
        self.disconnect_count += 1
        super().disconnect()


class SessionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._v510_gui_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.module = module

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        directory = Path(self.folder.name)
        self.storage = BotStorage(directory / "state.sqlite")
        self.live = ConnectionSettings(account="", market_data_type=1, port=4001, trading_mode="live")
        self.paper = replace(self.live, port=4002, trading_mode="paper")
        self.settings = replace(
            permissive_strategy(), contract_con_id=123, primary_exchange="NASDAQ", buy_rebound_trail_pct=0,
        )
        self.storage.save_connection_settings(self.live)
        self.storage.save_strategy_settings(self.settings)
        with patch.object(self.module, "debug_captures_dir", return_value=directory / "captures"):
            self.controller = self.module.TradingController(self.storage)
        self.addCleanup(self.controller._market_capture.shutdown)
        self.controller.emit_snapshot = Mock()
        self.broker = SessionBroker()
        self.controller.adapter = self.broker
        self.controller._handle_command("CONNECT", {"settings": self.live})

    def command(self, name, connection=None):
        self.controller._handle_command(name, {
            "connection": connection or self.paper, "strategy": self.settings, "query": "AAPL",
        })

    def test_paper_draft_cannot_start_through_live_session(self):
        self.command("SAVE_DRAFT_SETTINGS")
        self.broker.publish_price(100)
        self.command("START_STRATEGY")
        self.assertIsNone(self.controller.active_cycle)
        self.assertEqual(self.controller.connection.trading_mode, "paper")
        self.assertEqual(self.broker.endpoint[1], 4001)
        self.assertEqual(len(self.broker.connections), 1)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertIn("established broker session", self.controller.status)

    def test_changed_identity_is_rejected_before_autosave(self):
        for change in (
            {"port": 4002}, {"host": "other-host"}, {"client_id": 99},
            {"trading_mode": "paper"}, {"platform": "tws"},
        ):
            with self.subTest(change=change):
                self.command("START_STRATEGY", replace(self.live, **change))
                self.assertIsNone(self.controller.active_cycle)
                self.assertEqual(self.controller.connection, self.live)
        self.assertEqual(self.broker.placed_orders, [])

    def test_confirm_and_search_cannot_use_wrong_session(self):
        for command in ("CONFIRM_TICKER_PRICE", "SEARCH_CONTRACTS"):
            with self.subTest(command=command):
                self.command(command)
                self.assertIn("established broker session", self.controller.status)
                self.assertEqual(self.controller.connection, self.live)

    def test_unchanged_session_still_starts_and_places_buy(self):
        self.broker.publish_price(100)
        self.command("START_STRATEGY", self.live)
        self.broker.publish_price(97)
        self.controller._run_broker_cycle()
        self.controller._run_strategy_cycle()
        self.assertEqual(len(self.broker.placed_orders), 1)
        self.assertEqual(self.broker.placed_orders[0]["account"], "LIVE_TEST")
        self.assertEqual(len(self.broker.connections), 1)

    def test_explicit_connect_switches_socket_before_paper_start(self):
        self.command("SAVE_DRAFT_SETTINGS")
        self.controller._handle_command("CONNECT", {"settings": self.paper})
        self.assertEqual(self.broker.endpoint[1], 4002)
        self.assertEqual(self.broker.disconnect_count, 1)
        self.assertEqual(len(self.broker.connections), 2)
        self.broker.publish_price(100)
        self.command("START_STRATEGY")
        self.broker.publish_price(97)
        self.controller._run_broker_cycle()
        self.controller._run_strategy_cycle()
        self.assertEqual(self.broker.placed_orders[0]["account"], "PAPER_TEST")

    def test_account_or_market_data_draft_does_not_require_new_socket(self):
        connection = replace(self.live, account="LIVE_TEST", market_data_type=3)
        self.command("SAVE_DRAFT_SETTINGS", connection)
        self.assertIsNone(self.controller._connection_session_message())
        self.assertEqual(len(self.broker.connections), 1)

    def test_order_preflight_rejects_mismatch_even_if_start_was_bypassed(self):
        self.controller.connection = self.paper
        for side in ("BUY", "SELL"):
            with self.subTest(side=side):
                self.assertIn("established broker session", self.controller._order_submission_connectivity_message(side))

    def test_internal_start_cannot_bypass_established_session_check(self):
        self.controller.connection = self.paper
        self.broker.publish_price(100)
        self.controller._start_strategy(self.settings)
        self.assertIsNone(self.controller.active_cycle)
        self.assertEqual(self.broker.placed_orders, [])

    def test_auto_repeat_stops_before_creating_cycle_for_changed_profile(self):
        cycle = CycleState.new(self.settings, 1, "LIVE_TEST", 100, 0)
        cycle.stage = Stage.CYCLE_COMPLETE
        self.storage.upsert_cycle(cycle)
        self.controller.active_cycle = cycle
        self.controller.strategy = replace(self.settings, auto_repeat=True)
        self.controller.connection = self.paper
        self.controller._maybe_start_next_cycle()
        self.assertIs(self.controller.active_cycle, cycle)
        self.assertTrue(cycle.stop_after_current_cycle)
        self.assertEqual(self.broker.placed_orders, [])
        self.assertIn("established broker session", self.controller.status)

    def test_reconnect_with_live_adapter_socket_cannot_claim_new_identity(self):
        self.controller.connection = self.paper
        self.controller.connected = False
        self.assertTrue(self.controller._attempt_reconnect_if_due())
        self.assertEqual(self.broker.endpoint[1], 4002)
        self.assertEqual(self.broker.disconnect_count, 1)
        self.assertEqual(self.controller._established_connection_identity["port"], 4002)

    def active_cycle(self):
        cycle = CycleState.new(self.settings, 1, "LIVE_TEST", 100, 0)
        cycle.stage = Stage.SELL_TRAIL_ACTIVE
        cycle.buy_filled_qty = 10
        cycle.sell_order_ref = "synthetic-sell-1"
        cycle.sell_status = "Submitted"
        self.storage.upsert_cycle(cycle)
        self.controller.active_cycle = cycle
        return cycle

    def test_manual_acknowledgment_unchanged_target_still_works(self):
        cycle = self.active_cycle()
        self.controller.mark_recovery_manually_handled("Independently verified", expected_cycle=cycle.to_dict())
        self.controller._drain_commands()
        self.assertIsNone(self.controller.active_cycle)
        self.assertEqual(self.storage.get_cycle(cycle.id).stage, Stage.STOPPED)
        self.assertEqual(self.broker.cancelled_orders, [])
        self.assertEqual(self.broker.placed_orders, [])

    def test_manual_acknowledgment_rechecks_queued_target_and_state(self):
        for field, value in (
            ("id", "another-cycle"), ("buy_filled_qty", 20),
            ("sell_order_ref", "new-sell"), ("sell_status", "Filled"),
        ):
            with self.subTest(field=field):
                cycle = self.active_cycle()
                self.controller.mark_recovery_manually_handled("Verified", expected_cycle=cycle.to_dict())
                setattr(cycle, field, value)
                before = cycle.to_dict()
                self.controller._drain_commands()
                self.assertEqual(self.controller.active_cycle.to_dict(), before)
                self.assertIn("Manual handling blocked", self.controller.status)
        self.assertEqual(self.broker.cancelled_orders, [])

    def test_manual_acknowledgment_rejects_missing_reviewed_state(self):
        cycle = self.active_cycle()
        self.controller.mark_recovery_manually_handled("Verified")
        self.controller._drain_commands()
        self.assertIs(self.controller.active_cycle, cycle)
        self.assertIn("Manual handling blocked", self.controller.status)

    def test_manual_acknowledgment_payload_is_not_mutable_by_caller(self):
        cycle = self.active_cycle()
        expected = cycle.to_dict()
        self.controller.mark_recovery_manually_handled("Verified", expected_cycle=expected)
        expected["buy_filled_qty"] = 999
        self.controller._drain_commands()
        self.assertEqual(self.storage.get_cycle(cycle.id).stage, Stage.STOPPED)


class GuiCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.controller = _ControllerStub()
        self.controller.start_strategy = Mock()
        self.controller.mark_recovery_manually_handled = Mock()
        self.window = self.gui.MainWindow(self.controller)
        self.live = ConnectionSettings(port=4001, trading_mode="live")
        self.settings = replace(permissive_strategy(), contract_con_id=123, primary_exchange="NASDAQ")
        self.window._apply_snapshot_to_inputs(asdict(self.live), asdict(self.settings))
        self.window.current_snapshot = {
            "connected": True, "connection": asdict(self.live),
            "established_connection": {key: getattr(self.live, key) for key in ("host", "port", "client_id", "trading_mode", "platform")},
            "broker_connectivity": {"local_connected": True, "upstream_connected": True},
            "price_snapshot": {"price": 100},
        }
        self.messages = SimpleNamespace(Yes=1, No=2, question=Mock(return_value=1), warning=Mock(), information=Mock())
        self.message_patch = patch.object(self.gui, "QMessageBox", self.messages)
        self.message_patch.start()
        self.addCleanup(self.message_patch.stop)

    def select_paper(self):
        for index in range(self.window.profile_combo.count()):
            profile = self.window.profile_combo.itemData(index) or {}
            if profile.get("trading_mode") == "paper" and profile.get("platform") == "gateway":
                self.window.profile_combo.setCurrentIndex(index)
                self.window._on_profile_changed(index)
                return
        self.fail("Paper profile not found")

    def test_paper_edit_requires_explicit_connect_and_cannot_skip_live_confirmation(self):
        self.select_paper()
        self.window._update_command_bar_states(self.window.current_snapshot)
        self.assertTrue(self.window.command_step_buttons["connect"].isEnabled())
        self.assertFalse(self.window.start_btn.isEnabled())
        self.window._start_clicked()
        self.controller.start_strategy.assert_not_called()
        self.messages.warning.assert_called_once()
        self.messages.question.assert_not_called()

    def test_status_profile_keeps_established_live_identity(self):
        self.window.current_snapshot["connection"]["trading_mode"] = "paper"
        self.window.live_status_bar.update_data(self.window.current_snapshot)
        self.assertIn("LIVE", self.window.live_status_bar.pills["Profile"].value.text())

    def test_unchanged_live_session_keeps_confirmation(self):
        self.window._start_clicked()
        self.messages.question.assert_called_once()
        self.controller.start_strategy.assert_called_once()

    def test_manual_dialog_rejects_changed_cycle_order_and_fill(self):
        for field, value in (("id", "next"), ("buy_filled_qty", 20), ("sell_order_ref", "new")):
            with self.subTest(field=field):
                cycle = {"id": "reviewed", "ticker": "AAPL", "stage": Stage.SELL_TRAIL_ACTIVE.value, "buy_filled_qty": 10, "sell_order_ref": "old"}
                self.window.current_snapshot["active_cycle"] = cycle

                def confirm(*_args):
                    cycle[field] = value
                    return self.messages.Yes

                self.messages.question.side_effect = confirm
                self.window._recovery_mark_manual_clicked()
                self.controller.mark_recovery_manually_handled.assert_not_called()
        self.assertEqual(self.messages.warning.call_count, 3)

    def test_manual_dialog_allows_price_only_update_and_passes_reviewed_copy(self):
        cycle = {"id": "reviewed", "ticker": "AAPL", "stage": Stage.SELL_TRAIL_ACTIVE.value, "buy_filled_qty": 10, "last_price": 100}
        expected = deepcopy(cycle)
        self.window.current_snapshot["active_cycle"] = cycle

        def confirm(*_args):
            cycle["last_price"] = 101
            return self.messages.Yes

        self.messages.question.side_effect = confirm
        self.window._recovery_mark_manual_clicked()
        call = self.controller.mark_recovery_manually_handled.call_args
        self.assertEqual(call.kwargs["expected_cycle"], expected)
        self.assertIsNot(call.kwargs["expected_cycle"], cycle)


if __name__ == "__main__":
    unittest.main()
