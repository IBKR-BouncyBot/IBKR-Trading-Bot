from app.models import ConnectionSettings, Stage, StrategySettings, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


def _controller(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.connection = ConnectionSettings(trading_mode="live", market_data_type=0)
    return controller


def _cycle(**overrides):
    settings_values = dict(
        ticker="AAPL",
        investment_amount=1000.0,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        protective_sell_enabled=False,
        slippage_buffer_enabled=False,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        hard_risk_limits_enabled=True,
        max_daily_loss_ticker=0.0,
        max_daily_loss_total=0.0,
        max_cycles_per_ticker_day=0,
        max_consecutive_losses=0,
        max_spread_pct=0.0,
        min_trade_price=0.0,
        max_gap_from_prev_close_pct=0.0,
        block_delayed_data_in_live=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        what_if_check_enabled=False,
    )
    settings_values.update(overrides)
    settings = StrategySettings(**settings_values)
    cycle = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    cycle.last_price = 95.0
    return cycle


def test_risk_guard_blocks_wide_spread(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    controller.price_snapshot = {"fields": {"bid": 100.0, "ask": 102.0}}
    cycle = _cycle(max_spread_pct=1.0)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0})

    assert message is not None
    assert "spread" in message.lower()


def test_risk_guard_allows_spread_inside_limit(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    controller.price_snapshot = {"fields": {"bid": 100.0, "ask": 100.2}}
    cycle = _cycle(max_spread_pct=1.0)

    assert controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0}) is None


def test_risk_guard_blocks_ticker_daily_loss(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    day = utc_now_iso()[:10]
    losing = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL", investment_amount=1000), 99, "SIM", 100.0, 0.0)
    losing.stage = Stage.CYCLE_COMPLETE
    losing.net_pnl = -125.0
    losing.updated_at = f"{day}T12:00:00+00:00"
    controller.storage.upsert_cycle(losing)
    cycle = _cycle(max_daily_loss_ticker=100.0)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0})

    assert message is not None
    assert "daily app p/l" in message.lower()


def test_risk_guard_max_cycles_counts_total_not_only_today(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    old_complete = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL", investment_amount=1000), 7, "SIM", 100.0, 0.0)
    old_complete.stage = Stage.CYCLE_COMPLETE
    old_complete.updated_at = "2026-01-01T12:00:00+00:00"
    controller.storage.upsert_cycle(old_complete)
    cycle = _cycle(max_cycles_per_ticker_day=1)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0})

    assert message is not None
    assert "completed cycles in total" in message.lower()
    assert "today" not in message.lower()


def test_auto_repeat_stops_when_total_max_cycles_reached(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    settings = StrategySettings(
        ticker="AAPL",
        investment_amount=1000.0,
        hard_risk_limits_enabled=True,
        max_cycles_per_ticker_day=1,
        auto_repeat=True,
    )
    complete = StrategyEngine.start_cycle(settings, 1, "SIM", 100.0, 0.0)
    complete.stage = Stage.CYCLE_COMPLETE
    complete.net_pnl = 10.0
    controller.strategy = settings
    controller.active_cycle = complete
    controller.storage.upsert_cycle(complete)

    controller._maybe_start_next_cycle()

    assert controller.active_cycle is complete
    assert controller.storage.get_next_cycle_number("AAPL") == 2
    assert any("Max cycles reached (1/1)" in row["message"] for row in controller.storage.get_recent_events(20))


def test_risk_guard_blocks_buy_until_atr_ready_when_enabled(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    controller.price_snapshot = {
        "fields": {"bid": 100.0, "ask": 100.1},
        "atr_ready": False,
        "atr": {"ready": False, "bars_available": 3, "bars_required": 15, "reason": "waiting for enough RTH bars"},
    }
    cycle = _cycle(atr_adaptive_enabled=True, atr_block_new_buy_until_ready=True, hard_risk_limits_enabled=False)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0})

    assert message is not None
    assert "atr warmup" in message.lower()
    assert "3/15" in message


def test_risk_guard_blocks_delayed_data_in_live_profile(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    controller.connection.trading_mode = "live"
    controller.price_snapshot = {"selected_market_data_type": 3, "subscription_market_data_type": 3, "fields": {"bid": 100.0, "ask": 100.1}}
    cycle = _cycle(block_delayed_data_in_live=True)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 100.0})

    assert message is not None
    assert "non-live market data" in message.lower()


def test_risk_guard_blocks_gap_from_previous_close(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    controller.price_snapshot = {"fields": {"close": 100.0, "marketPrice": 112.0}}
    cycle = _cycle(max_gap_from_prev_close_pct=5.0)

    message = controller._risk_guard_message_for_buy(cycle, {"sizing_price": 112.0})

    assert message is not None
    assert "gap from close" in message.lower()
