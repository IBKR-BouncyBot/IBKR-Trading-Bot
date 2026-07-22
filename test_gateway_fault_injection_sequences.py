"""Generated outage, reconnection, stale-data, and submission-race tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models import Stage, StrategyAction
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


class _ConnectivityRaceAdapter(DeterministicBrokerAdapter):
    """Lose the upstream link after preflight but at the placement boundary."""

    def _place(self, *, order_type: str, **kwargs: Any) -> Any:
        self.upstream_lost()
        return super()._place(order_type=order_type, **kwargs)


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _waiting_controller(
    controller_module: Any,
    db: Path,
    broker: DeterministicBrokerAdapter,
) -> Any:
    settings = permissive_strategy()
    settings.stale_data_guard_enabled = True
    settings.max_selected_price_age_seconds = 3.0
    controller = make_controller(controller_module, db, broker, settings)
    controller.storage.backup_database = lambda *args, **kwargs: None
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    publish_fresh_price(controller, broker, 100.0)
    return controller


@pytest.mark.parametrize("data_lost", [False, True])
@pytest.mark.parametrize("iterations", [1, 5, 20])
def test_repeated_upstream_flapping_never_reuses_pre_outage_quote(
    controller_module: Any,
    tmp_path: Path,
    data_lost: bool,
    iterations: int,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = _waiting_controller(
        controller_module,
        tmp_path / f"flap_{data_lost}_{iterations}.sqlite",
        broker,
    )
    initial_seen = controller._api_data_seen_count

    for index in range(iterations):
        cached_before_outage = broker.cached_snapshot()
        broker.upstream_lost()
        controller._tick()

        assert controller._api_data_invalidated is True
        assert controller._order_submission_connectivity_message("BUY")
        before_cached_read = controller._api_data_seen_count
        controller._record_price_snapshot(cached_before_outage, broker.contract)
        assert controller._api_data_seen_count == before_cached_read
        assert controller.price_snapshot["strategy_price_usable"] is False

        broker.upstream_restored(data_lost=data_lost)
        controller._last_upstream_recovery_attempt_monotonic = 0.0
        controller._tick()

        assert controller._upstream_recovery_pending is False
        assert controller._api_data_invalidated is True
        assert controller.active_cycle is not None
        assert controller._stale_data_guard_message_for_buy(controller.active_cycle)

        publish_fresh_price(controller, broker, 100.0 + index / 100.0)
        controller._refresh_broker_connectivity_snapshot(detect_transition=False)
        assert controller._api_data_invalidated is False
        assert controller._order_submission_connectivity_message("BUY") is None

    assert controller._api_data_seen_count == initial_seen + iterations


@pytest.mark.parametrize("code", [1100, 2110])
def test_all_upstream_loss_codes_pause_price_processing_and_atr(
    controller_module: Any,
    tmp_path: Path,
    code: int,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = _waiting_controller(controller_module, tmp_path / f"loss_{code}.sqlite", broker)
    initial_seen = controller._api_data_seen_count
    initial_history = len(controller._price_history)
    initial_bars = len(controller._atr_bars)

    broker.upstream_lost(code=code)
    controller._tick()
    cached = broker.cached_snapshot()
    for _ in range(100):
        controller._record_price_snapshot(cached, broker.contract)

    assert controller._api_data_seen_count == initial_seen
    assert len(controller._price_history) == initial_history
    assert len(controller._atr_bars) == initial_bars
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller._order_submission_connectivity_message("BUY")


@pytest.mark.parametrize("data_lost", [False, True])
def test_restoration_reconciliation_failure_keeps_trading_paused(
    controller_module: Any,
    tmp_path: Path,
    data_lost: bool,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = _waiting_controller(
        controller_module,
        tmp_path / f"reconcile_fail_{data_lost}.sqlite",
        broker,
    )
    broker.upstream_lost()
    controller._tick()
    broker.upstream_restored(data_lost=data_lost)
    broker.fail_operations.add("open_orders")
    controller._last_upstream_recovery_attempt_monotonic = 0.0

    controller._tick()

    assert controller._upstream_recovery_pending is True
    assert controller._order_submission_connectivity_message("BUY")
    assert "reconciliation failed" in controller.status.lower()

    broker.fail_operations.remove("open_orders")
    controller._last_upstream_recovery_attempt_monotonic = 0.0
    controller._tick()
    publish_fresh_price(controller, broker, 100.1)
    controller._refresh_broker_connectivity_snapshot(detect_transition=False)

    assert controller._upstream_recovery_pending is False
    assert controller._api_data_invalidated is False
    assert controller._order_submission_connectivity_message("BUY") is None


def test_local_socket_loss_takes_precedence_over_upstream_state(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = _waiting_controller(controller_module, tmp_path / "local_loss.sqlite", broker)
    broker.disconnect()

    controller._tick()

    assert controller.connected is False
    assert controller._order_submission_connectivity_message("BUY")
    assert broker.placed_orders == []


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_connectivity_loss_at_exact_order_placement_boundary_fails_closed(
    controller_module: Any,
    tmp_path: Path,
    side: str,
) -> None:
    broker = _ConnectivityRaceAdapter()
    controller = _waiting_controller(controller_module, tmp_path / f"race_{side}.sqlite", broker)
    cycle = controller.active_cycle
    assert cycle is not None

    if side == "BUY":
        trigger = float(cycle.drop_trigger_price) * 0.999
        prepared, actions = StrategyEngine.on_price_update(cycle, trigger, is_rth=True)
        expected_stage = Stage.WAIT_INITIAL_DROP
    else:
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.quantity = 10
        cycle.buy_filled_qty = 10
        cycle.avg_buy_price = 98.0
        cycle.rise_trigger_price = 101.0
        controller.storage.upsert_cycle(cycle)
        prepared = cycle
        actions = [
            StrategyAction(
                "PLACE_SELL_MARKET",
                {
                    "ticker": cycle.ticker,
                    "quantity": 10,
                    "order_ref": f"IBKRBOT|{cycle.ticker}|RACE|SELL_MKT",
                },
            )
        ]
        expected_stage = Stage.WAIT_RISE_TRIGGER

    controller.active_cycle = prepared
    controller.storage.upsert_cycle(prepared)
    controller._execute_actions(actions, prepared)

    assert broker.placed_orders == []
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == expected_stage
    assert "connected" in controller.active_cycle.error_message.lower()
    audit = controller.storage.get_cycle_audit_bundle(controller.active_cycle.id)
    assert audit["orders"]
    assert audit["orders"][-1]["status"] == "SUBMIT_FAILED"


@pytest.mark.parametrize(
    "sequence",
    [
        [False],
        [True],
        [False, True],
        [True, False],
        [False, False, True],
        [True, True, False],
    ],
)
def test_generated_restore_sequences_end_only_after_fresh_event(
    controller_module: Any,
    tmp_path: Path,
    sequence: list[bool],
) -> None:
    broker = DeterministicBrokerAdapter()
    controller = _waiting_controller(controller_module, tmp_path / f"sequence_{sequence}.sqlite", broker)

    for index, data_lost in enumerate(sequence):
        broker.upstream_lost()
        controller._tick()
        broker.upstream_restored(data_lost=data_lost)
        controller._last_upstream_recovery_attempt_monotonic = 0.0
        controller._tick()
        assert controller._api_data_invalidated is True
        assert controller.active_cycle is not None
        assert controller._stale_data_guard_message_for_buy(controller.active_cycle)
        publish_fresh_price(controller, broker, 101.0 + index)
        controller._refresh_broker_connectivity_snapshot(detect_transition=False)
        assert controller._order_submission_connectivity_message("BUY") is None

    assert controller._recovery_required is False
    assert controller._upstream_recovery_pending is False
    assert controller._api_data_invalidated is False
