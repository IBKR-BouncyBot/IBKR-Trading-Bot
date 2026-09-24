"""Authoritative RTH evidence and display regressions; no broker sockets or Qt."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ib_adapter import IbAsyncTwsAdapter
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v500_broker_safety import _setup


class AuthoritativeSessionTests(unittest.TestCase):
    def test_us_hours_without_timezone_cannot_open_a_session(self) -> None:
        adapter, broker, contract = _setup()
        broker.details = [SimpleNamespace(liquidHours="20260924:0930-1600", timeZoneId="")]
        now = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
        with patch("app.ib_adapter.datetime", SimpleNamespace(now=lambda _tz: now, strptime=datetime.strptime)):
            status = adapter.regular_trading_hours_status(contract)
        self.assertFalse(status.is_open)
        self.assertEqual(status.source, "contract_rth_unavailable")
        self.assertIn("without a timeZoneId", status.message)
        self.assertEqual((status.session_open, status.session_close, status.time_zone), ("", "", ""))

    def test_missing_invalid_and_stale_metadata_have_no_session_boundaries(self) -> None:
        now = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
        cases = (
            ("", "America/New_York"),
            ("20260924:bad-range", "America/New_York"),
            ("20260923:0930-1600", "America/New_York"),
            ("20260924:0930-1600", "Not/A-Timezone"),
        )
        for hours, zone in cases:
            with self.subTest(hours=hours, zone=zone):
                adapter, broker, contract = _setup()
                broker.details = [SimpleNamespace(liquidHours=hours, timeZoneId=zone)]
                clock = SimpleNamespace(now=lambda _tz: now, strptime=datetime.strptime)
                with patch("app.ib_adapter.datetime", clock):
                    status = adapter.regular_trading_hours_status(contract)
                self.assertFalse(status.is_open)
                self.assertEqual((status.session_open, status.session_close), ("", ""))
                self.assertFalse(status.source.startswith("fallback"))

    def test_cached_qualified_metadata_preserves_early_close_without_request(self) -> None:
        adapter, broker, contract = _setup()
        contract.liquid_hours = "20260703:0930-1300"
        contract.time_zone = "America/New_York"
        now = datetime(2026, 7, 3, 16, 50, tzinfo=timezone.utc)
        with patch.object(broker, "reqContractDetails", side_effect=AssertionError("cached metadata should be used")):
            with patch("app.ib_adapter.datetime", SimpleNamespace(now=lambda _tz: now, strptime=datetime.strptime)):
                status = adapter.regular_trading_hours_status(contract)
        self.assertTrue(status.is_open)
        self.assertEqual(status.session_close, "2026-07-03T13:00:00-04:00")

    def test_london_policy_still_only_narrows_authoritative_session(self) -> None:
        now = datetime(2026, 7, 24, 15, 35, tzinfo=timezone.utc)
        status = IbAsyncTwsAdapter._parse_liquid_hours_window(
            "20260724:0800-1650", "Europe/London", now
        )
        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.is_open)
        capped = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(status, "LSE", now)
        self.assertFalse(capped.is_open)
        self.assertEqual(capped.session_close, "2026-07-24T16:30:00+01:00")
        self.assertEqual(capped.ibkr_session_close, "2026-07-24T16:50:00+01:00")


class AuthoritativeSessionDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.__exit__(None, None, None)

    def test_missing_or_invalid_evidence_displays_no_hours_or_countdown(self) -> None:
        checked = "2026-09-24T12:00:00+00:00"
        cases = (
            {},
            {"time_zone": "America/New_York"},
            {"liquid_hours": "20260924:0930-1600"},
            {"liquid_hours": "20260924:0930-1600", "time_zone": "Not/A-Timezone"},
            {"liquid_hours": "20260924:bad-range", "time_zone": "America/New_York"},
            {"liquid_hours": "20260924:CLOSED", "time_zone": "America/New_York"},
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                status = dict(evidence, checked_at=checked, source="contract_rth_unavailable")
                self.assertIsNone(self.gui._rth_window_from_status(status, checked))
                snapshot = {"rth_open": False, "rth_status": status}
                for short in (False, True):
                    text = self.gui._format_rth_status(snapshot, short=short)
                    self.assertNotIn("Regular hours", text)
                    self.assertNotIn("opens in", text)
                    self.assertNotIn("closes in", text)

    def test_old_fallback_boundaries_are_not_displayed_as_authoritative(self) -> None:
        status = {
            "source": "fallback_us_equity", "time_zone": "America/New_York",
            "checked_at": "2025-12-25T16:00:00+00:00",
            "session_open": "2025-12-25T09:30:00-05:00",
            "session_close": "2025-12-25T16:00:00-05:00",
        }
        self.assertIsNone(self.gui._rth_window_from_status(status, status["checked_at"]))

    def test_authoritative_early_close_keeps_correct_countdown(self) -> None:
        snapshot = {
            "rth_open": True,
            "rth_status": {
                "source": "contract_liquid_hours", "time_zone": "America/New_York",
                "liquid_hours": "20260703:0930-1300",
                "checked_at": "2026-07-03T16:50:00+00:00",
            },
        }
        self.assertEqual(self.gui._format_rth_status(snapshot, short=True), "RTH open - closes in 10m")
        self.assertIn("09:30-13:00", self.gui._format_rth_status(snapshot))

    def test_next_published_session_is_displayed_on_closed_day(self) -> None:
        status = {
            "source": "contract_liquid_hours", "time_zone": "America/New_York",
            "liquid_hours": "20261225:CLOSED;20261228:0930-1600",
            "checked_at": "2026-12-25T16:00:00+00:00",
        }
        window = self.gui._rth_window_from_status(status, status["checked_at"])
        self.assertIsNotNone(window)
        self.assertEqual(window["start_local"].isoformat(), "2026-12-28T09:30:00-05:00")
        self.assertIn("opens in", self.gui._format_rth_status({"rth_open": False, "rth_status": status}, short=True))

    def test_effective_session_bounds_override_broader_liquid_hours(self) -> None:
        status = {
            "source": "contract_liquid_hours_continuous_session", "time_zone": "Europe/London",
            "liquid_hours": "20260724:0800-1650", "checked_at": "2026-07-24T15:20:00+00:00",
            "session_open": "2026-07-24T08:00:00+01:00",
            "session_close": "2026-07-24T16:30:00+01:00",
        }
        text = self.gui._format_rth_status({"rth_open": True, "rth_status": status}, short=True)
        self.assertEqual(text, "RTH open - closes in 10m")


if __name__ == "__main__":
    unittest.main()
