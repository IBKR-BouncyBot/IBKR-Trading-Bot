from collections import deque
from dataclasses import fields

from app.models import CycleState, Stage, StrategySettings
from app.strategy import StrategyEngine

MUTABLE_TYPES = (dict, list, set, bytearray, deque)


def test_cycle_state_has_no_mutable_runtime_fields_for_shallow_copy_updates():
    cycle = CycleState.new(StrategySettings(ticker="AAPL"), cycle_number=1, account="DU123", last_price=100.0, reinvested_profit=0.0)

    mutable_fields = [field.name for field in fields(CycleState) if isinstance(getattr(cycle, field.name), MUTABLE_TYPES)]

    assert mutable_fields == []


def test_strategy_price_update_does_not_mutate_original_cycle():
    cycle = CycleState.new(StrategySettings(ticker="AAPL", initial_drop_pct=2.0, buy_rebound_trail_pct=1.0), 1, "DU123", 100.0, 0.0)
    original = cycle.to_dict()

    updated, actions = StrategyEngine.on_price_update(cycle, 98.0)

    assert cycle.to_dict() == original
    assert updated is not cycle
    assert updated.stage == Stage.BUY_TRAIL_ACTIVE
    assert [action.action_type for action in actions] == ["PLACE_BUY_TRAIL"]
