"""GUI-only connection/freshness regressions; synthetic data and no broker calls."""

from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.models import Stage
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _ControllerStub


class NamedLabel(Dummy):
    """Record the Qt style selector, which the generic stub does not store."""

    def setObjectName(self, name):
        self._object_name = name

    def objectName(self):
        return self.__dict__.get("_object_name", "")


def _blocker(code: str, side: str = "SELL") -> dict[str, str]:
    short = {
        "rth_closed": "RTH closed",
        "stale_data": "Stale data",
        "fresh_market_data_pending": "Waiting for data",
        "no_price": "No usable price",
    }.get(code, code)
    return {"code": code, "side": side, "short": short, "message": f"{side} blocked by {code}"}


def _snapshot(*, invalidated: bool = False) -> dict:
    data_code = "invalidated" if invalidated else "stale"
    blockers = [_blocker("fresh_market_data_pending" if invalidated else "stale_data"), _blocker("rth_closed")]
    return {
        "connected": True,
        "auto_reconnect_enabled": True,
        "connection": {"trading_mode": "paper"},
        "broker_connectivity": {
            "local_connected": True,
            "upstream_connected": True,
            "awaiting_fresh_market_data": invalidated,
            "message": "IBKR server link confirmed; fresh market data is arriving.",
            "error_code": 2104,
        },
        "strategy": {"max_selected_price_age_seconds": 3.0},
        "active_cycle": {"stage": Stage.WAIT_RISE_TRIGGER.value, "ticker": "TEST", "rth_only": True},
        "price_snapshot": {
            "price": 100.0,
            "source": "markPrice",
            "status": "No usable price" if invalidated else "OK - auto selected markPrice",
            "contract": {"ticker": "TEST", "currency": "USD"},
            "requested_market_data_type": 1,
            "selected_market_data_type": 1,
            "subscription_market_data_type": 1,
            "market_data_event_tracking": True,
            "market_data_event_tracking_available": True,
            "market_data_update_sequence": 4,
            "api_data_state": data_code,
            "api_data_age_seconds": 22680.0,
            "api_data_present": True,
            "api_data_invalidated": invalidated,
            "api_data_invalidated_reason": "Awaiting a new event after IBKR code 2104" if invalidated else "",
            "api_last_data_received_at": "2026-09-27T11:18:29+00:00",
            "timestamp": "2026-09-27T17:36:29+00:00",
            "age_seconds": 0.0,
            "rth_open": False,
            "strategy_price_usable": False,
        },
        "trading_status": {
            "summary": f"SELL blocked: {blockers[0]['short']} +1",
            "state": "waiting",
            "tooltip": "\n".join(item["message"] for item in blockers),
            "blockers": blockers,
        },
    }


class StatusPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def render(self, snapshot):
        before = deepcopy(snapshot)
        bar = self.gui.LiveStatusBar()
        bar.update_data(snapshot)
        self.assertEqual(snapshot, before, "Rendering must not change controller facts")
        return bar

    def panel(self, price):
        before = deepcopy(price)
        with patch.object(self.gui, "QLabel", NamedLabel):
            panel = self.gui.PricePanel()
        panel.update_data(None, price)
        self.assertEqual(price, before, "Rendering must not change quote freshness or timestamps")
        return panel

    def legacy_window(self):
        with patch.object(self.gui, "QLabel", NamedLabel):
            window = self.gui.MainWindow(_ControllerStub())
            window._price_feed_group()
        return window

    def test_closed_market_cached_prices_have_matching_ribbon_states(self):
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                snapshot = _snapshot(invalidated=invalidated)
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Connection"].value.text(), "Connected")
                self.assertEqual(bar.pills["Connection"]._state, "success")
                self.assertEqual(bar.pills["Data"].value.text(), "Live / Stale 6.3h")
                self.assertEqual(bar.pills["Data"]._state, "waiting")
                self.assertEqual(bar.pills["Trading"].value.text(), "SELL blocked: RTH closed")
                self.assertEqual(bar.pills["Trading"]._state, "waiting")
                self.assertFalse(snapshot["price_snapshot"]["strategy_price_usable"])

    def test_connection_tooltip_does_not_claim_stale_data_is_arriving(self):
        bar = self.render(_snapshot())
        tooltip = bar.pills["Connection"].toolTip()
        self.assertIn("Local API socket: connected", tooltip)
        self.assertIn("Gateway/TWS to IBKR servers: connected", tooltip)
        self.assertNotIn("fresh market data is arriving", tooltip)
        self.assertEqual(bar.pills["Connection"].title.toolTip(), tooltip)
        self.assertEqual(bar.pills["Connection"].value.toolTip(), tooltip)

    def test_invalidated_state_and_reason_remain_in_data_tooltip(self):
        snapshot = _snapshot(invalidated=True)
        bar = self.render(snapshot)
        tooltip = bar.pills["Data"].toolTip()
        self.assertIn("invalidated", tooltip)
        self.assertIn(snapshot["price_snapshot"]["api_data_invalidated_reason"], tooltip)
        self.assertIn("22680.0s", tooltip)
        self.assertEqual(bar.pills["Data"].title.toolTip(), tooltip)
        self.assertEqual(bar.pills["Data"].value.toolTip(), tooltip)

    def test_disconnection_and_recovery_connection_states_remain_prominent(self):
        cases = (
            ({"connected": False}, {}, "Reconnecting", "waiting"),
            ({}, {"local_connected": False}, "Reconnecting", "waiting"),
            ({}, {"upstream_connected": False}, "Waiting for IBKR", "waiting"),
            ({"upstream_recovery_pending": True}, {}, "Reconciling", "waiting"),
            ({}, {"upstream_connected": None}, "Checking link", "waiting"),
        )
        for outer, connection, expected, state in cases:
            with self.subTest(expected=expected, outer=outer):
                snapshot = _snapshot(invalidated=True)
                snapshot.update(outer)
                snapshot["broker_connectivity"].update(connection)
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Connection"].value.text(), expected)
                self.assertEqual(bar.pills["Connection"]._state, state)

    def test_recent_invalidated_price_does_not_become_green_or_stale(self):
        snapshot = _snapshot(invalidated=True)
        snapshot["price_snapshot"]["api_data_age_seconds"] = 0.5
        bar = self.render(snapshot)
        self.assertEqual(bar.pills["Connection"].value.text(), "Connected")
        self.assertEqual(bar.pills["Data"]._state, "waiting")
        self.assertIn("Waiting", bar.pills["Data"].value.text())
        self.assertNotIn("Stale", bar.pills["Data"].value.text())

    def test_upstream_loss_or_unavailable_tracking_are_not_normalized_as_stale(self):
        for upstream in (True, False):
            with self.subTest(upstream=upstream):
                snapshot = _snapshot(invalidated=True)
                snapshot["broker_connectivity"]["upstream_connected"] = upstream
                snapshot["price_snapshot"]["market_data_event_tracking_available"] = False
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Data"]._state, "risk" if upstream else "waiting")
                expected = "Update tracking unavailable" if upstream else "Waiting for IBKR"
                self.assertEqual(bar.pills["Data"].value.text(), expected)

    def test_price_never_received_is_not_presented_as_an_old_cached_price(self):
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                snapshot = _snapshot(invalidated=invalidated)
                snapshot["price_snapshot"].update(price=None, api_data_age_seconds=None, api_last_data_received_at=None)
                bar = self.render(snapshot)
                self.assertNotIn("Stale", bar.pills["Data"].value.text())
                self.assertNotEqual(bar.pills["Data"]._state, "success")

    def test_fresh_and_delayed_subscriptions_keep_their_existing_semantics(self):
        for mode, state in ((1, "success"), (3, "waiting"), (4, "waiting")):
            with self.subTest(mode=mode):
                snapshot = _snapshot()
                snapshot["price_snapshot"].update(
                    api_data_state="recent", api_data_age_seconds=0.5, subscription_market_data_type=mode,
                )
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Data"]._state, state)
                self.assertIn("Update 0.5s", bar.pills["Data"].value.text())

    def test_rth_headline_preserves_full_original_tooltip_on_all_trading_widgets(self):
        snapshot = _snapshot(invalidated=True)
        bar = self.render(snapshot)
        tooltip = snapshot["trading_status"]["tooltip"]
        for widget in (bar.pills["Trading"], bar.pills["Trading"].title, bar.pills["Trading"].value):
            self.assertEqual(widget.toolTip(), tooltip)

    def test_buy_wait_stage_can_show_rth_closed_without_hiding_blocker_details(self):
        snapshot = _snapshot()
        snapshot["active_cycle"]["stage"] = Stage.WAIT_INITIAL_DROP.value
        status = snapshot["trading_status"]
        status["blockers"] = [_blocker("stale_data", "BUY"), _blocker("rth_closed", "BUY")]
        status["summary"] = "BUY blocked: Stale data +1"
        status["tooltip"] = "BUY blocked by stale_data\nBUY blocked by rth_closed"
        bar = self.render(snapshot)
        self.assertEqual(bar.pills["Trading"].value.text(), "BUY blocked: RTH closed")
        self.assertEqual(bar.pills["Trading"].toolTip(), status["tooltip"])

    def test_other_blockers_and_no_price_preserve_controller_headline(self):
        for code in ("no_price", "storage", "recovery", "disconnected", "upstream_disconnected", "atr_warmup", "last_guard", "unknown"):
            with self.subTest(code=code):
                snapshot = _snapshot()
                status = snapshot["trading_status"]
                status["blockers"].append(_blocker(code))
                status["summary"] = f"SELL blocked: {code} +2"
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), status["summary"])
                self.assertEqual(bar.pills["Trading"].toolTip(), status["tooltip"])

    def test_order_monitoring_and_recovery_stages_are_not_relabelled(self):
        for stage in (Stage.BUY_TRAIL_ACTIVE, Stage.SELL_TRAIL_ACTIVE, Stage.MANUAL_REVIEW, Stage.ERROR, Stage.STOPPED):
            with self.subTest(stage=stage):
                snapshot = _snapshot()
                snapshot["active_cycle"]["stage"] = stage.value
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), snapshot["trading_status"]["summary"])

    def test_nonwaiting_controller_states_are_not_relabelled(self):
        for state, summary in (("risk", "Storage fault"), ("active", "Native SELL active"), ("inactive", "Stopped")):
            with self.subTest(state=state):
                snapshot = _snapshot()
                snapshot["trading_status"].update(state=state, summary=summary)
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), summary)
                self.assertEqual(bar.pills["Trading"]._state, state)

    def test_recovery_and_connection_flags_prevent_simplified_trading_headline(self):
        for flag in ("recovery_required", "startup_resume_required", "upstream_recovery_pending", "storage_fault", "connected"):
            with self.subTest(flag=flag):
                snapshot = _snapshot()
                snapshot[flag] = {"active": True} if flag == "storage_fault" else flag != "connected"
                bar = self.render(snapshot)
                self.assertEqual(bar.pills["Trading"].value.text(), snapshot["trading_status"]["summary"])

    def test_rth_open_or_absent_rth_blocker_preserves_original_summary(self):
        snapshot = _snapshot()
        snapshot["price_snapshot"]["rth_open"] = True
        snapshot["trading_status"]["blockers"] = [_blocker("stale_data")]
        snapshot["trading_status"]["summary"] = "SELL blocked: Stale data"
        bar = self.render(snapshot)
        self.assertEqual(bar.pills["Trading"].value.text(), "SELL blocked: Stale data")

    def test_monitor_uses_actual_update_time_and_calls_cache_check_a_cache_check(self):
        price = _snapshot()["price_snapshot"]
        panel = self.panel(price)
        self.assertEqual(panel.price_source.text(), "Source: markPrice | Last market update: 2026-09-27 11:18:29 UTC")
        self.assertNotIn("17:36:29", panel.price_source.text())
        self.assertIn("Cached snapshot checked: 0.0s ago", panel.price_refresh.text())
        self.assertNotIn("Snapshot age:", panel.price_refresh.text())

    def test_actual_event_timestamp_aliases_are_used_without_cached_timestamp_fallback(self):
        for field in ("api_last_data_received_at", "api_data_last_received_at", "market_data_update_received_at", None):
            with self.subTest(field=field):
                price = _snapshot()["price_snapshot"]
                price.pop("api_last_data_received_at")
                if field:
                    price[field] = "2026-09-27T11:18:29+00:00"
                panel = self.panel(price)
                expected = "2026-09-27 11:18:29 UTC" if field else "-"
                self.assertEqual(panel.price_source.text(), f"Source: markPrice | Last market update: {expected}")
                self.assertNotIn("17:36:29", panel.price_source.text())

    def test_old_price_remains_visible_but_is_marked_cached_for_both_states(self):
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                panel = self.panel(_snapshot(invalidated=invalidated)["price_snapshot"])
                self.assertIn("100", panel.big_price.text())
                self.assertIn("Cached price — market data stale", panel.price_status.text())
                self.assertNotIn("OK", panel.price_status.text())
                self.assertNotIn("No usable price", panel.price_status.text())

    def test_recent_invalidated_monitor_price_remains_waiting(self):
        price = _snapshot(invalidated=True)["price_snapshot"]
        price["api_data_age_seconds"] = 0.5
        panel = self.panel(price)
        self.assertIn("Cached price — awaiting a new market update", panel.price_status.text())
        self.assertNotEqual(panel.api_indicator_dot.objectName(), "ApiIndicatorGood")

    def test_fresh_monitor_status_and_price_are_preserved(self):
        price = _snapshot()["price_snapshot"]
        price.update(api_data_state="recent", api_data_age_seconds=0.5, strategy_price_usable=True)
        panel = self.panel(price)
        self.assertIn("OK - auto selected markPrice", panel.price_status.text())
        self.assertIn("100", panel.big_price.text())
        self.assertEqual(panel.api_indicator_dot.objectName(), "ApiIndicatorGood")

    def test_legacy_price_feed_uses_actual_update_time_and_warning_for_cached_prices(self):
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                window = self.legacy_window()
                price = _snapshot(invalidated=invalidated)["price_snapshot"]
                before = deepcopy(price)
                window._update_price_feed(price)
                self.assertEqual(price, before)
                self.assertIn("Cached price — market data stale", window.price_status_label.text())
                self.assertEqual(window.price_status_label.objectName(), "PriceStatusWarning")
                self.assertEqual(window.price_updated_label.text(), "Last market update (UTC): 2026-09-27 11:18:29 UTC")
                self.assertIn("cached snapshot checked 0.0s ago", window.price_mode_label.text())

    def test_legacy_price_feed_never_labels_snapshot_timestamp_a_market_update(self):
        window = self.legacy_window()
        price = _snapshot()["price_snapshot"]
        del price["api_last_data_received_at"]
        window._update_price_feed(price)
        self.assertEqual(window.price_updated_label.text(), "Last market update (UTC): -")
        self.assertNotIn("17:36:29", window.price_updated_label.text())

    def test_custom_freshness_limit_is_consistent_in_ribbon_monitor_and_legacy_feed(self):
        cases = (
            (None, 10.0, "success", "PriceStatusGood", "ApiIndicatorGood"),
            ({"max_selected_price_age_seconds": 3.0}, 10.0, "success", "PriceStatusGood", "ApiIndicatorGood"),
            ({"max_selected_price_age_seconds": 10.0}, 3.0, "waiting", "PriceStatusWarning", "ApiIndicatorWarn"),
        )
        for cycle, strategy_limit, state, price_style, api_style in cases:
            with self.subTest(cycle=cycle, strategy_limit=strategy_limit):
                snapshot = _snapshot()
                snapshot["active_cycle"] = cycle
                snapshot["strategy"]["max_selected_price_age_seconds"] = strategy_limit
                snapshot["price_snapshot"].update(api_data_state="recent", api_data_age_seconds=5.0)
                before = deepcopy(snapshot)
                window = self.legacy_window()
                window.current_snapshot = snapshot
                window._update_price_feed(snapshot["price_snapshot"])
                bar = self.render(snapshot)
                self.assertEqual(snapshot, before)
                self.assertEqual(bar.pills["Data"]._state, state)
                self.assertEqual(window.price_status_label.objectName(), price_style)
                self.assertEqual(window.price_panel.api_indicator_dot.objectName(), api_style)
                self.assertEqual("Cached price" in window.price_panel.price_status.text(), state == "waiting")


if __name__ == "__main__":
    unittest.main()
