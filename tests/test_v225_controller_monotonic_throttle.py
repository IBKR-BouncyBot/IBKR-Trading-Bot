import os

os.environ.setdefault("IBKR_BOT_HEADLESS_SIGNALS", "1")

from app import controller as controller_module
from app.controller import TradingController
from app.models import CycleState, StrategySettings


def test_price_warning_throttle_uses_monotonic_clock(monkeypatch):
    instance = object.__new__(TradingController)
    instance._last_price_warning_at = {}
    logged = []

    def fake_log(level, message, cycle=None):
        logged.append((level, message, cycle.ticker if cycle else None))

    instance._log = fake_log
    cycle = CycleState.new(StrategySettings(ticker="AAPL"), 1, "DU123", 100.0, 0.0)

    monotonic_values = iter([100.0, 131.0])
    wall_values = iter([1000.0, 900.0])
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(controller_module.time, "time", lambda: next(wall_values))

    instance._log_price_warning_throttled(cycle, "test warning", interval_seconds=30.0)
    instance._log_price_warning_throttled(cycle, "test warning", interval_seconds=30.0)

    assert [entry[1] for entry in logged] == ["test warning", "test warning"]
