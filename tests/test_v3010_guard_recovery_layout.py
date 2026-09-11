from __future__ import annotations

from pathlib import Path

from app.ib_adapter import MarketPriceSnapshot, PolledOrderState
from app.models import Stage, StrategySettings, atr_from_price_history, utc_now_iso
from app.storage import BotStorage
from app.strategy import StrategyEngine
from tests.test_controller_headless import _install_qt_stub

GUI = Path("app/gui.py").read_text(encoding="utf-8")


def _settings(**overrides: object) -> StrategySettings:
    values: dict[str, object] = {
        "ticker": "AAPL",
        "investment_amount": 1000.0,
        "initial_drop_pct": 2.0,
        "buy_rebound_trail_pct": 1.0,
        "rise_trigger_pct": 2.0,
        "sell_trailing_stop_pct": 1.0,
        "auto_repeat": False,
        "hard_risk_limits_enabled": False,
        "block_delayed_data_in_live": False,
        "what_if_check_enabled": False,
        "stale_data_guard_enabled": False,
        "volatility_filter_enabled": False,
        "session_timing_guard_enabled": False,
    }
    values.update(overrides)
    return StrategySettings(**values)


def test_max_spread_is_never_rewritten_from_live_market_data() -> None:
    risk_start = GUI.index("def _risk_default_market_kwargs")
    risk_block = GUI[risk_start : GUI.index("def _apply_suggested_broker_timing_defaults_from_amount", risk_start)]
    apply_start = GUI.index("def _apply_suggested_risk_limits_from_amount")
    apply_block = GUI[apply_start : GUI.index("def _selected_profile_data", apply_start)]

    assert '"bid": pick(' not in risk_block
    assert '"ask": pick(' not in risk_block
    assert 'maybe_set(self.max_spread_pct_spin, "max_spread_pct")' not in apply_block
    assert "This field is user-controlled and is never rewritten from live bid/ask data." in GUI
    assert "self.max_spread_pct_spin.valueChanged.connect(self._schedule_settings_autosave)" not in GUI
    assert "self.max_spread_pct_spin," in GUI


def test_live_dashboard_uses_full_width_recovery_audit_log_in_all_modes() -> None:
    view_start = GUI.index("def _apply_view_mode")
    view_block = GUI[view_start : GUI.index("def _build_dashboard", view_start)]
    dashboard_start = GUI.index("def _build_dashboard")
    dashboard_block = GUI[dashboard_start : GUI.index("def _connection_group", dashboard_start)]

    assert '"control_box"' not in view_block
    assert '("event_log_box", True)' in view_block
    assert "self.control_box" not in dashboard_block
    assert "self._control_group" not in dashboard_block
    assert "root.addWidget(self.event_log_box, 1)" in dashboard_block
    assert "lower.addWidget(self.event_log_box" not in dashboard_block
    assert "def _control_group" not in GUI
    assert 'self.start_btn = self.command_step_buttons["start"]' in GUI
    assert 'self.stop_btn = self.command_step_buttons["stop"]' in GUI


def test_stop_and_recovery_use_persisted_app_owned_quantity() -> None:
    visible_start = GUI.index("def _persisted_app_unsold_quantity")
    visible_block = GUI[visible_start : GUI.index("def _request_stop_action", visible_start)]
    stop_start = GUI.index("def _open_stop_strategy_dialog")
    stop_block = GUI[stop_start : GUI.index("def _stop_clicked", stop_start)]
    recovery_start = GUI.index("def _update_recovery_panel")
    recovery_block = GUI[recovery_start : GUI.index("def _set_recovery_details_text_preserve_scroll", recovery_start)]

    assert 'getattr(self.controller, "app_owned_unsold_position", None)' in visible_block
    close_start = GUI.index("def closeEvent")
    close_block = GUI[close_start : GUI.index("def _apply_styles", close_start)]

    assert "unsold_qty = self._persisted_app_unsold_quantity(cycle)" in stop_block
    assert "open_qty = self._persisted_app_unsold_quantity(cycle) if has_cycle else 0.0" in recovery_block
    assert "unsold_qty = self._persisted_app_unsold_quantity(cycle)" in close_block
    assert 'bought = _float_or_none(cycle.get("buy_filled_qty"))' not in close_block


