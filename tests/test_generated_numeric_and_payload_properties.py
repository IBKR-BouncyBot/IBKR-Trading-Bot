"""Deterministic property and metamorphic tests for numeric and payload safety."""

from __future__ import annotations

import math
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import IbAsyncTwsAdapter
from app.models import (
    CycleState,
    Stage,
    StrategySettings,
    effective_buy_sizing_price,
    minimum_sell_stop_price_for_profit,
    required_market_rise_pct_for_min_profit,
    required_sell_trigger_price_for_min_profit,
)
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _prepared_buy(settings: StrategySettings, anchor: float) -> tuple[Any, Any]:
    cycle = StrategyEngine.start_cycle(settings, 1, "", anchor, 0.0)
    trigger = round(float(cycle.anchor_price) * (1.0 - float(cycle.initial_drop_pct) / 100.0), 4)
    price = max(trigger * 0.999, trigger - 0.0001)
    prepared, actions = StrategyEngine.on_price_update(cycle, price, is_rth=True)
    assert prepared.stage == Stage.BUY_TRAIL_ACTIVE
    assert len(actions) == 1
    return prepared, actions[0]


def test_buy_quantity_is_monotonic_in_budget_and_slippage_buffer() -> None:
    rng = random.Random(41_001)
    for _ in range(1_000):
        anchor = rng.uniform(0.5, 2_000.0)
        budget = rng.uniform(anchor, 2_000_000.0)
        settings = permissive_strategy()
        settings.investment_amount = budget
        settings.initial_drop_pct = rng.uniform(0.01, 20.0)
        settings.buy_rebound_trail_pct = rng.uniform(0.0, 10.0)
        settings.slippage_buffer_enabled = False

        plain_cycle, plain_action = _prepared_buy(settings, anchor)
        buffered = StrategySettings(**{field: getattr(settings, field) for field in StrategySettings.__dataclass_fields__})
        buffered.slippage_buffer_enabled = True
        buffered.slippage_buffer_pct = rng.uniform(0.01, 5.0)
        buffered_cycle, buffered_action = _prepared_buy(buffered, anchor)

        assert plain_cycle.quantity >= buffered_cycle.quantity >= 0
        assert float(plain_action.payload["sizing_price"]) <= float(buffered_action.payload["sizing_price"])
        assert plain_cycle.quantity * float(plain_action.payload["sizing_price"]) <= plain_cycle.budget + 1e-7
        assert buffered_cycle.quantity * float(buffered_action.payload["sizing_price"]) <= buffered_cycle.budget + 1e-7

        richer = StrategySettings(**{field: getattr(buffered, field) for field in StrategySettings.__dataclass_fields__})
        richer.investment_amount = budget * rng.uniform(1.01, 4.0)
        richer_cycle, _ = _prepared_buy(richer, anchor)
        assert richer_cycle.quantity >= buffered_cycle.quantity


def test_effective_buy_sizing_ratio_is_scale_invariant() -> None:
    rng = random.Random(41_002)
    for _ in range(1_000):
        price = rng.uniform(0.01, 100_000.0)
        budget = rng.uniform(price, 10_000_000.0)
        slippage_pct = rng.uniform(0.0, 10.0)
        scale = rng.choice([0.01, 0.1, 10.0, 100.0])

        base_sizing = effective_buy_sizing_price(price, True, slippage_pct)
        scaled_sizing = effective_buy_sizing_price(price * scale, True, slippage_pct)

        assert scaled_sizing == pytest.approx(base_sizing * scale)
        assert math.floor(budget / base_sizing) == math.floor((budget * scale) / scaled_sizing)


def test_profit_trigger_is_monotonic_in_minimum_profit_trail_and_slippage() -> None:
    rng = random.Random(41_003)
    for _ in range(1_000):
        buy = rng.uniform(0.1, 10_000.0)
        minimum = rng.uniform(0.01, 30.0)
        trail = rng.uniform(0.0, 30.0)
        higher_minimum = min(99.0, minimum + rng.uniform(0.001, 10.0))
        higher_trail = min(98.0, trail + rng.uniform(0.001, 10.0))

        base = required_sell_trigger_price_for_min_profit(buy, minimum, trail)
        by_profit = required_sell_trigger_price_for_min_profit(buy, higher_minimum, trail)
        by_trail = required_sell_trigger_price_for_min_profit(buy, minimum, higher_trail)
        buffered = required_sell_trigger_price_for_min_profit(buy, minimum, trail, True, rng.uniform(0.01, 5.0))

        assert base >= buy
        assert by_profit + 1e-9 >= base
        assert by_trail + 1e-9 >= base
        assert buffered + 1e-9 >= base
        assert required_market_rise_pct_for_min_profit(higher_minimum, trail) >= required_market_rise_pct_for_min_profit(minimum, trail)
        assert required_market_rise_pct_for_min_profit(minimum, higher_trail) >= required_market_rise_pct_for_min_profit(minimum, trail)


def test_nonfinite_and_malformed_planning_inputs_fail_closed() -> None:
    invalid = [None, "", "not-a-number", float("nan"), float("inf"), float("-inf"), -1.0, 0.0]
    for value in invalid:
        assert effective_buy_sizing_price(value) == 0.0
        assert minimum_sell_stop_price_for_profit(value, None, 3.0) == 0.0

    assert math.isinf(required_sell_trigger_price_for_min_profit(100.0, 3.0, 100.0))
    assert required_market_rise_pct_for_min_profit(3.0, 100.0) == 999_999.0


