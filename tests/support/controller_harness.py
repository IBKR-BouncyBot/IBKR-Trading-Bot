"""Shared controller setup for deterministic non-GUI integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import StrategySettings
from app.storage import BotStorage
from tests.support.deterministic_broker import DeterministicBrokerAdapter


def permissive_strategy(*, ticker: str = "AAPL", auto_repeat: bool = False) -> StrategySettings:
    """Return settings that isolate state-machine behavior from optional guards."""
    return StrategySettings(
        ticker=ticker,
        investment_amount=10_000.0,
        initial_drop_pct=2.0,
        buy_rebound_trail_pct=1.0,
        rise_trigger_pct=3.0,
        sell_trailing_stop_pct=1.0,
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        protective_sell_enabled=False,
        hard_risk_limits_enabled=False,
        block_delayed_data_in_live=False,
        what_if_check_enabled=False,
        stale_data_guard_enabled=False,
        volatility_filter_enabled=False,
        session_timing_guard_enabled=False,
        reinvest_profits=False,
        auto_repeat=auto_repeat,
        rth_only=True,
    )


def make_controller(
    controller_module: Any,
    db_path: Path,
    broker: DeterministicBrokerAdapter,
    settings: StrategySettings,
) -> Any:
    controller = controller_module.TradingController(storage=BotStorage(db_path))
    controller.emit_snapshot = lambda *args, **kwargs: None
    controller.adapter = broker
    controller.connected = True
    controller.connection.account = ""
    controller.connection.market_data_type = 1
    controller.strategy = settings
    controller.contract = broker.contract
    controller._broker_connectivity = broker.connectivity_status().to_dict()
    controller._broker_connectivity_initialized = True
    controller._upstream_recovery_pending = False
    controller._recovery_required = False
    controller._startup_resume_required = False
    controller._latest_rth_status = broker.regular_trading_hours_status(broker.contract).to_dict()
    return controller


def publish_fresh_price(controller: Any, broker: DeterministicBrokerAdapter, price: float) -> None:
    snapshot = broker.publish_price(price)
    controller._record_price_snapshot(snapshot, broker.contract)
    controller._latest_rth_status = broker.regular_trading_hours_status(broker.contract).to_dict()
