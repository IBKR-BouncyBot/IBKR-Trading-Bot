from __future__ import annotations

from app.ib_adapter import MarketPriceSnapshot, QualifiedContract
from app.models import StrategySettings, atr_from_price_history, strategy_with_atr_adaptive_percentages
from app.storage import BotStorage
from tests.test_controller_headless import _install_qt_stub


def test_atr_from_price_history_uses_fixed_ohlc_bars():
    points: list[tuple[float, float]] = []
    # Four completed 60-second bars; period 3 needs at least period+1 bars.
    for idx, base in enumerate([100.0, 101.0, 99.0, 102.0]):
        start = idx * 60.0
        points.extend([
            (start + 1, base),
            (start + 20, base + 1.0),
            (start + 40, base - 1.0),
            (start + 59, base + 0.5),
        ])

    result = atr_from_price_history(points, period=3, bar_seconds=60)

    assert result["ready"] is True
    assert result["bars_available"] == 4
    assert result["true_ranges_used"] == 3
    assert result["atr"] > 0
    assert result["atr_pct"] > 0


def test_strategy_with_atr_adaptive_percentages_rewrites_manual_fields():
    settings = StrategySettings(
        ticker="AAPL",
        atr_adaptive_enabled=True,
        atr_initial_drop_multiplier=1.50,
        atr_buy_rebound_multiplier=0.75,
        atr_minimum_profit_multiplier=1.00,
        atr_sell_trail_multiplier=0.50,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )

    updated, values = strategy_with_atr_adaptive_percentages(settings, atr_pct=2.0)

    assert values["initial_drop_pct"] == 3.00
    assert values["buy_rebound_trail_pct"] == 1.50
    assert values["rise_trigger_pct"] == 2.00
    assert values["sell_trailing_stop_pct"] == 1.00
    assert updated.initial_drop_pct == 3.00
    assert updated.buy_rebound_trail_pct == 1.50
    assert updated.rise_trigger_pct == 2.00
    assert updated.sell_trailing_stop_pct == 1.00


def test_controller_applies_atr_adaptive_percentages_from_api_price_buffer(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(
        ticker="AAPL",
        atr_adaptive_enabled=True,
        atr_period=3,
        atr_bar_seconds=5,
        atr_initial_drop_multiplier=1.0,
        atr_buy_rebound_multiplier=1.0,
        atr_minimum_profit_multiplier=1.0,
        atr_sell_trail_multiplier=1.0,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )
    controller.storage.save_strategy_settings(controller.strategy)
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")

    # One monotonic time per snapshot call; enough to create more than four bars.
    times = iter([idx * 5.0 for idx in range(20)])
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: next(times))
    prices = [100, 101, 99, 102, 101, 103, 100, 104, 102, 105]
    for price in prices:
        controller._record_price_snapshot(
            MarketPriceSnapshot(
                price=float(price),
                source="marketPrice",
                requested_market_data_type=1,
                subscription_market_data_type=1,
                fields={"marketPrice": float(price), "bid": float(price) - 0.01, "ask": float(price) + 0.01},
                timestamp="2026-01-01T14:30:00Z",
                status="OK",
            ),
            contract,
        )

    assert controller.price_snapshot is not None
    assert controller.price_snapshot["atr_ready"] is True
    assert controller.price_snapshot["atr_adaptive_applied"] is True
    assert controller.strategy.initial_drop_pct > 0
    assert controller.strategy.initial_drop_pct == controller.strategy.buy_rebound_trail_pct
    assert controller.strategy.rise_trigger_pct == controller.strategy.sell_trailing_stop_pct


def test_atr_adaptive_can_leave_minimum_profit_manual():
    settings = StrategySettings(
        ticker="AAPL",
        rise_trigger_pct=7.25,
        atr_adaptive_enabled=True,
        atr_adapt_minimum_profit_enabled=False,
        atr_initial_drop_multiplier=1.50,
        atr_buy_rebound_multiplier=0.75,
        atr_minimum_profit_multiplier=1.00,
        atr_sell_trail_multiplier=0.50,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )

    updated, values = strategy_with_atr_adaptive_percentages(settings, atr_pct=2.0)

    assert values["initial_drop_pct"] == 3.00
    assert values["buy_rebound_trail_pct"] == 1.50
    assert values["rise_trigger_pct"] == 7.25
    assert values["atr_adapt_minimum_profit_enabled"] is False
    assert updated.rise_trigger_pct == 7.25
    assert updated.sell_trailing_stop_pct == 1.00



