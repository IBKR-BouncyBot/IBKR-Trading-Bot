"""Deterministic contract tests for every controller callable family.

The controller owns the highest-risk integration boundary in the application.
These tests use a temporary SQLite database and protocol-shaped broker doubles;
they never open a network socket, launch IBKR software, or submit a real order.
"""

from __future__ import annotations

import queue
import time
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from app.ib_adapter import BrokerConnectivityStatus, MarketPriceSnapshot, PolledOrderState, QualifiedContract
from app.ib_platform import PlatformLaunchResult
from app.models import ConnectionSettings, Stage, StopAction, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub


class ControllerAdapterStub:
    """Small observable broker boundary for controller unit tests."""

    def __init__(self) -> None:
        self.connected = True
        self.disconnected = False
        self.market_data_types: list[int] = []
        self.executions: list[dict[str, Any]] = []
        self.snapshot = MarketPriceSnapshot(
            price=100.0,
            source="last",
            requested_market_data_type=1,
            subscription_market_data_type=1,
            fields={"bid": 99.95, "ask": 100.05, "last": 100.0},
            timestamp="2026-07-11T12:00:00+00:00",
            status="OK",
            market_data_update_sequence=1,
            market_data_subscription_id="AAPL:123",
            market_data_event_tracking=True,
            market_data_event_tracking_available=True,
            upstream_connected=True,
        )
        self.status = BrokerConnectivityStatus(
            local_connected=True,
            upstream_connected=True,
            state="connected",
            message="ready",
            market_data_event_tracking=True,
        )

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True

    def connectivity_status(self) -> BrokerConnectivityStatus:
        if not self.connected:
            return BrokerConnectivityStatus(False, False, "local_disconnected", "disconnected")
        return self.status

    def set_market_data_type(self, market_data_type: int) -> None:
        self.market_data_types.append(int(market_data_type))

    def qualify_stock(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        primary_exchange: str = "",
        con_id: int | None = None,
    ) -> QualifiedContract:
        del exchange, currency
        return QualifiedContract(
            ticker=ticker,
            con_id=con_id or 123,
            raw=object(),
            primary_exchange=primary_exchange,
        )

    def regular_trading_hours_status(self, contract: QualifiedContract) -> Any:
        del contract
        return {
            "is_open": True,
            "source": "test",
            "message": "RTH open",
            "checked_at": "2026-07-11T12:00:00+00:00",
        }

    def price_snapshot(self, contract: QualifiedContract, timeout: float = 0.75) -> MarketPriceSnapshot:
        del contract, timeout
        return self.snapshot

    def recent_executions(self) -> list[dict[str, Any]]:
        return list(self.executions)


def _controller(controller_module: Any, tmp_path: Path, name: str = "bot.sqlite") -> Any:
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / name))
    controller.emit_snapshot = lambda *args, **kwargs: None
    return controller


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def test_headless_signal_descriptor_and_signal_namespace(controller_module: Any) -> None:
    class Owner:
        changed = controller_module.Signal(object)

    owner = Owner()
    seen: list[int] = []
    owner.changed.connect(seen.append)
    owner.changed.emit(7)

    assert Owner.changed.name == "changed"
    assert seen == [7]
    assert owner.changed.emissions == [((7,), {})]

    signals = controller_module.ControllerSignals()
    received: list[str] = []
    signals.event_logged.connect(received.append)
    signals.event_logged.emit("event")
    assert received == ["event"]


