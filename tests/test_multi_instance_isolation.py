"""Offline multi-instance isolation tests using one shared fake Gateway."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.ib_adapter import BrokerAdapterError, OrderHandle
from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


class _SharedGateway:
    def __init__(self) -> None:
        self.clients: dict[int, _GatewayClientAdapter] = {}
        self.transmissions: list[dict[str, Any]] = []

    def register(self, client_id: int, adapter: "_GatewayClientAdapter") -> None:
        if client_id in self.clients and self.clients[client_id] is not adapter:
            raise BrokerAdapterError(f"Client ID {client_id} is already connected.")
        self.clients[client_id] = adapter

    def unregister(self, client_id: int, adapter: "_GatewayClientAdapter") -> None:
        if self.clients.get(client_id) is adapter:
            del self.clients[client_id]


class _GatewayClientAdapter(DeterministicBrokerAdapter):
    def __init__(self, gateway: _SharedGateway, *, client_id: int, ticker: str, con_id: int) -> None:
        super().__init__(ticker=ticker, con_id=con_id)
        self.gateway = gateway
        self.client_id = client_id
        self.gateway.register(client_id, self)

    def connect(self, host: str, port: int, client_id: int, market_data_type: int = 1) -> None:
        self.gateway.register(client_id, self)
        self.client_id = client_id
        super().connect(host, port, client_id, market_data_type)

    def disconnect(self) -> None:
        self.gateway.unregister(self.client_id, self)
        super().disconnect()

    def _place(self, *, order_type: str, **kwargs: Any) -> OrderHandle:
        handle = super()._place(order_type=order_type, **kwargs)
        self.gateway.transmissions.append(
            {
                "client_id": self.client_id,
                "ticker": self.contract.ticker,
                "order_ref": handle.order_ref,
                "order_type": order_type,
            }
        )
        return handle


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _submit_buy(controller: Any, broker: DeterministicBrokerAdapter, anchor: float) -> Any:
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", anchor, 0.0)
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    publish_fresh_price(controller, broker, anchor)
    price = float(cycle.drop_trigger_price) * 0.999
    prepared, actions = StrategyEngine.on_price_update(cycle, price, is_rth=True)
    assert prepared.stage == Stage.BUY_TRAIL_ACTIVE
    controller.active_cycle = prepared
    controller.storage.upsert_cycle(prepared)
    controller._execute_actions(actions, prepared)
    return controller.active_cycle


def test_shared_gateway_accepts_unique_client_ids_and_rejects_collision() -> None:
    gateway = _SharedGateway()
    first = _GatewayClientAdapter(gateway, client_id=11, ticker="AAPL", con_id=1)
    second = _GatewayClientAdapter(gateway, client_id=12, ticker="MSFT", con_id=2)

    assert set(gateway.clients) == {11, 12}
    with pytest.raises(BrokerAdapterError, match="already connected"):
        _GatewayClientAdapter(gateway, client_id=11, ticker="NVDA", con_id=3)

    first.disconnect()
    assert set(gateway.clients) == {12}
    assert second.is_connected() is True


def test_two_instances_submit_only_to_their_own_client_and_database(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    gateway = _SharedGateway()
    broker_a = _GatewayClientAdapter(gateway, client_id=11, ticker="AAPL", con_id=1)
    broker_b = _GatewayClientAdapter(gateway, client_id=12, ticker="MSFT", con_id=2)
    controller_a = make_controller(
        controller_module,
        tmp_path / "instance_a" / "bot_state.sqlite",
        broker_a,
        permissive_strategy(ticker="AAPL"),
    )
    controller_b = make_controller(
        controller_module,
        tmp_path / "instance_b" / "bot_state.sqlite",
        broker_b,
        permissive_strategy(ticker="MSFT"),
    )
    controller_a.storage.backup_database = lambda *args, **kwargs: None
    controller_b.storage.backup_database = lambda *args, **kwargs: None

    cycle_a = _submit_buy(controller_a, broker_a, 100.0)
    cycle_b = _submit_buy(controller_b, broker_b, 250.0)

    assert [row["client_id"] for row in gateway.transmissions] == [11, 12]
    assert [row["ticker"] for row in gateway.transmissions] == ["AAPL", "MSFT"]
    assert cycle_a.buy_order_ref in broker_a.orders
    assert cycle_a.buy_order_ref not in broker_b.orders
    assert cycle_b.buy_order_ref in broker_b.orders
    assert cycle_b.buy_order_ref not in broker_a.orders
    assert controller_a.storage.get_cycle(cycle_b.id) is None
    assert controller_b.storage.get_cycle(cycle_a.id) is None


def test_external_master_style_event_is_audited_without_mutating_other_cycle(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    gateway = _SharedGateway()
    broker_a = _GatewayClientAdapter(gateway, client_id=11, ticker="AAPL", con_id=1)
    broker_b = _GatewayClientAdapter(gateway, client_id=12, ticker="MSFT", con_id=2)
    controller_a = make_controller(
        controller_module,
        tmp_path / "a" / "bot_state.sqlite",
        broker_a,
        permissive_strategy(ticker="AAPL"),
    )
    controller_b = make_controller(
        controller_module,
        tmp_path / "b" / "bot_state.sqlite",
        broker_b,
        permissive_strategy(ticker="MSFT"),
    )
    cycle_a = _submit_buy(controller_a, broker_a, 100.0)
    cycle_b = StrategyEngine.start_cycle(controller_b.strategy, 1, "", 250.0, 0.0)
    controller_b.active_cycle = cycle_b
    controller_b.storage.upsert_cycle(cycle_b)

    foreign_event = {
        "event_type": "OPEN_ORDER",
        "created_at": "2026-07-10T14:30:00+00:00",
        "ticker": "AAPL",
        "order_ref": cycle_a.buy_order_ref,
        "order_id": cycle_a.buy_order_id,
        "perm_id": cycle_a.buy_perm_id,
        "status": "Submitted",
    }
    broker_b.events.append(foreign_event)
    controller_b._drain_broker_events()

    persisted_b = controller_b.storage.get_cycle(cycle_b.id)
    assert persisted_b is not None
    assert persisted_b.stage == Stage.WAIT_INITIAL_DROP
    assert controller_b.storage.get_cycle(cycle_a.id) is None
    events = controller_b.storage.recent_broker_events(10)
    assert len(events) == 1
    assert events[0]["order_ref"] == cycle_a.buy_order_ref
    # The current controller associates unmatched IBKRBOT-prefixed events with
    # the active cycle for audit visibility.  The event still cannot create an
    # order record or change strategy state in the other installation.
    assert events[0]["cycle_id"] == cycle_b.id
    assert controller_b.storage.get_cycle_audit_bundle(cycle_b.id)["orders"] == []


@pytest.mark.xfail(
    strict=True,
    reason="Master-client feeds share the IBKRBOT prefix; installation-specific ownership is not encoded.",
)
def test_master_style_foreign_event_is_not_attributed_to_the_active_cycle(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    gateway = _SharedGateway()
    broker_a = _GatewayClientAdapter(gateway, client_id=11, ticker="AAPL", con_id=1)
    broker_b = _GatewayClientAdapter(gateway, client_id=12, ticker="MSFT", con_id=2)
    controller_a = make_controller(
        controller_module,
        tmp_path / "xfail_a" / "bot_state.sqlite",
        broker_a,
        permissive_strategy(ticker="AAPL"),
    )
    controller_b = make_controller(
        controller_module,
        tmp_path / "xfail_b" / "bot_state.sqlite",
        broker_b,
        permissive_strategy(ticker="MSFT"),
    )
    cycle_a = _submit_buy(controller_a, broker_a, 100.0)
    cycle_b = StrategyEngine.start_cycle(controller_b.strategy, 1, "", 250.0, 0.0)
    controller_b.active_cycle = cycle_b
    controller_b.storage.upsert_cycle(cycle_b)
    broker_b.events.append(
        {
            "event_type": "OPEN_ORDER",
            "created_at": "2026-07-10T14:30:00+00:00",
            "ticker": "AAPL",
            "order_ref": cycle_a.buy_order_ref,
            "status": "Submitted",
        }
    )

    controller_b._drain_broker_events()

    assert controller_b.storage.recent_broker_events(1)[0]["cycle_id"] is None


def test_reinvestment_and_runtime_artifacts_remain_directory_local(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    gateway = _SharedGateway()
    broker_a = _GatewayClientAdapter(gateway, client_id=11, ticker="AAPL", con_id=1)
    broker_b = _GatewayClientAdapter(gateway, client_id=12, ticker="AAPL", con_id=1)
    settings_a = permissive_strategy(ticker="AAPL")
    settings_b = permissive_strategy(ticker="AAPL")
    settings_a.reinvest_profits = True
    settings_b.reinvest_profits = True
    root_a = tmp_path / "copy_a"
    root_b = tmp_path / "copy_b"
    controller_a = make_controller(controller_module, root_a / "bot_state.sqlite", broker_a, settings_a)
    controller_b = make_controller(controller_module, root_b / "bot_state.sqlite", broker_b, settings_b)

    completed = StrategyEngine.start_cycle(settings_a, 1, "", 100.0, 0.0)
    completed.stage = Stage.CYCLE_COMPLETE
    completed.buy_filled_qty = 10
    completed.sell_filled_qty = 10
    completed.avg_buy_price = 100.0
    completed.avg_sell_price = 105.0
    completed.gross_pnl = 50.0
    completed.net_pnl = 48.0
    controller_a.storage.upsert_cycle(completed)

    assert controller_a.storage.get_realized_net_profit_for_ticker("AAPL") == pytest.approx(48.0)
    assert controller_b.storage.get_realized_net_profit_for_ticker("AAPL") == pytest.approx(0.0)

    controller_a.storage.add_event("INFO", "only A", ticker="AAPL")
    controller_b.storage.add_event("INFO", "only B", ticker="AAPL")
    backup_a = controller_a.storage.backup_database("instance_a", keep=2)
    backup_b = controller_b.storage.backup_database("instance_b", keep=2)

    assert backup_a is not None and backup_a.parent == root_a / "backups"
    assert backup_b is not None and backup_b.parent == root_b / "backups"
    assert "only A" in (root_a / "debug_reports" / "audit_events_readable.log").read_text()
    assert "only B" not in (root_a / "debug_reports" / "audit_events_readable.log").read_text()
    assert "only B" in (root_b / "debug_reports" / "audit_events_readable.log").read_text()
