from __future__ import annotations

import zipfile
from pathlib import Path

from app.market_data_capture import MarketDataCaptureManager


def test_trade_capture_writes_only_after_post_window_complete(tmp_path: Path):
    manager = MarketDataCaptureManager(tmp_path, pre_window_seconds=2, post_window_seconds=3, buffer_window_seconds=10)
    for i, price in enumerate([100.0, 99.5, 99.0]):
        manager.record_snapshot({"ticker": "AAPL", "price": price, "source": "test", "stage": "2_BUY_TRAIL_ACTIVE"}, monotonic_ts=float(i), wall_time_utc=f"t{i}")

    event_id = manager.start_capture(
        event_type="BUY_FILL",
        event_monotonic=2.0,
        ticker="AAPL",
        cycle_id="cycle-1",
        cycle_number=1,
        order_ref="IBKRBOT|AAPL|CYCLE-1|BUY_TRAIL",
        perm_id=123,
        payload={"order": {"status": "Filled"}},
    )
    assert event_id
    assert manager.pending_count == 1
    assert list(tmp_path.rglob("*.zip")) == []

    manager.record_snapshot({"ticker": "AAPL", "price": 100.5, "source": "test", "stage": "3_WAIT_RISE_TRIGGER"}, monotonic_ts=4.0, wall_time_utc="t4")
    assert list(tmp_path.rglob("*.zip")) == []

    manager.record_snapshot({"ticker": "AAPL", "price": 101.0, "source": "test", "stage": "3_WAIT_RISE_TRIGGER"}, monotonic_ts=5.0, wall_time_utc="t5")
    files = list(tmp_path.rglob("*.zip"))
    assert len(files) == 1
    assert manager.pending_count == 0
    with zipfile.ZipFile(files[0]) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "event.json", "market_data.csv", "market_data.jsonl"} <= names
        csv_text = zf.read("market_data.csv").decode()
        assert "AAPL" in csv_text
        assert "100.5" in csv_text


def test_capture_buffer_prunes_old_rows(tmp_path: Path):
    manager = MarketDataCaptureManager(tmp_path, pre_window_seconds=2, post_window_seconds=1, buffer_window_seconds=3, max_rows=100)
    for i in range(10):
        manager.record_snapshot({"ticker": "AAPL", "price": 100 + i}, monotonic_ts=float(i))
    assert manager.buffer_size <= 4