def test_public_command_api_queues_exact_commands_and_exports_local_data(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(controller_module, tmp_path)
    connection = ConnectionSettings(host="127.0.0.1", port=4001, client_id=17)
    strategy = StrategySettings(ticker="AAPL", investment_amount=1_000.0)

    assert controller.db_path == tmp_path / "bot.sqlite"
    controller.connect_tws(connection)
    controller.disconnect_tws()
    controller.start_ibkr_platform(connection)
    controller.start_strategy(connection, strategy)
    controller.request_stop(StopAction.STOP_NOW_NO_BROKER_ACTION)
    controller.refresh_history("AAPL")
    controller.refresh_broker_state()
    controller.resume_recovery_monitoring()
    controller.mark_recovery_manually_handled("verified in Gateway")
    controller.cancel_recovery_app_order()
    controller.save_draft_settings(connection, strategy)
    controller.search_tickers(connection, "AAPL")
    controller.search_contracts(connection, "MSFT")
    controller.confirm_ticker_price(connection, strategy)

    commands: list[tuple[str, dict[str, Any]]] = []
    while True:
        try:
            commands.append(controller._commands.get_nowait())
        except queue.Empty:
            break
    assert [name for name, _ in commands] == [
        "CONNECT",
        "DISCONNECT",
        "START_PLATFORM",
        "START_STRATEGY",
        "STOP_ACTION",
        "REFRESH_HISTORY",
        "REFRESH_BROKER_STATE",
        "RESUME_RECOVERY_MONITORING",
        "MARK_RECOVERY_MANUALLY_HANDLED",
        "CANCEL_RECOVERY_APP_ORDER",
        "SAVE_DRAFT_SETTINGS",
        "SEARCH_CONTRACTS",
        "SEARCH_CONTRACTS",
        "CONFIRM_TICKER_PRICE",
    ]
    assert commands[8][1]["note"] == "verified in Gateway"
    assert controller.get_cycle_audit_details("missing")["cycle"] is None

    export_root = tmp_path / "exports"
    monkeypatch.setattr(controller_module, "exports_dir", lambda: export_root)
    history_path = controller.export_history("AAPL")
    assert history_path.exists()
    assert history_path.read_text(encoding="utf-8").startswith("ticker,")

    bundle_path = controller.export_audit_bundle(target_dir=export_root)
    assert bundle_path.exists()
    assert bundle_path.suffix == ".zip"


def test_stale_cycle_refresh_and_upstream_ready_are_fail_closed(controller_module: Any, tmp_path: Path) -> None:
    controller = _controller(controller_module, tmp_path)
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.updated_at = "2000-01-01T00:00:00+00:00"
    controller.active_cycle = cycle
    controller._recovery_required = False
    controller._refresh_stale_active_cycle_flag()
    assert controller._stale_active_cycle_detected is True
    assert controller._recovery_required is True

    adapter = ControllerAdapterStub()
    controller.adapter = adapter
    controller.connected = True
    controller._broker_connectivity_initialized = True
    assert controller._upstream_trading_ready() is True
    adapter.status = BrokerConnectivityStatus(True, False, "upstream_lost", "offline", error_code=1100)
    assert controller._upstream_trading_ready() is False


def test_start_platform_and_disconnect_update_observable_state(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(controller_module, tmp_path)
    settings = ConnectionSettings(platform="gateway", platform_path="C:/IBKR/ibgateway.exe")
    monkeypatch.setattr(
        controller_module,
        "launch_platform",
        lambda platform, path: PlatformLaunchResult(True, path, f"Started {platform}"),
    )

    controller._start_ibkr_platform(settings)
    assert controller._auto_reconnect_enabled is True
    assert controller.status == "Started gateway"
    assert controller.signals.connection_changed.emissions[-1][0][0] is False

    adapter = ControllerAdapterStub()
    controller.adapter = adapter
    controller.connected = True
    controller._broker_display_accounts = ["DU123"]
    controller._disconnect()
    assert adapter.disconnected is True
    assert controller.connected is False
    assert controller.status == "Disconnected"
    assert controller._broker_display_accounts == []
    assert controller.price_snapshot is None or controller._api_data_invalidated is True


def test_confirm_ticker_price_records_usable_and_missing_price_paths(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(controller_module, tmp_path)
    adapter = ControllerAdapterStub()
    controller.adapter = adapter
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(controller, "_log", lambda level, message, cycle=None: messages.append((level, message)))
    monkeypatch.setattr(controller, "_update_rth_status", lambda contract: {"is_open": True})

    def record(snapshot: MarketPriceSnapshot, contract: QualifiedContract) -> None:
        del contract
        controller.price_snapshot = {"strategy_price_usable": snapshot.price is not None}

    monkeypatch.setattr(controller, "_record_price_snapshot", record)
    settings = StrategySettings(ticker="AAPL", contract_con_id=123)
    controller._confirm_ticker_price(settings)
    assert controller.contract is not None and controller.contract.con_id == 123
    assert messages[-1][0] == "INFO"
    assert adapter.market_data_types == [controller.connection.market_data_type]

    adapter.snapshot = MarketPriceSnapshot(
        price=None,
        source="none",
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields={},
        timestamp="2026-07-11T12:00:00+00:00",
    )
    controller._confirm_ticker_price(settings)
    assert messages[-1][0] == "WARN"

    with pytest.raises(ValueError, match="Ticker"):
        controller._confirm_ticker_price(StrategySettings(ticker=""))
    with pytest.raises(ValueError, match="conId"):
        controller._confirm_ticker_price(StrategySettings(ticker="AAPL", contract_con_id=0))


def _execution(
    *,
    execution_id: str,
    side: str,
    order_ref: str,
    order_id: int,
    perm_id: int,
    shares: float,
    price: float,
    commission: float = 0.0,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "side": side,
        "order_ref": order_ref,
        "order_id": order_id,
        "perm_id": perm_id,
        "shares": shares,
        "price": price,
        "avg_price": price,
        "commission": commission,
        "currency": "USD",
        "executed_at": "2026-07-11T12:00:00+00:00",
    }


def test_execution_identity_aggregation_and_recording(controller_module: Any, tmp_path: Path) -> None:
    controller = _controller(controller_module, tmp_path)
    adapter = ControllerAdapterStub()
    controller.adapter = adapter
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.buy_order_ref = "IBKRBOT|AAPL|BUY"
    cycle.buy_order_id = 101
    cycle.buy_perm_id = 202
    cycle.sell_order_ref = "IBKRBOT|AAPL|SELL"
    cycle.sell_order_id = 303
    cycle.sell_perm_id = 404
    cycle.protective_sell_order_ref = "IBKRBOT|AAPL|PROTECTIVE_SELL"
    cycle.protective_sell_order_id = 505
    cycle.protective_sell_perm_id = 606
    controller.storage.upsert_cycle(cycle)

    assert controller._order_identity_for_side(cycle, "buy") == (cycle.buy_order_ref, 101, 202)
    assert controller._order_identity_for_side(cycle, "protective_sell") == (
        cycle.protective_sell_order_ref,
        505,
        606,
    )
    assert controller._order_identity_for_side(cycle, "sell") == (cycle.sell_order_ref, 303, 404)

    first = _execution(
        execution_id="E1",
        side="BOT",
        order_ref=cycle.buy_order_ref,
        order_id=101,
        perm_id=202,
        shares=2,
        price=99.0,
        commission=0.5,
    )
    second = _execution(
        execution_id="E2",
        side="BUY",
        order_ref="other",
        order_id=999,
        perm_id=202,
        shares=3,
        price=101.0,
        commission=0.75,
    )
    wrong_side = dict(first, execution_id="E3", side="SLD")
    invalid = dict(first, execution_id="E4", shares="bad")
    adapter.executions = [first, second, wrong_side, invalid]

    assert controller._execution_matches_order(cycle, first, "BUY") is True
    assert controller._execution_matches_order(cycle, wrong_side, "BUY") is False
    assert controller._execution_matches_order(cycle, {"side": "BUY", "order_id": 101}, "BUY") is True
    assert controller._execution_matches_order(cycle, {"side": "BUY", "perm_id": "bad"}, "BUY") is False

    quantity, average, commission, rows = controller._aggregate_recovered_executions(cycle, "BUY")
    assert quantity == 2
    assert average == pytest.approx(99.0)
    assert commission == pytest.approx(0.5)
    assert len(rows) == 2

    controller._record_recovered_executions(cycle, [first, second], "BUY")
    controller._record_recovered_executions(cycle, [first], "BUY")
    details = controller.storage.cycle_audit_details(cycle.id)
    assert [row["execution_id"] for row in details["executions"]] == ["E1", "E2"]


def test_recovery_reconstructs_buy_sell_and_protective_fills(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(name: str) -> tuple[Any, ControllerAdapterStub, Any]:
        controller = _controller(controller_module, tmp_path, name)
        adapter = ControllerAdapterStub()
        controller.adapter = adapter
        monkeypatch.setattr(controller, "_execute_actions", lambda actions, cycle: None)
        return controller, adapter, StrategyEngine.start_cycle(
            StrategySettings(ticker="AAPL", protective_sell_enabled=False), 1, "", 100.0, 0.0
        )

    buy_controller, buy_adapter, buy_cycle = build("buy.sqlite")
    buy_cycle.stage = Stage.BUY_TRAIL_ACTIVE
    buy_cycle.quantity = 2
    buy_cycle.buy_order_ref = "IBKRBOT|AAPL|BUY"
    buy_cycle.buy_order_id = 101
    buy_cycle.buy_perm_id = 202
    buy_controller.storage.upsert_cycle(buy_cycle)
    buy_adapter.executions = [
        _execution(
            execution_id="BUY1",
            side="BOT",
            order_ref=buy_cycle.buy_order_ref,
            order_id=101,
            perm_id=202,
            shares=2,
            price=99.0,
            commission=0.4,
        )
    ]
    recovered_buy = buy_controller._recover_buy_from_executions(buy_cycle)
    assert recovered_buy is not None
    assert recovered_buy.stage == Stage.WAIT_RISE_TRIGGER
    assert recovered_buy.buy_filled_qty == 2

    sell_controller, sell_adapter, sell_cycle = build("sell.sqlite")
    sell_cycle.stage = Stage.SELL_TRAIL_ACTIVE
    sell_cycle.buy_filled_qty = 2
    sell_cycle.avg_buy_price = 99.0
    sell_cycle.sell_order_ref = "IBKRBOT|AAPL|SELL"
    sell_cycle.sell_order_id = 303
    sell_cycle.sell_perm_id = 404
    sell_controller.storage.upsert_cycle(sell_cycle)
    sell_adapter.executions = [
        _execution(
            execution_id="SELL1",
            side="SLD",
            order_ref=sell_cycle.sell_order_ref,
            order_id=303,
            perm_id=404,
            shares=2,
            price=102.0,
            commission=0.5,
        )
    ]
    recovered_sell = sell_controller._recover_sell_from_executions(sell_cycle)
    assert recovered_sell is not None
    assert recovered_sell.stage == Stage.CYCLE_COMPLETE
    assert recovered_sell.net_pnl == pytest.approx(5.5)

    protective_controller, protective_adapter, protective_cycle = build("protective.sqlite")
    protective_cycle.stage = Stage.WAIT_RISE_TRIGGER
    protective_cycle.buy_filled_qty = 2
    protective_cycle.avg_buy_price = 99.0
    protective_cycle.protective_sell_order_ref = "IBKRBOT|AAPL|PROTECTIVE_SELL"
    protective_cycle.protective_sell_order_id = 505
    protective_cycle.protective_sell_perm_id = 606
    protective_controller.storage.upsert_cycle(protective_cycle)
    protective_adapter.executions = [
        _execution(
            execution_id="PROTECT1",
            side="SLD",
            order_ref=protective_cycle.protective_sell_order_ref,
            order_id=505,
            perm_id=606,
            shares=2,
            price=97.0,
            commission=0.5,
        )
    ]
    recovered_protective = protective_controller._recover_protective_sell_from_executions(protective_cycle)
    assert recovered_protective is not None
    assert recovered_protective.stage == Stage.CYCLE_COMPLETE
    assert recovered_protective.protective_sell_filled_qty == 2

    protective_adapter.executions = []
    assert protective_controller._recover_protective_sell_from_executions(protective_cycle) is None


def test_price_error_and_confirmed_contract_poll_paths(
    controller_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(controller_module, tmp_path)
    controller._api_last_data_monotonic = time.monotonic() - 2.0
    controller._api_last_change_monotonic = time.monotonic() - 3.0
    controller._api_last_data_wall_time = "2026-07-11T12:00:00+00:00"
    controller._api_last_change_wall_time = "2026-07-11T11:59:59+00:00"
    controller._latest_rth_status = {"is_open": True, "message": "open"}
    controller._set_price_error_snapshot("feed failed")
    assert controller.price_snapshot["strategy_price_usable"] is False
    assert controller.price_snapshot["api_data_state"] == "stale"
    assert controller.price_snapshot["rth_open"] is True

    adapter = ControllerAdapterStub()
    adapter.snapshot = MarketPriceSnapshot(
        price=None,
        source="none",
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields={},
        timestamp="2026-07-11T12:00:00+00:00",
    )
    controller.adapter = adapter
    controller.contract = QualifiedContract("AAPL", 123, object())
    log_messages: list[str] = []
    monkeypatch.setattr(controller, "_update_rth_status", lambda contract: {})
    monkeypatch.setattr(controller, "_record_price_snapshot", lambda snapshot, contract: None)
    monkeypatch.setattr(controller, "_log", lambda level, message, cycle=None: log_messages.append(message))
    controller._poll_confirmed_contract_price_if_due(force=True)
    assert any("no usable price" in message for message in log_messages)

    monkeypatch.setattr(adapter, "price_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    controller._poll_confirmed_contract_price_if_due(force=True)
    assert controller.price_snapshot["error"] == "offline"
    assert any("refresh failed" in message for message in log_messages)


def test_recovery_flag_time_parsing_and_rth_age(controller_module: Any, tmp_path: Path) -> None:
    controller = _controller(controller_module, tmp_path)
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    controller.storage.upsert_cycle(cycle)
    controller._mark_recovery_required(cycle, "broker state is ambiguous")
    assert controller.active_cycle.stage == Stage.MANUAL_REVIEW
    assert controller.active_cycle.recovery_required is True
    assert controller._recovery_required is True
    assert controller.storage.get_latest_active_cycle("AAPL").stage == Stage.MANUAL_REVIEW
    assert any(
        row["event_type"] == "RECOVERY_REQUIRED"
        for row in controller.storage.cycle_audit_details(cycle.id)["decision_events"]
    )

    assert controller._safe_parse_utc_iso(None) is None
    assert controller._safe_parse_utc_iso("not-a-date") is None
    parsed_z = controller._safe_parse_utc_iso("2026-07-11T12:00:00Z")
    assert parsed_z is not None and parsed_z.tzinfo == timezone.utc
    parsed_naive = controller._safe_parse_utc_iso("2026-07-11T12:00:00")
    assert parsed_naive is not None and parsed_naive.tzinfo == timezone.utc

    controller._latest_rth_status = {"checked_at": "bad"}
    assert controller._rth_status_age_seconds() is None
    controller._latest_rth_status = {"checked_at": "2026-07-11T12:00:00+00:00"}
    assert controller._rth_status_age_seconds() >= 0.0


@pytest.mark.parametrize(
    ("side", "status_field"),
    [
        ("BUY", "buy_status"),
        ("PROTECTIVE_SELL", "protective_sell_status"),
        ("SELL", "sell_status"),
    ],
)
def test_terminal_unfilled_order_moves_to_stopped_error(
    controller_module: Any,
    tmp_path: Path,
    side: str,
    status_field: str,
) -> None:
    controller = _controller(controller_module, tmp_path, f"{side}.sqlite")
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.recovery_required = True
    cycle.close_position_market_requested = True
    controller.storage.upsert_cycle(cycle)
    polled = PolledOrderState(
        order_ref=f"IBKRBOT|AAPL|{side}",
        order_id=11,
        perm_id=22,
        status="Inactive",
        filled=0,
        remaining=10,
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={"status": "Inactive"},
    )

    controller._move_no_fill_order_to_stopped_error(cycle, polled, side)
    assert controller.active_cycle.stage == Stage.ERROR
    assert getattr(controller.active_cycle, status_field) == "Inactive"
    assert controller.active_cycle.recovery_required is False
    assert controller.active_cycle.close_position_market_requested is False
    assert "no longer working" in controller.active_cycle.error_message


def test_repeat_settings_are_derived_from_completed_cycle_not_draft_ticker(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller = _controller(controller_module, tmp_path)
    controller.strategy = StrategySettings(ticker="MSFT", auto_repeat=False, tif="DAY")
    source = StrategySettings(
        ticker="AAPL",
        investment_amount=12_345.0,
        initial_drop_pct=2.5,
        buy_rebound_trail_pct=0.7,
        rise_trigger_pct=1.8,
        sell_trailing_stop_pct=0.9,
        protective_sell_enabled=True,
        protective_sell_trailing_stop_pct=3.5,
        hard_risk_limits_enabled=True,
        max_spread_pct=0.6,
        max_cycles_per_ticker_day=4,
        reinvest_profits=False,
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        contract_con_id=123,
    )
    cycle = StrategyEngine.start_cycle(source, 3, "", 100.0, 0.0)
    cycle.con_id = 123

    repeat = controller._settings_for_repeat_cycle(cycle)
    assert repeat.ticker == "AAPL"
    assert repeat.investment_amount == 12_345.0
    assert repeat.initial_drop_pct == 2.5
    assert repeat.protective_sell_enabled is True
    assert repeat.max_spread_pct == 0.6
    assert repeat.contract_con_id == 123
    assert repeat.primary_exchange == "NASDAQ"
    assert repeat.auto_repeat is False
    assert repeat.tif == "DAY"


def test_account_display_cache_and_cycle_age_boundaries(controller_module: Any, tmp_path: Path) -> None:
    controller = _controller(controller_module, tmp_path)

    class AccountAdapter(ControllerAdapterStub):
        def managed_accounts(self) -> list[str]:
            return ["DU111", "DU222", "DU111", ""]

    controller.adapter = AccountAdapter()
    controller._refresh_display_accounts()
    assert controller._broker_display_accounts == ["DU111", "DU222"]
    assert controller._display_account_label() == "DU111 +1 accounts"

    controller._broker_display_accounts = []
    controller._remember_recovery_account_values(["DU333", "DU333", "DU444"])
    assert controller._broker_display_accounts == ["DU333", "DU444"]
    controller._remember_recovery_account_values("DU555")
    assert controller._broker_display_accounts[-1] == "DU555"

    controller.connection.account = "DUCONFIGURED"
    controller._remember_recovery_account_values("DUIGNORED")
    assert controller._display_account_label() == "DUCONFIGURED"

    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "DUCYCLE", 100.0, 0.0)
    controller.connection.account = ""
    controller.active_cycle = cycle
    assert controller._display_account_label() == "DUCYCLE"
    assert controller._active_cycle_stale_age_seconds(cycle) is not None
    cycle.updated_at = "invalid"
    assert controller._active_cycle_stale_age_seconds(cycle) == float("inf")
    cycle.stage = Stage.CYCLE_COMPLETE
    assert controller._active_cycle_stale_age_seconds(cycle) is None
