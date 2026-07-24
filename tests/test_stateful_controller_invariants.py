"""Generated state-machine tests for controller, strategy, storage, and broker.

The scenarios are deterministic and seed-reproducible.  They exercise complete
order lifecycles, partial fills, zero-trail market orders, connectivity gates,
external positions, and cycle-limit behavior without a GUI or network session.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from app.models import Stage, StrategySettings
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _generated_settings(rng: random.Random) -> StrategySettings:
    settings = permissive_strategy()
    settings.investment_amount = rng.uniform(500.0, 25_000.0)
    settings.initial_drop_pct = rng.uniform(0.25, 8.0)
    settings.buy_rebound_trail_pct = rng.choice([0.0, rng.uniform(0.05, 3.0)])
    settings.rise_trigger_pct = rng.uniform(0.25, 8.0)
    settings.sell_trailing_stop_pct = rng.choice([0.0, rng.uniform(0.05, 3.0)])
    settings.slippage_buffer_enabled = rng.choice([False, True])
    settings.slippage_buffer_pct = rng.uniform(0.01, 1.5)
    return settings


def _configure_cycle(controller: Any, settings: StrategySettings, anchor: float, cycle_number: int = 1) -> Any:
    cycle = StrategyEngine.start_cycle(settings, cycle_number, "", anchor, 0.0)
    controller.strategy = settings
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return cycle


def _submit_initial_buy(controller: Any, broker: DeterministicBrokerAdapter, cycle: Any, drop_price: float) -> Any:
    publish_fresh_price(controller, broker, drop_price)
    prepared, actions = StrategyEngine.on_price_update(cycle, drop_price, is_rth=True)
    assert prepared.stage == Stage.BUY_TRAIL_ACTIVE
    assert len(actions) == 1
    controller.active_cycle = prepared
    controller.storage.upsert_cycle(prepared)
    controller._execute_actions(actions, prepared)
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    return controller.active_cycle


def _submit_final_sell(controller: Any, broker: DeterministicBrokerAdapter, cycle: Any) -> Any:
    assert cycle.rise_trigger_price is not None
    trigger_price = float(cycle.rise_trigger_price) * 1.001
    publish_fresh_price(controller, broker, trigger_price)
    prepared, actions = StrategyEngine.on_price_update(cycle, trigger_price, is_rth=True)
    assert prepared.stage == Stage.SELL_TRAIL_ACTIVE
    assert len(actions) == 1
    controller.active_cycle = prepared
    controller.storage.upsert_cycle(prepared)
    controller._execute_actions(actions, prepared)
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    return controller.active_cycle


@pytest.mark.parametrize("seed", list(range(24)))
def test_generated_full_cycle_preserves_cross_module_invariants(
    controller_module: Any,
    tmp_path: Path,
    seed: int,
) -> None:
    rng = random.Random(31_000 + seed)
    broker = DeterministicBrokerAdapter()
    settings = _generated_settings(rng)
    controller = make_controller(controller_module, tmp_path / f"state_{seed}.sqlite", broker, settings)
    controller.storage.backup_database = lambda *args, **kwargs: None

    anchor = rng.uniform(8.0, 500.0)
    publish_fresh_price(controller, broker, anchor)
    cycle = _configure_cycle(controller, settings, anchor)

    # An unrelated account-wide position must not be treated as app-owned.
    broker.external_position = rng.uniform(1.0, 10_000.0)
    assert controller._app_owned_position_blocker_for_buy(cycle) is None

    drop_price = float(cycle.drop_trigger_price) * rng.uniform(0.97, 0.9999)
    buy_cycle = _submit_initial_buy(controller, broker, cycle, drop_price)
    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0]["action"] == "BUY"
    assert broker.placed_orders[0]["quantity"] == buy_cycle.quantity

    requested_quantity = int(buy_cycle.quantity)
    assert requested_quantity > 0
    partial = rng.choice([False, True]) and requested_quantity > 1
    filled_quantity = rng.randint(1, requested_quantity - 1) if partial else requested_quantity
    buy_price = drop_price * rng.uniform(1.0, 1.02)
    buy_state = broker.fill_order(
        str(buy_cycle.buy_order_ref),
        shares=filled_quantity,
        price=buy_price,
        commission=rng.uniform(0.0, 2.0),
        execution_id=f"BUY-{seed}",
        terminal=not partial,
    )
    controller._handle_buy_order_poll(buy_cycle, buy_state)
    if partial:
        terminal_buy = broker.poll_order(str(buy_cycle.buy_order_ref))
        assert terminal_buy is not None and terminal_buy.status == "Cancelled"
        controller._handle_buy_order_poll(controller.active_cycle, terminal_buy)

    position_cycle = controller.active_cycle
    assert position_cycle is not None
    assert position_cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert position_cycle.buy_filled_qty == filled_quantity
    assert controller.storage.get_app_owned_unsold_position("AAPL")["quantity"] == pytest.approx(float(filled_quantity))
    if partial:
        assert str(buy_cycle.buy_order_ref) in broker.cancelled_orders

    sell_cycle = _submit_final_sell(controller, broker, position_cycle)
    assert broker.placed_orders[-1]["action"] == "SELL"
    assert broker.placed_orders[-1]["quantity"] == filled_quantity
    assert broker.placed_orders[-1]["quantity"] <= position_cycle.buy_filled_qty

    sell_price = max(float(position_cycle.rise_trigger_price), buy_price) * rng.uniform(1.0, 1.02)
    sell_state = broker.fill_order(
        str(sell_cycle.sell_order_ref),
        shares=filled_quantity,
        price=sell_price,
        commission=rng.uniform(0.0, 2.0),
        execution_id=f"SELL-{seed}",
        terminal=True,
    )
    controller._handle_sell_order_poll(sell_cycle, sell_state)

    completed = controller.active_cycle
    assert completed is not None
    assert completed.stage == Stage.CYCLE_COMPLETE
    assert completed.sell_filled_qty == filled_quantity
    assert completed.sell_filled_qty <= completed.buy_filled_qty
    assert controller.storage.get_app_owned_unsold_position("AAPL")["quantity"] == pytest.approx(0.0)
    assert broker.open_app_orders() == []
    assert broker.external_position > 0

    persisted = controller.storage.get_cycle(completed.id)
    assert persisted is not None
    assert persisted.stage == Stage.CYCLE_COMPLETE
    assert persisted.net_pnl == pytest.approx(completed.net_pnl)
    assert len(controller.storage.get_cycle_audit_bundle(completed.id)["executions"]) == 2


@pytest.mark.parametrize("data_lost", [False, True])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_order_submission_boundary_fails_closed_during_upstream_outage_and_can_retry(
    controller_module: Any,
    tmp_path: Path,
    data_lost: bool,
    side: str,
) -> None:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy()
    controller = make_controller(controller_module, tmp_path / f"outage_{side}_{data_lost}.sqlite", broker, settings)
    controller.storage.backup_database = lambda *args, **kwargs: None
    publish_fresh_price(controller, broker, 100.0)
    cycle = _configure_cycle(controller, settings, 100.0)

    if side == "BUY":
        price = float(cycle.drop_trigger_price) * 0.999
        prepared, actions = StrategyEngine.on_price_update(cycle, price, is_rth=True)
    else:
        cycle.stage = Stage.WAIT_RISE_TRIGGER
        cycle.quantity = 10
        cycle.buy_filled_qty = 10
        cycle.avg_buy_price = 98.0
        cycle.rise_trigger_price = 101.0
        controller.storage.upsert_cycle(cycle)
        prepared, actions = StrategyEngine.on_price_update(cycle, 102.0, is_rth=True)
    assert actions

    broker.upstream_lost()
    controller._drain_broker_events()
    controller.active_cycle = prepared
    controller.storage.upsert_cycle(prepared)
    controller._execute_actions(actions, prepared)

    assert broker.placed_orders == []
    assert controller.active_cycle is not None
    expected_stage = Stage.WAIT_INITIAL_DROP if side == "BUY" else Stage.WAIT_RISE_TRIGGER
    assert controller.active_cycle.stage == expected_stage
    assert "connect" in controller.active_cycle.error_message.lower()

    broker.upstream_restored(data_lost=data_lost)
    controller._drain_broker_events()
    controller._upstream_recovery_pending = False
    controller._recovery_required = False
    controller._broker_connectivity = broker.connectivity_status().to_dict()
    publish_fresh_price(controller, broker, 100.0 if side == "BUY" else 102.0)

    retry_cycle = controller.active_cycle
    assert retry_cycle is not None
    if side == "BUY":
        retry_cycle.anchor_price = 100.0
        retry_cycle.drop_trigger_price = 98.0
        retry_cycle.last_price = 97.9
        retry_cycle.error_message = ""
        retry_cycle, retry_actions = StrategyEngine.on_price_update(retry_cycle, 97.9, is_rth=True)
    else:
        retry_cycle.rise_trigger_price = 101.0
        retry_cycle.last_price = 102.0
        retry_cycle.error_message = ""
        retry_cycle, retry_actions = StrategyEngine.on_price_update(retry_cycle, 102.0, is_rth=True)
    controller.active_cycle = retry_cycle
    controller.storage.upsert_cycle(retry_cycle)
    controller._execute_actions(retry_actions, retry_cycle)

    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0]["action"] == side


def test_max_cycle_limit_stops_repeat_without_creating_a_new_cycle(controller_module: Any, tmp_path: Path) -> None:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy(auto_repeat=True)
    settings.hard_risk_limits_enabled = True
    settings.max_cycles_per_ticker_day = 1
    controller = make_controller(controller_module, tmp_path / "max_cycles.sqlite", broker, settings)
    controller.storage.backup_database = lambda *args, **kwargs: None

    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.sell_filled_qty = 10
    cycle.avg_buy_price = 98.0
    cycle.avg_sell_price = 102.0
    cycle.net_pnl = 40.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._maybe_start_next_cycle()

    assert controller.active_cycle.id == cycle.id
    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller.storage.get_completed_cycle_count("AAPL") == 1
    assert controller.storage.get_next_cycle_number("AAPL") == 2
    assert broker.placed_orders == []
