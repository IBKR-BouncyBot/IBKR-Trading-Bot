"""Controller-level decision tables for trading guards and recovery confidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models import Stage
from app.strategy import StrategyEngine
from tests.support.controller_harness import make_controller, permissive_strategy, publish_fresh_price
from tests.support.deterministic_broker import DeterministicBrokerAdapter
from tests.test_controller_headless import _install_qt_stub


@pytest.fixture
def controller_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IBKR_BOT_HEADLESS_SIGNALS", "1")
    return _install_qt_stub(monkeypatch)


def _controller_with_cycle(controller_module: Any, tmp_path: Path, stage: Stage = Stage.WAIT_INITIAL_DROP) -> tuple[Any, Any, Any]:
    broker = DeterministicBrokerAdapter()
    settings = permissive_strategy()
    controller = make_controller(controller_module, tmp_path / "decision.sqlite", broker, settings)
    publish_fresh_price(controller, broker, 100.0)
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.stage = stage
    if stage in {Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE, Stage.CYCLE_COMPLETE}:
        cycle.quantity = 10
        cycle.buy_filled_qty = 10
        cycle.avg_buy_price = 98.0
        cycle.rise_trigger_price = 101.0
    if stage == Stage.CYCLE_COMPLETE:
        cycle.sell_filled_qty = 10
        cycle.avg_sell_price = 102.0
        cycle.net_pnl = 40.0
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)
    return controller, broker, cycle


@pytest.mark.parametrize(
    ("stage", "connected", "upstream", "recovery_pending", "expected_code", "expected_prefix"),
    [
        (Stage.WAIT_INITIAL_DROP, False, False, False, "disconnected", "BUY blocked"),
        (Stage.BUY_TRAIL_ACTIVE, True, False, False, "upstream_disconnected", "BUY blocked"),
        (Stage.WAIT_RISE_TRIGGER, True, False, False, "upstream_disconnected", "SELL blocked"),
        (Stage.SELL_TRAIL_ACTIVE, True, True, True, "upstream_recovery", "SELL blocked"),
    ],
)
def test_connectivity_guard_decision_table(
    controller_module: Any,
    tmp_path: Path,
    stage: Stage,
    connected: bool,
    upstream: bool,
    recovery_pending: bool,
    expected_code: str,
    expected_prefix: str,
) -> None:
    controller, broker, _ = _controller_with_cycle(controller_module, tmp_path, stage)
    controller.connected = connected
    broker.local_connected = connected
    broker.upstream_connected = upstream
    broker.upstream_state = "connected" if upstream else "upstream_disconnected"
    broker.upstream_message = "ready" if upstream else "offline"
    broker.upstream_error_code = None if upstream else 1100
    controller._upstream_recovery_pending = recovery_pending

    status = controller._trading_status_snapshot()

    assert status["summary"].startswith(expected_prefix)
    assert status["state"] == "waiting"
    assert expected_code in {item["code"] for item in status["blockers"]}


@pytest.mark.parametrize(
    ("condition", "expected_code", "summary_fragment"),
    [
        ("no_price", "no_price", "No usable price"),
        ("invalidated", "fresh_market_data_pending", "Waiting for data"),
        ("stale", "stale_data", "Stale data"),
        ("rth_closed", "rth_closed", "RTH closed"),
        ("atr_warmup", "atr_warmup", "ATR 3/15"),
    ],
)
def test_market_and_strategy_guard_decision_table(
    controller_module: Any,
    tmp_path: Path,
    condition: str,
    expected_code: str,
    summary_fragment: str,
) -> None:
    controller, broker, cycle = _controller_with_cycle(controller_module, tmp_path)
    if condition == "no_price":
        controller.price_snapshot = None
        controller._api_data_invalidated = False
    elif condition == "invalidated":
        controller._api_data_invalidated = True
        controller._api_data_invalidated_reason = "Waiting for post-reconnect market data."
        controller.price_snapshot["api_data_invalidated"] = True
        controller.price_snapshot["api_data_invalidated_reason"] = controller._api_data_invalidated_reason
    elif condition == "stale":
        controller._api_data_invalidated = False
        controller.price_snapshot["api_data_state"] = "stale"
        controller.price_snapshot["api_data_age_seconds"] = 60.0
        cycle.stale_data_guard_enabled = False
    elif condition == "rth_closed":
        broker.rth_open = False
        controller._latest_rth_status = broker.regular_trading_hours_status(broker.contract).to_dict()
    elif condition == "atr_warmup":
        cycle.atr_adaptive_enabled = True
        cycle.atr_block_new_buy_until_ready = True
        controller.price_snapshot["atr_ready"] = False
        controller.price_snapshot["atr"] = {
            "ready": False,
            "bars_available": 3,
            "bars_required": 15,
            "reason": "ATR has not collected enough RTH-only bars yet",
        }
        controller.price_snapshot["atr_bars_available"] = 3
        controller.price_snapshot["atr_bars_required"] = 15
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    status = controller._trading_status_snapshot()

    assert expected_code in {item["code"] for item in status["blockers"]}
    assert summary_fragment in status["summary"] or summary_fragment in status["tooltip"]


def test_app_owned_position_blocks_buy_but_external_position_does_not(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller, broker, waiting_cycle = _controller_with_cycle(controller_module, tmp_path)
    broker.external_position = 890.0
    status = controller._trading_status_snapshot()
    assert "app_owned_position" not in {item["code"] for item in status["blockers"]}

    owned_cycle = StrategyEngine.start_cycle(permissive_strategy(), 2, "", 100.0, 0.0)
    owned_cycle.stage = Stage.WAIT_RISE_TRIGGER
    owned_cycle.buy_filled_qty = 7
    owned_cycle.avg_buy_price = 98.0
    owned_cycle.buy_order_ref = f"IBKRBOT|AAPL|{owned_cycle.id}|BUY"
    controller.storage.upsert_cycle(owned_cycle)
    controller.storage.add_execution(
        cycle=owned_cycle,
        ticker="AAPL",
        side="BUY",
        shares=7,
        price=98.0,
        order_ref=owned_cycle.buy_order_ref,
        execution_id="OWNED-BUY",
    )
    controller.active_cycle = waiting_cycle

    status = controller._trading_status_snapshot()
    blocker = next(item for item in status["blockers"] if item["code"] == "app_owned_position")
    assert blocker["side"] == "BUY"
    assert "7 unsold app-owned" in blocker["message"]
    assert "890" not in blocker["message"]


@pytest.mark.parametrize(
    ("startup", "stage", "stale", "recovery_required", "connected", "probe", "expected_summary", "expected_confidence"),
    [
        (True, Stage.WAIT_INITIAL_DROP, False, False, True, {}, "Start required", "broker_partially_checked"),
        (False, Stage.ERROR, False, False, True, {}, "Blocked", "broker_partially_checked"),
        (False, Stage.MANUAL_REVIEW, False, False, True, {}, "Blocked", "manual_review_required"),
        (False, Stage.WAIT_INITIAL_DROP, True, False, True, {}, "Running", "manual_review_required"),
        (False, Stage.WAIT_INITIAL_DROP, False, True, True, {}, "Running", "manual_review_required"),
        (False, Stage.WAIT_INITIAL_DROP, False, False, False, {}, "BUY blocked", "local_state_only"),
        (False, Stage.WAIT_INITIAL_DROP, False, False, True, {"error": "partial"}, "Running", "broker_partially_checked"),
        (False, Stage.WAIT_INITIAL_DROP, False, False, True, {"checked_at": "now"}, "Running", "fully_reconciled"),
        (False, Stage.CYCLE_COMPLETE, False, False, True, {"checked_at": "now"}, "Stopped", "fully_reconciled"),
    ],
)
def test_recovery_confidence_and_trading_status_decision_table(
    controller_module: Any,
    tmp_path: Path,
    startup: bool,
    stage: Stage,
    stale: bool,
    recovery_required: bool,
    connected: bool,
    probe: dict[str, Any],
    expected_summary: str,
    expected_confidence: str,
) -> None:
    controller, broker, cycle = _controller_with_cycle(controller_module, tmp_path, stage)
    controller._startup_resume_required = startup
    controller._stale_active_cycle_detected = stale
    controller._recovery_required = recovery_required
    controller.connected = connected
    broker.local_connected = connected
    broker.upstream_connected = connected
    controller._last_recovery_probe = dict(probe)
    if stage == Stage.ERROR:
        cycle.error_message = "Injected strategy error."
    if stage == Stage.MANUAL_REVIEW:
        cycle.error_message = "Injected recovery review."
    controller.active_cycle = cycle

    status = controller._trading_status_snapshot()

    assert status["summary"].startswith(expected_summary)
    assert controller._recovery_confidence() == expected_confidence


def test_completed_cycle_and_guard_only_state_do_not_become_recovery_required(
    controller_module: Any,
    tmp_path: Path,
) -> None:
    controller, _, cycle = _controller_with_cycle(controller_module, tmp_path)
    cycle.atr_adaptive_enabled = True
    cycle.atr_block_new_buy_until_ready = True
    controller.price_snapshot["atr_ready"] = False
    controller.price_snapshot["atr_bars_available"] = 0
    controller.price_snapshot["atr_bars_required"] = 15
    controller.active_cycle = cycle

    status = controller._trading_status_snapshot()
    assert any(item["code"] == "atr_warmup" for item in status["blockers"])
    assert controller._recovery_required is False
    assert cycle.recovery_required is False

    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.sell_filled_qty = 10
    cycle.avg_buy_price = 98.0
    cycle.avg_sell_price = 102.0
    controller.storage.upsert_cycle(cycle)
    controller.active_cycle = cycle
    controller._last_recovery_probe = {"checked_at": "now", "open_app_orders": []}

    assert controller._trading_status_snapshot()["summary"] == "Stopped"
    assert controller._recovery_confidence() == "fully_reconciled"
