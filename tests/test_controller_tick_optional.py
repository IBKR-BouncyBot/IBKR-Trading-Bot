from app.ib_adapter import MarketPriceSnapshot, PolledOrderState, QualifiedContract
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class FakeAdapter:
    def __init__(self):
        self.poll_count = 0

    def is_connected(self):
        return True

    def qualify_stock(self, ticker, exchange, currency, primary_exchange=""):
        return QualifiedContract(ticker=ticker, con_id=123, raw=object())

    def set_market_data_type(self, market_data_type):
        return None

    def last_price(self, contract, timeout=1.0):
        return None

    def price_snapshot(self, contract, timeout=1.0):
        return MarketPriceSnapshot(
            price=None,
            source="none",
            requested_market_data_type=3,
            subscription_market_data_type=3,
            fields={},
            timestamp="test",
            status="No usable price",
        )

    def poll_order(self, order_ref):
        self.poll_count += 1
        return PolledOrderState(
            order_ref=order_ref,
            order_id=10,
            perm_id=20,
            status="Submitted",
            filled=0,
            remaining=5,
            avg_fill_price=0.0,
            commission=0.0,
            executions=[],
            raw={},
        )


def test_active_buy_order_polling_continues_without_market_price(tmp_path, monkeypatch):
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    fake = FakeAdapter()
    controller.adapter = fake
    controller.connected = True

    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.buy_order_ref = "IBKRBOT|AAPL|CYCLE-000001|TEST|BUY_TRAIL"
    cycle.buy_order_id = 10
    controller.active_cycle = cycle
    storage.upsert_cycle(cycle)

    controller._tick()

    assert fake.poll_count == 1
    assert controller.active_cycle is not None
    assert controller.active_cycle.buy_status == "Submitted"
