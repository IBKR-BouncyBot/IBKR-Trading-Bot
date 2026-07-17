"""Accelerated, deterministic soak tests for bounded runtime structures."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.market_data_capture import MarketDataCaptureManager
from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub

pytestmark = pytest.mark.soak


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def test_market_capture_buffer_stays_at_configured_hard_limit(tmp_path: Path) -> None:
    manager = MarketDataCaptureManager(
        tmp_path / "captures",
        pre_window_seconds=10_000.0,
        post_window_seconds=10_000.0,
        buffer_window_seconds=10_000.0,
        max_rows=1_000,
        async_writes=False,
    )

    for index in range(25_000):
        manager.record_snapshot(
            {"price": 100.0 + (index % 100) / 100.0, "sequence": index},
            monotonic_ts=index / 10.0,
        )

    assert manager.buffer_size == 1_000
    assert manager.pending_count == 0
    assert manager.completed_files == []
    assert manager._buffer[0]["sequence"] == 24_000
    assert manager._buffer[-1]["sequence"] == 24_999


def test_incremental_atr_bar_storage_stays_bounded_to_512(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = make_controller(
        controller_module,
        tmp_path / "atr_bars.sqlite",
        broker,
        permissive_strategy(),
    )

    controller._atr_bar_seconds_cache = 60
    for index in range(2_000):
        controller._update_incremental_atr_bars(
            timestamp=float(index * 60),
            price=100.0 + (index % 37) / 10.0,
            bar_seconds=60,
        )

    assert len(controller._atr_bars) == 512
    assert controller._atr_bars[0]["bucket"] == pytest.approx(1_488.0)
    assert controller._atr_bars[-1]["bucket"] == pytest.approx(1_999.0)


def test_price_event_soak_keeps_history_bounded_and_consumes_each_sequence_once(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = make_controller(
        controller_module,
        tmp_path / "prices.sqlite",
        broker,
        permissive_strategy(),
    )
    controller._market_capture.enabled = False
    controller._latest_rth_status = broker.regular_trading_hours_status(broker.contract).to_dict()
    latest = None
    for index in range(22_500):
        latest = broker.publish_price(100.0 + (index % 101) / 100.0)
        controller._record_price_snapshot(latest, broker.contract)
    assert latest is not None

    assert len(controller._price_history) == 21_600
    assert controller._api_data_seen_count == 22_500
    assert len(controller._atr_bars) <= 512

    before_seen = controller._api_data_seen_count
    before_history = len(controller._price_history)
    for _ in range(1_000):
        controller._record_price_snapshot(latest, broker.contract)

    assert controller._api_data_seen_count == before_seen
    assert len(controller._price_history) == before_history
    assert controller.price_snapshot["cached_fields_only"] is True


def test_hundreds_of_completed_cycles_keep_history_and_reinvestment_exact(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy()
    settings.reinvest_profits = True
    controller = make_controller(controller_module, tmp_path / "cycles.sqlite", broker, settings)

    expected_total = 0.0
    for number in range(1, 251):
        cycle = StrategyEngine.start_cycle(settings, number, "", 100.0, 0.0)
        cycle.stage = Stage.CYCLE_COMPLETE
        cycle.buy_filled_qty = 10
        cycle.sell_filled_qty = 10
        cycle.avg_buy_price = 100.0
        cycle.avg_sell_price = 100.0 + ((number % 11) - 5) / 10.0
        cycle.gross_pnl = (cycle.avg_sell_price - cycle.avg_buy_price) * 10
        cycle.net_pnl = cycle.gross_pnl - 0.20
        expected_total += cycle.net_pnl
        controller.storage.upsert_cycle(cycle)

    summary = controller.storage.history_summary("AAPL")
    history = controller.storage.history_cycles("AAPL", limit=1_000)

    assert summary["cycles"] == 250
    assert len(history) == 250
    assert summary["total_net_pnl"] == pytest.approx(expected_total)
    assert controller.storage.get_realized_net_profit_for_ticker("AAPL") == pytest.approx(expected_total)
    assert controller.storage.get_next_cycle_number("AAPL") == 251


def test_connection_flap_soak_does_not_accumulate_events_or_threads() -> None:
    broker = DeterministicBrokerAdapter()
    baseline_threads = {thread.ident for thread in threading.enumerate()}

    for index in range(1_000):
        broker.upstream_lost(code=1100 if index % 2 == 0 else 2110)
        assert len(broker.drain_broker_events()) == 1
        broker.upstream_restored(data_lost=index % 3 == 0)
        assert len(broker.drain_broker_events()) == 1
        broker.publish_price(100.0 + index / 10_000.0)

    assert list(broker.events) == []
    assert broker.update_sequence >= 1
    assert broker.subscription_generation == 1 + sum(1 for index in range(1_000) if index % 3 == 0)
    assert {thread.ident for thread in threading.enumerate()} == baseline_threads
