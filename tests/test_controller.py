from pathlib import Path

from app.ib_adapter import MarketPriceSnapshot, QualifiedContract
from app.models import ConnectionSettings, Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class FakeAdapter:
    def __init__(self, price=None):
        self.price = price
        self.poll_count = 0
        self.market_data_type = None

    def connect(self, *args, **kwargs):
        return None

    def disconnect(self):
        return None

    def is_connected(self):
        return True

    def set_market_data_type(self, market_data_type: int):
        self.market_data_type = market_data_type

    def qualify_stock(self, ticker: str, exchange: str, currency: str, primary_exchange: str = "") :
        return QualifiedContract(ticker=ticker.upper(), con_id=123, raw=object())

    def last_price(self, contract, timeout=0.75):
        return self.price

    def price_snapshot(self, contract, timeout=0.75):
        return MarketPriceSnapshot(
            price=self.price,
            source="test",
            requested_market_data_type=self.market_data_type or 3,
            subscription_market_data_type=self.market_data_type or 3,
            fields={"last": self.price},
            timestamp="test",
            status="OK" if self.price is not None else "No usable price",
        )

    def poll_order(self, order_ref: str):
        self.poll_count += 1
        return None

    def open_app_orders(self):
        return []

    def cancel_order(self, *args, **kwargs):
        return None


def _controller_with_cycle(tmp_path: Path, monkeypatch, stage: Stage):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage)
    settings = StrategySettings(ticker="AAPL", investment_amount=1000)
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.stage = stage
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|abc|BUY_TRAIL"
    cycle.sell_order_ref = "IBKRBOT|AAPL|CYCLE-000001|abc|SELL_TRAIL"
    storage.upsert_cycle(cycle)
    controller.active_cycle = cycle
    controller.connected = True
    controller.connection = ConnectionSettings(market_data_type=3)
    return controller, cycle


def test_active_buy_order_poll_does_not_require_market_price(tmp_path: Path, monkeypatch):
    controller, _cycle = _controller_with_cycle(tmp_path, monkeypatch, Stage.BUY_TRAIL_ACTIVE)
    fake = FakeAdapter(price=None)
    controller.adapter = fake

    controller._tick()

    assert fake.poll_count == 1
    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE


def test_active_sell_stage_updates_live_price_when_available(tmp_path: Path, monkeypatch):
    controller, _cycle = _controller_with_cycle(tmp_path, monkeypatch, Stage.SELL_TRAIL_ACTIVE)
    fake = FakeAdapter(price=123.45)
    controller.adapter = fake

    controller._tick()

    assert fake.poll_count == 1
    assert controller.active_cycle is not None
    assert controller.active_cycle.last_price == 123.45
