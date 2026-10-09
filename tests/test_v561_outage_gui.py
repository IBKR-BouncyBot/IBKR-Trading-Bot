"""An unavailable broker link is a wait; durable recovery faults remain errors."""

from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.models import Stage
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs


class OutageGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def snapshot(self, stage=Stage.WAIT_RISE_TRIGGER, outage="upstream"):
        local = outage != "local"
        upstream = None if outage == "unconfirmed" else outage != "upstream"
        code = {
            "local": "disconnected", "upstream": "upstream_disconnected",
            "unconfirmed": "upstream_disconnected", "reconcile": "upstream_recovery",
        }.get(outage)
        return {
            "connected": local,
            "auto_reconnect_enabled": True,
            "status": "Broker connection temporarily unavailable" if outage else "Connected",
            "broker_connectivity": {
                "local_connected": local, "upstream_connected": upstream,
                "awaiting_fresh_market_data": bool(outage),
            },
            "upstream_recovery_pending": outage == "reconcile",
            "active_cycle": {
                "id": "outage-cycle", "ticker": "TEST", "stage": stage.value,
                "last_price": 100.0, "error_message": "",
            },
            "price_snapshot": {
                "price": 100.0, "api_data_age_seconds": 0.2,
                "api_data_state": "invalidated" if outage else "recent",
                "api_data_invalidated": bool(outage),
                "market_data_event_tracking": True,
                "market_data_event_tracking_available": True,
                "subscription_market_data_type": 1,
            },
            "trading_status": {
                "summary": "SELL blocked: reconnecting" if outage else "Running",
                "state": "waiting" if outage else "active",
                "tooltip": "Fresh broker evidence is required before the next action.",
                "blockers": [{"code": code, "side": "SELL"}] if code else [],
            },
        }

    def command_cards(self, snapshot, *, locked=False, mismatch=False):
        before = deepcopy(snapshot)
        keys = ("connect", "ticker", "confirm", "start", "stop")
        window = SimpleNamespace(
            current_snapshot=snapshot,
            command_steps={key: self.gui.CommandStepCard(key, Dummy()) for key in keys},
            _manual_input_lock_enabled=locked,
            _connection_session_mismatch=lambda _snapshot: mismatch,
        )
        self.gui.MainWindow._update_command_bar_states(window, snapshot)
        self.assertEqual(snapshot, before)
        return window.command_steps

    def status_bar(self, snapshot):
        before = deepcopy(snapshot)
        bar = self.gui.LiveStatusBar()
        bar.update_data(snapshot)
        self.assertEqual(snapshot, before)
        return bar

    def test_each_active_stage_waits_without_enabling_strategy_actions(self):
        stages = (Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE, Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE)
        for stage in stages:
            for outage in ("local", "upstream", "unconfirmed", "reconcile"):
                with self.subTest(stage=stage, outage=outage):
                    cards = self.command_cards(self.snapshot(stage, outage))
                    self.assertEqual(cards["connect"].state.text(), "WAITING")
                    self.assertEqual(cards["start"].state.text(), "WAITING")
                    for key in ("ticker", "confirm", "start"):
                        self.assertFalse(cards[key].button.isEnabled())
                    self.assertEqual(cards["connect"].button.isEnabled(), outage == "local")

    def test_local_disconnect_during_snapshot_transition_is_waiting(self):
        snapshot = self.snapshot(outage="local")
        snapshot["connected"] = True
        snapshot["status"] = "Connection error; reconnecting"
        cards = self.command_cards(snapshot)
        self.assertEqual(cards["connect"].state.text(), "WAITING")
        self.assertTrue(cards["connect"].button.isEnabled())
        self.assertEqual(cards["start"].state.text(), "WAITING")

    def test_operator_disconnect_does_not_claim_automatic_reconnection(self):
        for structured in (True, False):
            with self.subTest(structured=structured):
                snapshot = self.snapshot(outage="local")
                snapshot["auto_reconnect_enabled"] = False
                snapshot["status"] = "Disconnected"
                if not structured:
                    snapshot.pop("trading_status")
                cards = self.command_cards(snapshot)
                self.assertEqual(cards["connect"].state.text(), "READY")
                self.assertEqual(cards["connect"].detail.text(), "Connect to resume broker monitoring")
                self.assertTrue(cards["connect"].button.isEnabled())
                for key in ("ticker", "confirm", "start"):
                    self.assertFalse(cards[key].button.isEnabled())
                bar = self.status_bar(snapshot)
                self.assertEqual(bar.pills["Connection"].value.text(), "Disconnected")
                self.assertEqual(bar.pills["Connection"]._state, "waiting")
                self.assertEqual(bar.pills["Trading"].value.text(), "Paused: disconnected")
                self.assertEqual(bar.pills["Trading"]._state, "waiting")

    def test_missing_reconnect_evidence_does_not_claim_automatic_retry(self):
        snapshot = self.snapshot(outage="local")
        snapshot.pop("auto_reconnect_enabled")
        bar = self.status_bar(snapshot)
        self.assertEqual(bar.pills["Connection"].value.text(), "Disconnected")
        self.assertEqual(bar.pills["Trading"].value.text(), "Paused: disconnected")
        cards = self.command_cards(snapshot)
        self.assertEqual(cards["connect"].state.text(), "READY")
        self.assertTrue(cards["connect"].button.isEnabled())

    def test_idle_connected_session_waits_for_upstream_and_reconciliation(self):
        for outage in ("upstream", "unconfirmed", "reconcile"):
            with self.subTest(outage=outage):
                snapshot = self.snapshot(outage=outage)
                snapshot["active_cycle"] = None
                cards = self.command_cards(snapshot)
                for key in ("ticker", "confirm", "start"):
                    self.assertEqual(cards[key].state.text(), "WAITING")
                    self.assertFalse(cards[key].button.isEnabled())

    def test_real_recovery_faults_remain_red_even_during_an_outage(self):
        for stage in (Stage.MANUAL_REVIEW, Stage.ERROR):
            for outage in ("local", "upstream", "reconcile", ""):
                with self.subTest(stage=stage, outage=outage):
                    snapshot = self.snapshot(stage, outage)
                    snapshot["active_cycle"]["error_message"] = "Order outcome unknown; recovery blocked."
                    snapshot["trading_status"].update(summary="Blocked", state="risk")
                    cards = self.command_cards(snapshot)
                    self.assertEqual(cards["start"].state.text(), "ERROR")
                    self.assertFalse(cards["start"].button.isEnabled())
                    bar = self.status_bar(snapshot)
                    self.assertEqual(bar.pills["Trading"].value.text(), "Blocked")
                    self.assertEqual(bar.pills["Trading"]._state, "risk")

    def test_durable_fault_flags_are_not_hidden_as_a_reconnect_wait(self):
        for field in ("recovery_required", "storage_fault"):
            with self.subTest(field=field):
                snapshot = self.snapshot()
                snapshot[field] = {"active": True} if field == "storage_fault" else True
                cards = self.command_cards(snapshot)
                self.assertEqual(cards["start"].state.text(), "ERROR")
                self.assertFalse(cards["start"].button.isEnabled())
                bar = self.status_bar(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), snapshot["trading_status"]["summary"])

    def test_real_buy_guard_and_session_mismatch_are_still_blocked(self):
        snapshot = self.snapshot(Stage.WAIT_INITIAL_DROP, outage="")
        snapshot["active_cycle"]["error_message"] = "BUY blocked: hard risk limit reached."
        cards = self.command_cards(snapshot)
        self.assertEqual(cards["start"].state.text(), "BLOCKED")
        self.assertFalse(cards["start"].button.isEnabled())
        cards = self.command_cards(self.snapshot(outage=""), mismatch=True)
        self.assertEqual(cards["connect"].state.text(), "READY")
        self.assertTrue(cards["connect"].button.isEnabled())
        self.assertEqual(cards["start"].state.text(), "BLOCKED")
        self.assertFalse(cards["start"].button.isEnabled())

    def test_recovered_started_cycle_returns_to_done_without_a_second_start(self):
        snapshot = self.snapshot(outage="")
        cards = self.command_cards(snapshot)
        for key in ("connect", "start"):
            self.assertEqual(cards[key].state.text(), "DONE")
            self.assertFalse(cards[key].button.isEnabled())
        bar = self.status_bar(snapshot)
        self.assertEqual(bar.pills["Connection"]._state, "success")
        self.assertEqual(bar.pills["Data"]._state, "success")
        self.assertEqual(bar.pills["Trading"].value.text(), "Running")

    def test_stored_cycle_still_allows_explicit_start_after_broker_is_ready(self):
        snapshot = self.snapshot(Stage.MANUAL_REVIEW, outage="")
        snapshot["startup_resume_required"] = True
        cards = self.command_cards(snapshot)
        self.assertEqual(cards["start"].state.text(), "READY")
        self.assertEqual(cards["start"].detail.text(), "Click to resume stored cycle")
        self.assertTrue(cards["start"].button.isEnabled())
        snapshot["upstream_recovery_pending"] = True
        cards = self.command_cards(snapshot)
        self.assertEqual(cards["start"].state.text(), "ERROR")
        self.assertFalse(cards["start"].button.isEnabled())

    def test_waiting_card_uses_amber_and_keeps_the_button_disabled(self):
        card = self.gui.CommandStepCard("Start", Dummy())
        card.setStyleSheet = Mock()
        card.set_state("Waiting", False, "Reconciling")
        self.assertIn(self.gui._theme_hex("#d97706", "#f59e0b"), card.setStyleSheet.call_args.args[0])
        self.assertEqual(card.state.text(), "WAITING")
        self.assertFalse(card.button.isEnabled())

    def test_ribbon_waits_and_retains_stage_and_full_broker_evidence(self):
        for outage, connection_text, trading_text in (
            ("local", "Reconnecting", "Paused: reconnecting"),
            ("upstream", "Waiting for IBKR", "Paused: reconnecting"),
            ("unconfirmed", "Checking link", "Paused: reconnecting"),
            ("reconcile", "Reconciling", "Paused: reconciling"),
        ):
            with self.subTest(outage=outage):
                snapshot = self.snapshot(outage=outage)
                bar = self.status_bar(snapshot)
                self.assertEqual(bar.pills["Connection"].value.text(), connection_text)
                self.assertEqual(bar.pills["Connection"]._state, "waiting")
                self.assertEqual(bar.pills["Data"]._state, "waiting")
                self.assertEqual(bar.pills["Trading"].value.text(), trading_text)
                self.assertEqual(bar.pills["Trading"]._state, "waiting")
                self.assertEqual(bar.pills["Trading"].toolTip(), snapshot["trading_status"]["tooltip"])
                self.assertEqual(bar.pills["Stage"].value.text(), "3 of 5")

    def test_outage_does_not_hide_unrelated_trading_guards(self):
        for code in ("app_owned_position", "last_guard", "guard_evaluation_unavailable"):
            with self.subTest(code=code):
                snapshot = self.snapshot()
                snapshot["trading_status"]["blockers"].append({"code": code})
                bar = self.status_bar(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), snapshot["trading_status"]["summary"])

    def test_legacy_status_without_structured_trading_status_never_claims_running(self):
        snapshot = self.snapshot()
        snapshot.pop("trading_status")
        bar = self.status_bar(snapshot)
        self.assertEqual(bar.pills["Trading"].value.text(), "Paused: reconnecting")
        snapshot["active_cycle"].update(stage=Stage.MANUAL_REVIEW.value, error_message="Recovery blocked")
        bar = self.status_bar(snapshot)
        self.assertEqual(bar.pills["Trading"]._state, "risk")

    def test_manual_lock_remains_effective_during_outage(self):
        cards = self.command_cards(self.snapshot(), locked=True)
        for card in cards.values():
            self.assertEqual(card.state.text(), "LOCKED")
            self.assertFalse(card.button.isEnabled())


if __name__ == "__main__":
    unittest.main()