def test_atr_adaptive_can_leave_protective_sell_manual_by_default():
    settings = StrategySettings(
        ticker="AAPL",
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=4.25,
        atr_adaptive_enabled=True,
        atr_adapt_protective_sell_enabled=False,
        atr_protective_sell_multiplier=3.00,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )

    updated, values = strategy_with_atr_adaptive_percentages(settings, atr_pct=2.0)

    assert values["protective_sell_trailing_stop_pct"] == 4.25
    assert values["atr_adapt_protective_sell_enabled"] is False
    assert updated.protective_sell_trailing_stop_pct == 4.25


def test_atr_adaptive_can_update_protective_sell_when_enabled():
    settings = StrategySettings(
        ticker="AAPL",
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=4.25,
        atr_adaptive_enabled=True,
        atr_adapt_protective_sell_enabled=True,
        atr_protective_sell_multiplier=2.50,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )

    updated, values = strategy_with_atr_adaptive_percentages(settings, atr_pct=2.0)

    assert values["protective_sell_trailing_stop_pct"] == 5.00
    assert values["atr_adapt_protective_sell_enabled"] is True
    assert updated.protective_sell_trailing_stop_pct == 5.00

def test_atr_adaptive_minimum_profit_toggle_persists_in_settings():
    settings = StrategySettings(ticker="AAPL", atr_adaptive_enabled=True, atr_adapt_minimum_profit_enabled=False)
    assert settings.atr_adapt_minimum_profit_enabled is False
    assert settings.validate() == []


def test_controller_does_not_update_atr_adaptive_percentages_outside_rth(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(
        ticker="AAPL",
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
        atr_adaptive_enabled=True,
        atr_period=3,
        atr_bar_seconds=5,
        atr_initial_drop_multiplier=1.0,
        atr_buy_rebound_multiplier=1.0,
        atr_minimum_profit_multiplier=1.0,
        atr_sell_trail_multiplier=1.0,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )
    controller.storage.save_strategy_settings(controller.strategy)
    controller._latest_rth_status = {"is_open": False, "message": "Closed", "checked_at": "2026-01-01T21:00:00+00:00"}
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")

    times = iter([idx * 5.0 for idx in range(20)])
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: next(times))
    for price in [100, 101, 99, 102, 101, 103, 100, 104, 102, 105]:
        controller._record_price_snapshot(
            MarketPriceSnapshot(
                price=float(price),
                source="marketPrice",
                requested_market_data_type=1,
                subscription_market_data_type=1,
                fields={"marketPrice": float(price), "bid": float(price) - 0.01, "ask": float(price) + 0.01},
                timestamp="2026-01-01T21:00:00Z",
                status="OK",
            ),
            contract,
        )

    assert list(controller._price_history) == []
    assert controller.price_snapshot is not None
    assert controller.price_snapshot["atr_ready"] is False
    assert controller.price_snapshot["atr_rth_open"] is False
    assert "outside RTH" in controller.price_snapshot["atr"]["reason"]
    assert controller.strategy.initial_drop_pct == 5.0
    assert controller.strategy.buy_rebound_trail_pct == 1.0
    assert controller.strategy.rise_trigger_pct == 2.0
    assert controller.strategy.sell_trailing_stop_pct == 1.0


def test_controller_resumes_atr_updates_when_rth_reopens(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(
        ticker="AAPL",
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=2.0,
        sell_trailing_stop_pct=1.0,
        atr_adaptive_enabled=True,
        atr_period=3,
        atr_bar_seconds=5,
        atr_initial_drop_multiplier=1.0,
        atr_buy_rebound_multiplier=1.0,
        atr_minimum_profit_multiplier=1.0,
        atr_sell_trail_multiplier=1.0,
        atr_min_pct=0.10,
        atr_max_pct=20.0,
    )
    controller.storage.save_strategy_settings(controller.strategy)
    controller._latest_rth_status = {"is_open": True, "message": "Open", "checked_at": "2026-01-01T14:30:00+00:00"}
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")

    times = iter([idx * 5.0 for idx in range(20)])
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: next(times))
    for price in [100, 101, 99, 102, 101, 103, 100, 104, 102, 105]:
        controller._record_price_snapshot(
            MarketPriceSnapshot(
                price=float(price),
                source="marketPrice",
                requested_market_data_type=1,
                subscription_market_data_type=1,
                fields={"marketPrice": float(price), "bid": float(price) - 0.01, "ask": float(price) + 0.01},
                timestamp="2026-01-01T14:30:00Z",
                status="OK",
            ),
            contract,
        )

    assert len(controller._price_history) == 10
    assert controller.price_snapshot is not None
    assert controller.price_snapshot["atr_ready"] is True
    assert controller.price_snapshot["atr_rth_open"] is True
    assert controller.price_snapshot["atr"]["rth_only"] is True
    assert controller.strategy.initial_drop_pct != 5.0
