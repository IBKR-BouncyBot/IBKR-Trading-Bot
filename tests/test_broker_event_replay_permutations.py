"""Offline replay tests for asynchronous broker callback ordering.

IBKR callbacks are audit evidence; the normalized order poll is authoritative
for strategy transitions.  These tests replay equivalent callback histories in
different orders and verify identical persisted trading state.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from app.ib_adapter import PolledOrderState
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


def _controller_and_buy_cycle(controller_module: Any, tmp_path: Path) -> tuple[Any, Any]:
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    controller.emit_snapshot = lambda *args, **kwargs: None
    controller.strategy = StrategySettings(
        ticker="AAPL",
        investment_amount=1_000.0,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        hard_risk_limits_enabled=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        auto_repeat=False,
    )
    cycle = StrategyEngine.start_cycle(controller.strategy, 1, "", 100.0, 0.0)
    cycle.stage = Stage.BUY_TRAIL_ACTIVE
    cycle.quantity = 10
    cycle.buy_order_ref = f"IBKRBOT|AAPL|{cycle.id}|BUY_TRAIL"
    cycle.buy_order_id = 1001
    cycle.buy_perm_id = 101001
    cycle.buy_status = "Submitted"
    storage.upsert_cycle(cycle)
    storage.add_order(
        cycle=cycle,
        action="BUY",
        order_type="TRAIL",
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        order_ref=cycle.buy_order_ref,
        quantity=cycle.quantity,
        trailing_percent=cycle.buy_rebound_trail_pct,
        initial_stop_price=cycle.buy_initial_trail_stop_price,
        status="Submitted",
    )
    controller.active_cycle = cycle
    return controller, cycle


def _execution(exec_id: str, shares: int, price: float, order_ref: str) -> dict[str, Any]:
    return {
        "execId": exec_id,
        "execution_id": exec_id,
        "shares": float(shares),
        "price": float(price),
        "avgPrice": float(price),
        "commission": 0.25,
        "currency": "USD",
        "order_ref": order_ref,
        "orderRef": order_ref,
        "time": "2026-07-10T14:30:00+00:00",
    }


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


@pytest.mark.parametrize("event_order", list(itertools.permutations(("OPEN_ORDER", "EXEC_DETAILS", "COMMISSION_REPORT", "ORDER_STATUS"))))
def test_callback_order_does_not_change_authoritative_buy_fill_result(
    controller_module: Any,
    tmp_path: Path,
    event_order: tuple[str, ...],
) -> None:
    controller, cycle = _controller_and_buy_cycle(controller_module, tmp_path)
    adapter = DeterministicBrokerAdapter()
    controller.adapter = adapter
    controller.connected = True

    payloads = {
        "OPEN_ORDER": {
            "event_type": "OPEN_ORDER",
            "order_ref": cycle.buy_order_ref,
            "order_id": cycle.buy_order_id,
            "perm_id": cycle.buy_perm_id,
            "status": "Submitted",
        },
        "EXEC_DETAILS": {
            "event_type": "EXEC_DETAILS",
            "order_ref": cycle.buy_order_ref,
            "order_id": cycle.buy_order_id,
            "perm_id": cycle.buy_perm_id,
            "execution_id": "E-BUY-1",
            "shares": 10,
            "price": 98.0,
        },
        "COMMISSION_REPORT": {
            "event_type": "COMMISSION_REPORT",
            "order_ref": cycle.buy_order_ref,
            "order_id": cycle.buy_order_id,
            "perm_id": cycle.buy_perm_id,
            "execution_id": "E-BUY-1",
            "commission": 0.25,
        },
        "ORDER_STATUS": {
            "event_type": "ORDER_STATUS",
            "order_ref": cycle.buy_order_ref,
            "order_id": cycle.buy_order_id,
            "perm_id": cycle.buy_perm_id,
            "status": "Filled",
            "filled": 10,
            "remaining": 0,
        },
    }
    for event_type in event_order:
        adapter.events.append(dict(payloads[event_type]))
    controller._drain_broker_events()

    polled = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=98.0,
        commission=0.25,
        executions=[_execution("E-BUY-1", 10, 98.0, str(cycle.buy_order_ref))],
        raw={"source": "authoritative poll"},
    )
    controller._handle_buy_order_poll(cycle, polled)

    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None
    assert persisted.stage == Stage.WAIT_RISE_TRIGGER
    assert persisted.buy_filled_qty == 10
    assert persisted.avg_buy_price == pytest.approx(98.0)
    assert controller.storage.get_app_owned_unsold_position("AAPL")["quantity"] == pytest.approx(10.0)

    audit = controller.storage.get_cycle_audit_bundle(cycle.id)
    broker_events = [row for row in controller.storage.recent_broker_events(10) if row["cycle_id"] == cycle.id]
    assert len(broker_events) == 4
    assert {row["event_type"] for row in broker_events} == set(event_order)
    assert len(audit["executions"]) == 1


@pytest.mark.parametrize("execution_order", [("E1", "E2"), ("E2", "E1")])
def test_execution_replay_is_idempotent_and_order_independent(
    controller_module: Any,
    tmp_path: Path,
    execution_order: tuple[str, str],
) -> None:
    controller, cycle = _controller_and_buy_cycle(controller_module, tmp_path)
    executions_by_id = {
        "E1": _execution("E1", 4, 98.0, str(cycle.buy_order_ref)),
        "E2": _execution("E2", 6, 98.5, str(cycle.buy_order_ref)),
    }
    executions = [executions_by_id[key] for key in execution_order]
    polled = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=98.3,
        commission=0.50,
        executions=executions,
        raw={"sequence": list(execution_order)},
    )

    controller._record_polled_executions(cycle, polled, "BUY")
    controller._record_polled_executions(cycle, polled, "BUY")

    audit = controller.storage.get_cycle_audit_bundle(cycle.id)
    rows = sorted(audit["executions"], key=lambda row: row["execution_id"])
    assert [row["execution_id"] for row in rows] == ["E1", "E2"]
    assert sum(float(row["shares"]) for row in rows) == pytest.approx(10.0)
    assert sum(float(row["commission"]) for row in rows) == pytest.approx(0.50)


@pytest.mark.parametrize("duplicates", [1, 2, 5])
def test_duplicate_terminal_poll_cannot_duplicate_execution_ledger(
    controller_module: Any,
    tmp_path: Path,
    duplicates: int,
) -> None:
    controller, cycle = _controller_and_buy_cycle(controller_module, tmp_path)
    polled = PolledOrderState(
        order_ref=str(cycle.buy_order_ref),
        order_id=cycle.buy_order_id,
        perm_id=cycle.buy_perm_id,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=98.0,
        commission=0.25,
        executions=[_execution("E-ONCE", 10, 98.0, str(cycle.buy_order_ref))],
        raw={"status": "Filled"},
    )

    for _ in range(duplicates):
        controller._record_polled_executions(cycle, polled, "BUY")

    with controller.storage.connect() as con:
        count = con.execute("SELECT COUNT(*) FROM executions WHERE execution_id = ?", ("E-ONCE",)).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("event_count", [1, 10, 100])
def test_raw_broker_replay_preserves_event_payloads_without_advancing_strategy(
    controller_module: Any,
    tmp_path: Path,
    event_count: int,
) -> None:
    controller, cycle = _controller_and_buy_cycle(controller_module, tmp_path)
    adapter = DeterministicBrokerAdapter()
    controller.adapter = adapter
    controller.connected = True

    for index in range(event_count):
        adapter.events.append(
            {
                "event_type": "ORDER_STATUS",
                "created_at": f"2026-07-10T14:30:{index % 60:02d}+00:00",
                "order_ref": cycle.buy_order_ref,
                "order_id": cycle.buy_order_id,
                "perm_id": cycle.buy_perm_id,
                "status": "Submitted",
                "raw_marker": {"index": index},
            }
        )
    controller._drain_broker_events()

    persisted = controller.storage.get_cycle(cycle.id)
    assert persisted is not None and persisted.stage == Stage.BUY_TRAIL_ACTIVE
    rows = controller.storage.recent_broker_events(event_count + 5)
    assert len(rows) == event_count
    markers = {row["raw"]["raw_marker"]["index"] for row in rows}
    assert markers == set(range(event_count))
