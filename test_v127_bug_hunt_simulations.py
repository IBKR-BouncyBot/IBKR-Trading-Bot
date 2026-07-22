import os
from pathlib import Path

from app.ib_adapter import MarketPriceSnapshot, QualifiedContract
from app.lockfile import SingleInstanceError, SingleInstanceLock
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from tests.simulated_strategy_runner import load_price_rows_csv, load_prices_csv, run_one_cycle
from tests.test_controller_headless import _install_qt_stub

DATA_DIR = Path(__file__).resolve().parent / "simulated_data"


def settings(**overrides) -> StrategySettings:
    values = dict(
        ticker="AAPL",
        investment_amount=10_000,
        initial_drop_pct=5.0,
        buy_rebound_trail_pct=2.0,
        rise_trigger_pct=10.0,
        sell_trailing_stop_pct=1.0,
        reinvest_profits=True,
        auto_repeat=True,
        protective_sell_enabled=False,
        protective_sell_trailing_stop_pct=3.0,
        slippage_buffer_enabled=False,
        slippage_buffer_pct=0.50,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
    )
    values.update(overrides)
    return StrategySettings(**values)


def test_csv_protective_sell_can_exit_before_minimum_profit():
    result = run_one_cycle(
        settings(protective_sell_enabled=True),
        load_prices_csv(DATA_DIR / "protective_sell_exits_before_profit.csv"),
    )

    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert "protective_sell_submitted" in result.event_kinds()
    assert "protective_sell_filled" in result.event_kinds()
    assert "sell_trail_submitted" not in result.event_kinds()
    assert result.cycle.protective_sell_filled_qty == result.cycle.buy_filled_qty


def test_csv_protective_sell_is_cancelled_before_final_profit_sell():
    result = run_one_cycle(
        settings(protective_sell_enabled=True),
        load_prices_csv(DATA_DIR / "protective_replaced_by_profit_sell.csv"),
    )

    kinds = result.event_kinds()
    assert result.cycle.stage == Stage.CYCLE_COMPLETE
    assert kinds.index("protective_sell_cancelled") < kinds.index("sell_trail_submitted")
    assert kinds[-1] == "sell_filled"
    assert result.cycle.net_pnl > 0


def test_csv_slippage_buffer_sizes_buy_from_buffered_stop_price():
    result = run_one_cycle(
        settings(investment_amount=1000, slippage_buffer_enabled=True, slippage_buffer_pct=5.0),
        load_prices_csv(DATA_DIR / "slippage_buffer_budget.csv"),
    )

    buy_action = next(event for event in result.events if event.kind == "buy_trail_submitted")
    sizing_price = float(buy_action.payload["sizing_price"])
    stop_price = float(buy_action.payload["initial_stop_price"])
    assert sizing_price > stop_price
    assert result.cycle.quantity == int(1000 // sizing_price)


def test_csv_rth_closed_ticks_do_not_submit_until_rth_reopens():
    result = run_one_cycle(
        settings(),
        load_price_rows_csv(DATA_DIR / "rth_reopens_after_drop.csv"),
    )

    first_buy = next(event for event in result.events if event.kind == "buy_trail_submitted")
    assert first_buy.price == 93.0
    assert result.cycle.stage == Stage.CYCLE_COMPLETE


def test_csv_no_sell_trigger_holds_position_without_final_order():
    result = run_one_cycle(settings(), load_prices_csv(DATA_DIR / "no_sell_trigger_holds_position.csv"))

    assert result.cycle.stage == Stage.WAIT_RISE_TRIGGER
    assert result.cycle.buy_filled_qty > 0
    assert "sell_trail_submitted" not in result.event_kinds()


def test_csv_anchor_resets_multiple_times_before_initial_drop():
    result = run_one_cycle(
        settings(rise_trigger_pct=3.0),
        load_prices_csv(DATA_DIR / "anchor_reset_multiple.csv"),
    )

    assert result.cycle.anchor_price == 108.0
    assert "buy_trail_submitted" in result.event_kinds()
    first_buy = next(event for event in result.events if event.kind == "buy_trail_submitted")
    assert first_buy.price == 102.60


def test_csv_long_flat_runtime_does_not_create_order_or_grow_unbounded():
    result = run_one_cycle(settings(), load_prices_csv(DATA_DIR / "long_flat_runtime.csv"))

    assert result.cycle.stage == Stage.WAIT_INITIAL_DROP
    assert result.event_kinds() == []
    assert result.cycle.anchor_price > 100.0



def test_controller_price_history_rolling_buffer_is_bounded(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = StrategySettings(ticker="AAPL", atr_adaptive_enabled=False)
    contract = QualifiedContract(ticker="AAPL", con_id=123, raw=object(), primary_exchange="NASDAQ")

    for idx in range(22_100):
        controller._record_price_snapshot(
            MarketPriceSnapshot(
                price=100.0 + (idx % 10) * 0.01,
                source="marketPrice",
                requested_market_data_type=1,
                subscription_market_data_type=1,
                fields={"marketPrice": 100.0 + (idx % 10) * 0.01, "bid": 99.9, "ask": 100.1},
                timestamp="2026-01-01T14:30:00Z",
                status="OK",
            ),
            contract,
        )

    assert len(controller._price_history) <= 21_600
    assert controller.price_snapshot["api_data_seen_count"] == 22_100



def test_warning_throttle_cache_is_bounded(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    active = None
    # Create a minimal active cycle through the strategy model to exercise the real method.
    from app.strategy import StrategyEngine
    active = StrategyEngine.start_cycle(settings(), 1, "DU1", 100.0, 0.0)
    controller.storage.upsert_cycle(active)
    for idx in range(900):
        active.stage = Stage.WAIT_INITIAL_DROP
        controller._log_price_warning_throttled(active, f"simulated warning {idx}", interval_seconds=0)
    assert len(controller._last_price_warning_at) <= 513


def test_backup_database_names_are_unique_under_rapid_calls(tmp_path):
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    paths = [storage.backup_database("rapid") for _ in range(5)]
    assert all(path is not None and path.exists() for path in paths)
    assert len({path.name for path in paths if path is not None}) == 5


def test_single_instance_lock_recovers_stale_lock_file(tmp_path):
    lock_path = tmp_path / "bot.lock"
    lock_path.write_text("999999999", encoding="ascii")
    lock = SingleInstanceLock(path=lock_path)
    lock.acquire()
    try:
        assert lock_path.exists()
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    finally:
        lock.release()


def test_single_instance_lock_rejects_live_lock(tmp_path):
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(path=lock_path)
    first.acquire()
    try:
        second = SingleInstanceLock(path=lock_path)
        try:
            second.acquire()
        except SingleInstanceError:
            pass
        else:
            raise AssertionError("second lock unexpectedly acquired")
    finally:
        first.release()
