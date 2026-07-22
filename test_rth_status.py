from datetime import datetime, timezone

from app.ib_adapter import IbAsyncTwsAdapter


def test_parse_liquid_hours_open_window():
    now = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260105:0930-20260105:1600",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is True
    assert status.session_open.startswith("2026-01-05T09:30:00")
    assert status.session_close.startswith("2026-01-05T16:00:00")
    assert status.session_date == "20260105"


def test_parse_liquid_hours_closed_day():
    now = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260105:CLOSED",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is False
    assert status.session_open == ""
    assert status.session_close == ""
    assert status.session_date == "20260105"


def test_parse_liquid_hours_early_close_exposes_session_boundaries():
    now = datetime(2026, 7, 3, 16, 50, tzinfo=timezone.utc)  # 12:50 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260703:0930-20260703:1300",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is True
    assert status.session_open.startswith("2026-07-03T09:30:00")
    assert status.session_close.startswith("2026-07-03T13:00:00")


def test_parse_liquid_hours_multiple_ranges_uses_outer_regular_session():
    now = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260105:0930-20260105:1200,20260105:1300-20260105:1600",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is True
    assert status.session_open.startswith("2026-01-05T09:30:00")
    assert status.session_close.startswith("2026-01-05T16:00:00")


def test_parse_liquid_hours_compact_early_close_endpoint():
    now = datetime(2026, 7, 3, 16, 50, tzinfo=timezone.utc)  # 12:50 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260703:0930-1300",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is True
    assert status.session_close.startswith("2026-07-03T13:00:00")


def test_parse_liquid_hours_split_session_gap_is_closed_but_keeps_outer_boundaries():
    now = datetime(2026, 1, 5, 17, 30, tzinfo=timezone.utc)  # 12:30 New York.
    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        "20260105:0930-1200,1300-1600",
        "America/New_York",
        now,
    )
    assert status is not None
    assert status.is_open is False
    assert status.session_open.startswith("2026-01-05T09:30:00")
    assert status.session_close.startswith("2026-01-05T16:00:00")



def test_fallback_us_equity_rth_open_and_closed():
    open_status = IbAsyncTwsAdapter._fallback_us_equity_rth(datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc))
    closed_status = IbAsyncTwsAdapter._fallback_us_equity_rth(datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc))
    assert open_status.is_open is True
    assert closed_status.is_open is False
    assert open_status.session_open.startswith("2026-01-05T09:30:00")
    assert open_status.session_close.startswith("2026-01-05T16:00:00")
