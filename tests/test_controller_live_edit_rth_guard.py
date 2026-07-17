from pathlib import Path

from app.ib_adapter import QualifiedContract, RthStatus
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class ClosedRthAdapter:
    def __init__(self):
        self.place_calls = 0

    def qualify_stock(self, ticker, exchange, currency, primary_exchange="", con_id=None):
        return QualifiedContract(ticker=ticker.upper(), con_id=con_id or 123, raw=object())

    def regular_trading_hours_status(self, contract):
        return RthStatus(False, "test", "simulated RTH closed", "now")

    def place_trailing_stop(self, *args, **kwargs):
        self.place_calls += 1
        raise AssertionError("RTH-closed live edit should not submit an order")


def test_live_edit_re_evaluation_does_not_bypass_rth_guard(tmp_path: Path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage)
    controller.adapter = ClosedRthAdapter()
    controller.connected = True

    base = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=10.0,
        buy_rebound_trail_pct=2.0,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    cycle = StrategyEngine.start_cycle(base, 1, "", 100.0, 0.0)
    cycle.last_price = 96.0
    cycle.drop_trigger_price = 90.0
    cycle.stage = Stage.WAIT_INITIAL_DROP
    controller.active_cycle = cycle
    storage.upsert_cycle(cycle)

    # Editing initial_drop from 10% to 4% makes the stored last price equal the
    # drop trigger. The controller must still check RTH before order placement.
    edited = StrategySettings(
        ticker="AAPL",
        investment_amount=1000,
        initial_drop_pct=4.0,
        buy_rebound_trail_pct=2.0,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
    )
    controller._apply_active_strategy_edits(edited)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.WAIT_INITIAL_DROP
    assert controller.active_cycle.buy_order_ref is None
    assert controller.active_cycle.error_message is not None
    assert controller.active_cycle.error_message.startswith("RTH guard:")
    assert controller.adapter.place_calls == 0
