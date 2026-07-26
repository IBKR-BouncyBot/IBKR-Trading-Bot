"""Regression replays reduced from real BouncyBot audit and Gateway incidents."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ib_adapter import IbAsyncTwsAdapter, OrderPriceNormalization, PolledOrderState
from app.models import ConnectionSettings, Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.support.incident_replay import load_incident_fixture
from tests.test_comprehensive_ib_adapter import FakeIB, FakeOrder, FakeStock
from tests.test_controller_headless import _install_qt_stub


class _MarketRuleBroker(DeterministicBrokerAdapter):
    """Deterministic broker that reports a specific market-rule increment."""

    def __init__(self, *, ticker: str, con_id: int, increment: float, market_rule_id: int) -> None:
        super().__init__(ticker=ticker, con_id=con_id)
        self.increment = float(increment)
        self.market_rule_id = int(market_rule_id)
        self.contract.min_tick = self.increment
        self.contract.market_rule_id = self.market_rule_id
        self.contract.market_rule_exchange = "SMART"
        self.contract.market_rule_advertised = True

    def normalize_order_price(
        self,
        contract: Any,
        price: float,
        direction: str,
    ) -> OrderPriceNormalization:
        base = super().normalize_order_price(contract, price, direction)
        return OrderPriceNormalization(
            original_price=base.original_price,
            normalized_price=base.normalized_price,
            increment=base.increment,
            direction=base.direction,
            source="market_rule",
            market_rule_id=self.market_rule_id,
            market_rule_exchange="SMART",
        )


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _live_ib_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[IbAsyncTwsAdapter, FakeIB]:
    adapter = IbAsyncTwsAdapter()
    ib = FakeIB()
    adapter.ib = ib
    adapter._upstream_connected = True
    adapter._upstream_state = "connected"
    adapter._upstream_message = "ready"
    monkeypatch.setattr(adapter, "_require_ib_async", lambda: (FakeIB, FakeOrder, FakeStock))
    return adapter, ib


def _controller(
    controller_module: Any,
    tmp_path: Path,
    *,
    ticker: str,
    con_id: int,
    broker: DeterministicBrokerAdapter | None = None,
) -> tuple[Any, DeterministicBrokerAdapter]:
    active_broker = broker or DeterministicBrokerAdapter(ticker=ticker, con_id=con_id)
    settings = permissive_strategy(ticker=ticker)
    settings.contract_con_id = con_id
    controller = make_controller(
        controller_module,
        tmp_path / f"{ticker.lower()}-incident.sqlite",
        active_broker,
        settings,
    )
    controller.connection.account = "SIM"
    controller.storage.backup_database = lambda *args, **kwargs: None
    controller._start_trade_market_data_capture = lambda *args, **kwargs: None
    return controller, active_broker


def _buy_cycle(
    controller: Any,
    broker: DeterministicBrokerAdapter,
    *,
    cycle_number: int,
    order_ref: str,
    quantity: int,
    trailing_percent: float,
    initial_stop: float,
) -> Any:
    cycle = StrategyEngine.start_cycle(controller.strategy, cycle_number, "SIM", initial_stop, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = int(quantity)
    cycle.con_id = int(broker.contract.con_id)
    cycle.buy_order_ref = str(order_ref)
    handle = broker.place_trailing_stop(
        contract=broker.contract,
        action="BUY",
        quantity=cycle.quantity,
        trailing_percent=float(trailing_percent),
        initial_stop_price=float(initial_stop),
        order_ref=cycle.buy_order_ref,
        tif="GTC",
        account="SIM",
        outside_rth=False,
    )
    broker.events.clear()
    cycle.buy_order_id = handle.order_id
    cycle.buy_perm_id = handle.perm_id
    cycle.buy_status = handle.status
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller.storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="TRAIL",
        order_id=handle.order_id,
        perm_id=handle.perm_id,
        order_ref=cycle.buy_order_ref,
        quantity=cycle.quantity,
        trailing_percent=float(trailing_percent),
        initial_stop_price=float(initial_stop),
        status=handle.status,
    )
    return cycle


def _callback_execution(cycle: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": "EXEC_DETAILS",
        "created_at": "2026-07-20T14:00:00+00:00",
        "executed_at": "2026-07-20T13:59:59+00:00",
        "order_ref": cycle.buy_order_ref,
        "order_id": cycle.buy_order_id,
        "perm_id": cycle.buy_perm_id,
        "execution_id": row["execution_id"],
        "side": "BOT",
        "shares": row["shares"],
        "price": row["price"],
        "currency": "USD",
        "ticker": cycle.ticker,
    }


def _callback_commission(cycle: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": "COMMISSION_REPORT",
        "created_at": "2026-07-20T14:00:01+00:00",
        "order_ref": cycle.buy_order_ref,
        "order_id": cycle.buy_order_id,
        "perm_id": cycle.buy_perm_id,
        "execution_id": row["execution_id"],
        "commission": row["commission"],
        "currency": row["currency"],
        "ticker": cycle.ticker,
    }


def test_iren_fixture_replays_market_rule_and_what_if_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_incident_fixture("iren_invalid_price")
    contract_data = fixture["contract"]
    adapter, ib = _live_ib_adapter(monkeypatch)

    raw = FakeStock(
        contract_data["ticker"],
        contract_data["exchange"],
        contract_data["currency"],
        primaryExchange=contract_data["primary_exchange"],
    )
    raw.conId = int(contract_data["con_id"])
    ib.qualified_contracts = [raw]
    ib.contract_details = [
        SimpleNamespace(
            contract=raw,
            minTick=float(contract_data["min_tick"]),
            validExchanges=",".join(contract_data["valid_exchanges"]),
            orderTypes="MKT,TRAIL,WHATIF",
            marketRuleIds=",".join(str(value) for value in contract_data["market_rule_ids"]),
            liquidHours="20260724:0930-20260724:1600",
            timeZoneId="US/Eastern",
            minSize=1.0,
            sizeIncrement=1.0,
        )
    ]
    ib.market_rule_values[int(contract_data["market_rule_id"])] = [
        SimpleNamespace(lowEdge=float(row["low_edge"]), increment=float(row["increment"]))
        for row in contract_data["market_rule_bands"]
    ]

    contract = adapter.qualify_stock(
        contract_data["ticker"],
        contract_data["exchange"],
        contract_data["currency"],
        contract_data["primary_exchange"],
        int(contract_data["con_id"]),
    )
    normalized = adapter.normalize_order_price(
        contract,
        float(fixture["replay"]["proposed_stop"]),
        "up",
    )

    assert contract.min_tick == pytest.approx(0.0001)
    assert normalized.market_rule_id == int(contract_data["market_rule_id"])
    assert normalized.increment == pytest.approx(0.01)
    assert normalized.normalized_price == pytest.approx(fixture["expected"]["normalized_buy_stop"])
    assert ib.market_rule_requests == [557]

    what_if = fixture["replay"]["what_if"]
    ib.next_order_state = SimpleNamespace(
        status=what_if["status"],
        warningText="",
        initMarginChange="",
        maintMarginChange="",
        equityWithLoanChange="",
    )
    result = adapter.what_if_trailing_stop(
        contract=contract,
        action="BUY",
        quantity=int(fixture["replay"]["quantity"]),
        trailing_percent=float(fixture["replay"]["trailing_percent"]),
        initial_stop_price=normalized.normalized_price,
        order_ref="IBKRBOT|IREN|CYCLE-000001|INCIDENT|BUY_TRAIL|WHATIF",
        tif="GTC",
        account="",
        outside_rth=False,
    )

    assert result["ok"] is fixture["expected"]["what_if_ok"]
    assert result["status"] == "ValidationError"
    assert len(ib.what_if_orders) == 1
    assert ib.what_if_orders[0][1].transmit is True
    assert ib.what_if_orders[0][1].whatIf is True


def test_iren_fixture_replays_definitive_rejection_circuit_breaker(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    fixture = load_incident_fixture("iren_invalid_price")
    controller, broker = _controller(
        controller_module,
        tmp_path,
        ticker="IREN",
        con_id=int(fixture["contract"]["con_id"]),
    )
    cycle = _buy_cycle(
        controller,
        broker,
        cycle_number=1,
        order_ref="IBKRBOT|IREN|CYCLE-000001|INCIDENT|BUY_TRAIL",
        quantity=int(fixture["replay"]["quantity"]),
        trailing_percent=float(fixture["replay"]["trailing_percent"]),
        initial_stop=float(fixture["expected"]["normalized_buy_stop"]),
    )
    rejection = fixture["replay"]["live_order"]
    polled = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status=str(rejection["status"]),
        filled=0,
        remaining=int(rejection["remaining"]),
        avg_fill_price=0.0,
        commission=0.0,
        executions=[],
        raw={
            "broker_errors": [
                {
                    "error_code": int(rejection["error_code"]),
                    "message": str(rejection["message"]),
                }
            ]
        },
    )

    controller._handle_buy_order_poll(cycle, polled)

    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None
    assert persisted.stage.value == fixture["expected"]["terminal_stage"]
    assert "201" in str(persisted.error_message)
    assert "no replacement" in str(persisted.error_message).lower()
    assert len(broker.placed_orders) == 1
    decisions = controller.storage.get_cycle_audit_bundle(cycle.id)["decision_events"]
    assert [row["event_type"] for row in decisions].count("ORDER_TERMINAL_WITHOUT_FILL") == 1


def test_nbis_fixture_reconciles_second_fill_during_cancel(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    fixture = load_incident_fixture("nbis_partial_fill_cancel_race")
    controller, broker = _controller(
        controller_module,
        tmp_path,
        ticker="NBIS",
        con_id=int(fixture["contract"]["con_id"]),
    )
    cycle = _buy_cycle(
        controller,
        broker,
        cycle_number=23,
        order_ref=str(fixture["order"]["order_ref"]),
        quantity=int(fixture["order"]["quantity"]),
        trailing_percent=float(fixture["order"]["trailing_percent"]),
        initial_stop=float(fixture["order"]["initial_stop"]),
    )
    first, _, second, *_ = fixture["events"]

    first_state = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=int(first["shares"]),
        price=float(first["price"]),
        commission=float(first["commission"]),
        execution_id=str(first["execution_id"]),
        terminal=False,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(cycle, first_state)

    assert controller.active_cycle.stage == Stage.BUY_TRAIL_ACTIVE
    assert controller.active_cycle.buy_filled_qty == 28
    assert controller.active_cycle.buy_remainder_cancel_requested is True
    assert broker.cancelled_orders == [cycle.buy_order_ref]

    second_state = broker.fill_order(
        str(cycle.buy_order_ref),
        shares=int(second["shares"]),
        price=float(second["price"]),
        commission=float(second["commission"]),
        execution_id=str(second["execution_id"]),
        terminal=True,
    )
    broker.events.clear()
    controller._handle_buy_order_poll(controller.active_cycle, second_state)

    expected = fixture["expected"]
    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None
    assert persisted.stage.value == expected["terminal_stage"]
    assert persisted.buy_filled_qty == expected["buy_filled_qty"]
    assert persisted.avg_buy_price == pytest.approx(expected["average_buy_price"])
    assert persisted.buy_commission == pytest.approx(expected["buy_commission"])
    assert persisted.buy_remainder_cancel_requested is False
    assert controller.storage.get_app_owned_unsold_position(
        "NBIS",
        con_id=int(fixture["contract"]["con_id"]),
    )["quantity"] == pytest.approx(expected["sellable_quantity"])

    executions = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert len(executions) == expected["execution_count"]
    assert {row["execution_id"] for row in executions} == {"NBIS-PART-1", "NBIS-PART-2"}
    assert sum(float(row["shares"]) for row in executions) == pytest.approx(56.0)


_CALLBACK_ORDERS = [
    ("exec1", "commission1", "exec2", "commission2"),
    ("commission1", "exec1", "commission2", "exec2"),
    ("exec2", "commission2", "exec1", "commission1"),
    ("commission2", "exec2", "commission1", "exec1"),
    ("commission1", "commission2", "exec1", "exec2"),
    ("exec1", "exec2", "commission2", "commission1"),
]


@pytest.mark.parametrize("event_order", _CALLBACK_ORDERS)
def test_nbis_fixture_late_callback_order_is_idempotent(
    controller_module: Any,
    tmp_path: Path,
    event_order: tuple[str, ...],
) -> None:
    fixture = load_incident_fixture("nbis_partial_fill_cancel_race")
    controller, broker = _controller(
        controller_module,
        tmp_path,
        ticker="NBIS",
        con_id=int(fixture["contract"]["con_id"]),
    )
    cycle = _buy_cycle(
        controller,
        broker,
        cycle_number=23,
        order_ref=str(fixture["order"]["order_ref"]),
        quantity=int(fixture["order"]["quantity"]),
        trailing_percent=float(fixture["order"]["trailing_percent"]),
        initial_stop=float(fixture["order"]["initial_stop"]),
    )
    first, _, second, first_commission, second_commission = fixture["events"]
    payloads = {
        "exec1": _callback_execution(cycle, first),
        "commission1": _callback_commission(cycle, first_commission),
        "exec2": _callback_execution(cycle, second),
        "commission2": _callback_commission(cycle, second_commission),
    }

    broker.events.extend(dict(payloads[key]) for key in event_order)
    controller._drain_broker_events()

    terminal = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=56,
        remaining=0,
        avg_fill_price=float(fixture["expected"]["average_buy_price"]),
        commission=float(fixture["expected"]["buy_commission"]),
        executions=[],
        raw={"source": "incident terminal order status"},
    )
    controller._handle_buy_order_poll(controller.active_cycle, terminal)

    broker.events.extend([dict(payloads[key]) for key in event_order])
    controller._drain_broker_events()

    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None
    assert persisted.stage == Stage.WAIT_RISE_TRIGGER
    assert persisted.buy_filled_qty == 56
    assert persisted.buy_commission == pytest.approx(fixture["expected"]["buy_commission"])
    executions = controller.storage.get_cycle_audit_bundle(cycle.id)["executions"]
    assert {row["execution_id"] for row in executions} == {"NBIS-PART-1", "NBIS-PART-2"}
    assert sum(float(row["shares"]) for row in executions) == pytest.approx(56.0)
    assert sum(float(row["commission"]) for row in executions) == pytest.approx(
        fixture["expected"]["buy_commission"]
    )


def test_foreign_fixture_callbacks_remain_unowned_and_cannot_mutate_local_cycle(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    fixture = load_incident_fixture("foreign_order_ref_isolation")
    local = fixture["local_cycle"]
    controller, broker = _controller(
        controller_module,
        tmp_path,
        ticker=str(local["ticker"]),
        con_id=526906130,
    )
    cycle = StrategyEngine.start_cycle(controller.strategy, 40, "SIM", 42.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.con_id = 526906130
    cycle.quantity = int(local["buy_filled_qty"])
    cycle.buy_filled_qty = int(local["buy_filled_qty"])
    cycle.avg_buy_price = 42.0
    cycle.buy_commission = float(local["buy_commission"])
    cycle.buy_order_ref = str(local["order_ref"])
    cycle.buy_status = "Filled"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    controller.storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="TRAIL",
        order_id=1001,
        perm_id=101001,
        order_ref=cycle.buy_order_ref,
        quantity=cycle.quantity,
        trailing_percent=0.2,
        initial_stop_price=42.0,
        status="Filled",
    )
    controller.status = "local baseline"

    broker.events.extend(dict(event) for event in fixture["foreign_events"])
    controller._drain_broker_events()

    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None
    assert persisted.stage == Stage.WAIT_RISE_TRIGGER
    assert persisted.buy_filled_qty == int(local["buy_filled_qty"])
    assert persisted.buy_commission == pytest.approx(float(local["buy_commission"]))
    assert controller.status == "local baseline"
    audit = controller.storage.get_cycle_audit_bundle(cycle.id)
    assert audit["decision_events"] == []
    assert audit["executions"] == []

    broker_events = controller.storage.recent_broker_events(20)
    assert len(broker_events) == fixture["expected"]["foreign_broker_events_unowned"]
    assert all(row["cycle_id"] is None for row in broker_events)
    assert {row["ticker"] for row in broker_events} == {"NBIS", "LAC"}


def _delayed_data_controller(
    controller_module: Any,
    tmp_path: Path,
) -> tuple[Any, DeterministicBrokerAdapter, Any, Any]:
    fixture = load_incident_fixture("vwra_delayed_data_block")
    broker = _MarketRuleBroker(
        ticker="VWRA",
        con_id=int(fixture["contract"]["con_id"]),
        increment=float(fixture["contract"]["price_increment"]),
        market_rule_id=int(fixture["contract"]["market_rule_id"]),
    )
    controller, _ = _controller(
        controller_module,
        tmp_path,
        ticker="VWRA",
        con_id=int(fixture["contract"]["con_id"]),
        broker=broker,
    )
    controller.strategy.hard_risk_limits_enabled = True
    controller.strategy.block_delayed_data_in_live = True
    controller.connection = ConnectionSettings(account="SIM", trading_mode="live", market_data_type=0)
    market_data = fixture["market_data"]
    controller.price_snapshot = {
        "price": float(market_data["selected_price"]),
        "selected_market_data_type": int(market_data["selected_type"]),
        "subscription_market_data_type": int(market_data["subscription_type"]),
        "fields": {
            "bid": float(market_data["bid"]),
            "ask": float(market_data["ask"]),
            "last": float(market_data["selected_price"]),
            "marketPrice": float(market_data["selected_price"]),
        },
    }
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "SIM", 187.0, 0.0)
    cycle.con_id = int(fixture["contract"]["con_id"])
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = 53
    cycle.buy_order_ref = "IBKRBOT|VWRA|CYCLE-000001|DELAYED1|BUY_TRAIL"
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    action = SimpleNamespace(
        action_type="PLACE_BUY_TRAIL",
        payload={
            "ticker": "VWRA",
            "quantity": 53,
            "order_type": "TRAIL",
            "trailing_percent": 0.1,
            "initial_stop_price": 186.48,
            "reference_price": 186.48,
            "sizing_price": 186.48,
            "budget": 10_000.0,
            "order_ref": cycle.buy_order_ref,
        },
    )
    return controller, broker, cycle, action


def test_vwra_delayed_fixture_blocks_before_order_intent(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    fixture = load_incident_fixture("vwra_delayed_data_block")
    controller, broker, cycle, action = _delayed_data_controller(controller_module, tmp_path)

    controller._execute_actions([action], cycle)

    assert broker.placed_orders == []
    with controller.storage.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == fixture["expected"][
            "order_intent_count"
        ]
        assert con.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    assert controller.active_cycle.stage.value == fixture["expected"]["terminal_stage"]
    assert controller.active_cycle.buy_status == "PreflightBlocked"
    assert "non-live market data mode 3" in str(controller.active_cycle.error_message)
    with controller.storage.connect() as con:
        normalized_rows = con.execute(
            "SELECT COUNT(*) FROM events WHERE message LIKE 'Normalized BUY trailing stop%'"
        ).fetchone()[0]
    assert normalized_rows == 0


def test_vwra_stage3_fixture_waits_for_broker_valid_sell_stop(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    fixture = load_incident_fixture("vwra_stage3_market_rule_rounding")
    contract = fixture["contract"]
    broker = _MarketRuleBroker(
        ticker="VWRA",
        con_id=int(contract["con_id"]),
        increment=float(contract["price_increment"]),
        market_rule_id=int(contract["market_rule_id"]),
    )
    controller, _ = _controller(
        controller_module,
        tmp_path,
        ticker="VWRA",
        con_id=int(contract["con_id"]),
        broker=broker,
    )
    cycle_data = fixture["cycle"]
    cycle = StrategyEngine.start_cycle(controller.strategy, 2, "SIM", 187.0, 0.0)
    cycle.stage = Stage.WAIT_RISE_TRIGGER
    cycle.con_id = int(contract["con_id"])
    cycle.quantity = int(cycle_data["quantity"])
    cycle.buy_filled_qty = int(cycle_data["quantity"])
    cycle.avg_buy_price = float(cycle_data["average_buy_price"])
    cycle.rise_trigger_pct = float(cycle_data["minimum_profit_percent"])
    cycle.sell_trailing_stop_pct = float(cycle_data["sell_trailing_percent"])

    first = fixture["first_quote"]
    first_candidate, first_actions = StrategyEngine.on_price_update(
        cycle,
        float(first["selected_price"]),
        is_rth=True,
    )
    assert first_candidate.stage == Stage.SELL_TRAIL_ACTIVE
    assert len(first_actions) == 1
    controller.active_cycle = first_candidate
    controller.storage.upsert_cycle(first_candidate)
    controller.price_snapshot = {
        "price": float(first["selected_price"]),
        "fields": {
            "bid": float(first["bid"]),
            "ask": float(first["ask"]),
            "last": float(first["last"]),
            "marketPrice": float(first["selected_price"]),
        },
    }
    controller._execute_actions(first_actions, first_candidate)

    after_first = controller.active_cycle
    assert after_first.stage == Stage.WAIT_RISE_TRIGGER
    assert broker.placed_orders == []
    assert "minimum-profit stop" in str(after_first.error_message)

    later = fixture["later_quote"]
    later_candidate, later_actions = StrategyEngine.on_price_update(
        after_first,
        float(later["selected_price"]),
        is_rth=True,
    )
    controller.active_cycle = later_candidate
    controller.storage.upsert_cycle(later_candidate)
    controller.price_snapshot = {
        "price": float(later["selected_price"]),
        "fields": {
            "bid": float(later["bid"]),
            "ask": float(later["ask"]),
            "last": float(later["last"]),
            "marketPrice": float(later["selected_price"]),
        },
    }
    controller._execute_actions(later_actions, later_candidate)

    assert controller.active_cycle.stage == Stage.SELL_TRAIL_ACTIVE
    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0]["initial_stop_price"] == pytest.approx(
        later["expected_normalized_sell_stop"]
    )


def test_vwra_lse_fixture_characterizes_liquid_hours_and_continuous_close_gap() -> None:
    fixture = load_incident_fixture("vwra_lse_continuous_close_mismatch")
    contract = fixture["contract"]
    observed = fixture["observed_workflow"]
    oracle = fixture["independent_session_oracle"]
    submitted = dt.datetime.fromisoformat(observed["replacement_market_sell_submitted_utc"])

    status = IbAsyncTwsAdapter._parse_liquid_hours_window(
        str(contract["ibkr_liquid_hours"]),
        str(contract["ibkr_time_zone"]),
        now_utc=submitted,
    )

    assert status is not None and status.is_open is True
    app_close = dt.datetime.fromisoformat(status.session_close).astimezone(dt.timezone.utc)
    oracle_close = dt.datetime.fromisoformat(oracle["continuous_close_utc"])
    difference = (app_close - oracle_close).total_seconds() / 60.0

    assert difference == fixture["expected"]["liquid_close_minus_continuous_close_minutes"]
    assert oracle_close < submitted < app_close
    assert observed["replacement_status"] == "Cancelled"
    assert observed["replacement_filled"] == 0
    assert observed["shares_remaining"] == 53
    assert observed["terminal_stage"] == "ERROR"

    effective = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
        status,
        str(contract["primary_exchange"]),
        submitted,
    )
    assert effective.is_open is False
    assert dt.datetime.fromisoformat(effective.session_close).astimezone(
        dt.timezone.utc
    ) == oracle_close