@pytest.mark.parametrize("seed", list(range(20)))
def test_cycle_serialization_round_trip_preserves_trading_state(seed: int) -> None:
    rng = random.Random(41_100 + seed)
    settings = permissive_strategy()
    settings.investment_amount = rng.uniform(500.0, 50_000.0)
    settings.initial_drop_pct = rng.uniform(0.1, 10.0)
    settings.buy_rebound_trail_pct = rng.uniform(0.0, 5.0)
    settings.rise_trigger_pct = rng.uniform(0.1, 10.0)
    settings.sell_trailing_stop_pct = rng.uniform(0.0, 5.0)
    settings.slippage_buffer_enabled = rng.choice([False, True])
    settings.slippage_buffer_pct = rng.uniform(0.01, 2.0)
    cycle = StrategyEngine.start_cycle(settings, seed + 1, "", rng.uniform(5.0, 500.0), rng.uniform(-100.0, 500.0))
    cycle.stage = rng.choice(list(Stage))
    cycle.quantity = rng.randint(0, 1_000)
    cycle.buy_filled_qty = rng.randint(0, cycle.quantity) if cycle.quantity else 0
    cycle.sell_filled_qty = rng.randint(0, cycle.buy_filled_qty) if cycle.buy_filled_qty else 0
    cycle.avg_buy_price = rng.uniform(1.0, 1_000.0) if cycle.buy_filled_qty else None
    cycle.avg_sell_price = rng.uniform(1.0, 1_000.0) if cycle.sell_filled_qty else None
    cycle.error_message = "generated" if rng.choice([False, True]) else None

    payload = cycle.to_dict()
    payload["unknown_future_field"] = {"ignored": True}
    restored = CycleState.from_dict(payload)

    assert restored.to_dict() == cycle.to_dict()
    assert restored is not cycle


def test_execution_normalizer_rejects_malformed_payloads_and_accepts_valid_shape() -> None:
    adapter = IbAsyncTwsAdapter()
    malformed = [
        object(),
        SimpleNamespace(execution=None),
        SimpleNamespace(execution=SimpleNamespace(shares=0, price=100)),
        SimpleNamespace(execution=SimpleNamespace(shares=1, price=0)),
        SimpleNamespace(execution=SimpleNamespace(shares="bad", price=100)),
        SimpleNamespace(execution=SimpleNamespace(shares=1, price="bad")),
    ]
    for value in malformed:
        assert adapter._execution_dict_from_fill(value) is None

    valid = SimpleNamespace(
        contract=SimpleNamespace(symbol="aapl", conId=123, secType="STK", currency="USD"),
        order=SimpleNamespace(orderRef="IBKRBOT|AAPL|BUY"),
        execution=SimpleNamespace(
            orderRef="IBKRBOT|AAPL|BUY",
            orderId=1,
            permId=2,
            side="BOT",
            shares="3",
            price="99.5",
            avgPrice=99.5,
            execId="EXEC-1",
            time="2026-07-10T14:30:00+00:00",
            acctNumber="",
            exchange="NASDAQ",
        ),
        commissionReport=SimpleNamespace(commission="0.35", currency="USD"),
    )
    normalized = adapter._execution_dict_from_fill(valid)
    assert normalized is not None
    assert normalized["ticker"] == "AAPL"
    assert normalized["shares"] == pytest.approx(3.0)
    assert normalized["price"] == pytest.approx(99.5)
    assert normalized["commission"] == pytest.approx(0.35)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_generated_trailing_payloads_respect_tick_and_visible_market_reference(
    controller_module: Any,
    tmp_path: Path,
    side: str,
) -> None:
    rng = random.Random(41_200 if side == "BUY" else 41_201)
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy()
    controller = make_controller(controller_module, tmp_path / f"payload_{side}.sqlite", broker, settings)
    for index in range(200):
        broker.contract.min_tick = rng.choice([0.0001, 0.001, 0.005, 0.01, 0.05, 0.1])
        settings.slippage_buffer_enabled = rng.choice([False, True])
        settings.slippage_buffer_pct = rng.uniform(0.01, 2.0)
        market = rng.uniform(1.0, 2_000.0)
        publish_fresh_price(controller, broker, market)
        cycle = StrategyEngine.start_cycle(settings, 1, "", market, 0.0)
        trail = rng.uniform(0.01, 10.0)
        raw_stop = market * rng.uniform(0.75, 1.25)
        payload = {
            "quantity": 100,
            "trailing_percent": trail,
            "initial_stop_price": raw_stop,
            "order_ref": f"IBKRBOT|AAPL|{index}|{side}",
        }
        if side == "SELL":
            cycle.buy_filled_qty = 100
            cycle.avg_buy_price = market * 0.5
            cycle.rise_trigger_pct = 0.05

        normalized, error = controller._normalize_trailing_order_payload(cycle, payload, side)
        stop = float(normalized["initial_stop_price"])
        units = stop / broker.contract.min_tick
        assert units == pytest.approx(round(units), abs=1e-7)
        if side == "BUY":
            visible = max(float(controller.price_snapshot["price"]), float(controller.price_snapshot["fields"]["ask"]))
            assert stop + 1e-9 >= visible * (1.0 + trail / 100.0)
            assert int(normalized["quantity"]) >= 0
            assert int(normalized["quantity"]) * float(normalized["sizing_price"]) <= cycle.budget + 1e-7
        else:
            visible = min(float(controller.price_snapshot["price"]), float(controller.price_snapshot["fields"]["bid"]))
            assert stop <= visible * (1.0 - trail / 100.0) + broker.contract.min_tick + 1e-9
            assert error is None or "minimum-profit stop" in error or "Normalized SELL" in error