def test_atr_observations_and_bars_continue_when_adaptation_is_disabled(tmp_path, monkeypatch) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = _settings(
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        atr_period=2,
        atr_bar_seconds=60,
    )
    controller._latest_rth_status = {"is_open": True, "checked_at": utc_now_iso()}

    snapshot = MarketPriceSnapshot(
        price=100.0,
        source="test",
        requested_market_data_type=1,
        subscription_market_data_type=1,
        fields={"last": 100.0, "bid": 99.99, "ask": 100.01},
        timestamp=utc_now_iso(),
        status="OK",
    )
    controller._record_price_snapshot(snapshot, None)
    assert len(controller._price_history) == 1

    now = 10_000.0
    controller._price_history.clear()
    controller._price_history.extend(
        [
            (now - 180.0, 100.0),
            (now - 120.0, 101.0),
            (now - 60.0, 99.0),
            (now, 100.0),
        ]
    )
    atr = controller._build_atr_snapshot(now + 1.0)

    assert atr["adaptive_enabled"] is False
    assert atr["collecting"] is True
    assert atr["bars_available"] >= 3
    assert atr["data_ready"] is True
    assert atr["ready"] is True
    assert "adaptive percentage updates are disabled" in str(atr["reason"]).lower()


def test_incremental_atr_matches_reference_aggregation(tmp_path, monkeypatch) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    controller = controller_module.TradingController(storage=BotStorage(tmp_path / "bot_state.sqlite"))
    controller.strategy = _settings(
        atr_adaptive_enabled=False,
        atr_block_new_buy_until_ready=False,
        atr_period=3,
        atr_bar_seconds=60,
    )
    controller._latest_rth_status = {"is_open": True, "checked_at": utc_now_iso()}
    points = [
        (9_600.0, 100.0),
        (9_620.0, 101.0),
        (9_660.0, 100.5),
        (9_680.0, 102.0),
        (9_720.0, 101.5),
        (9_740.0, 99.0),
        (9_780.0, 100.0),
        (9_800.0, 103.0),
    ]
    controller._price_history.extend(points)

    expected = atr_from_price_history(points, period=3, bar_seconds=60)
    actual = controller._build_atr_snapshot(9_801.0)

    assert actual["ready"] is expected["ready"]
    assert actual["bars_available"] == expected["bars_available"]
    assert actual["true_ranges_used"] == expected["true_ranges_used"]
    assert actual["atr"] == expected["atr"]
    assert actual["atr_pct"] == expected["atr_pct"]


def test_terminal_sell_poll_retires_older_recovery_probe_order(tmp_path, monkeypatch) -> None:
    controller_module = _install_qt_stub(monkeypatch)
    storage = BotStorage(tmp_path / "bot_state.sqlite")
    controller = controller_module.TradingController(storage=storage)
    settings = _settings(
        auto_repeat=True,
        hard_risk_limits_enabled=True,
        max_cycles_per_ticker_day=1,
    )
    controller.strategy = settings
    cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.quantity = 10
    cycle.buy_filled_qty = 10
    cycle.avg_buy_price = 100.0
    cycle.sell_order_ref = "IBKRBOT|AAPL|CYCLE-1|SELL_TRAIL"
    cycle.sell_order_id = 22
    cycle.sell_perm_id = 33
    cycle.sell_status = "Submitted"
    controller.active_cycle = cycle
    storage.upsert_cycle(cycle)
    controller._last_recovery_probe = {
        "checked_at": "2026-07-10T13:04:00+00:00",
        "cycle_id": cycle.id,
        "open_app_orders": [
            {
                "order_ref": cycle.sell_order_ref,
                "order_id": 22,
                "perm_id": 33,
                "status": "Submitted",
                "filled": 0,
                "remaining": 10,
            }
        ],
        "open_order_refs": [cycle.sell_order_ref],
    }
    polled = PolledOrderState(
        order_ref=cycle.sell_order_ref,
        order_id=22,
        perm_id=33,
        status="Filled",
        filled=10,
        remaining=0,
        avg_fill_price=103.0,
        commission=1.0,
        executions=[],
        raw={"status": "Filled"},
    )

    controller._handle_sell_order_poll(cycle, polled)

    assert controller.active_cycle is not None
    assert controller.active_cycle.stage == Stage.CYCLE_COMPLETE
    assert controller._last_recovery_probe["open_app_orders"] == []
    assert controller._last_recovery_probe["open_order_refs"] == []
    assert controller._last_recovery_probe["order_state_updated_at"]
    assert controller.app_owned_unsold_position("AAPL")["quantity"] == 0
    assert storage.get_next_cycle_number("AAPL") == 2
    assert any("Max cycles reached (1/1)" in row["message"] for row in storage.get_recent_events(20))
