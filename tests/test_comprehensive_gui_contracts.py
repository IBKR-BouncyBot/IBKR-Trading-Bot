"""Headless component and contract tests for every GUI helper family.

These tests intentionally avoid screenshot assertions. They verify formatting,
state classification, widget state transitions, timeline reconstruction, and
that every widget can execute its construction/update/paint entry points under
deterministic Qt doubles.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import ConnectionSettings, Stage, StopAction, StrategySettings, recovery_cycle_signature
from tests.support.qt_stubs import Dummy, EventStub, PointStub, RectStub, SignalStub, imported_gui_with_stubs


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(Path.cwd()) as module:
        yield module


class ControllerStub:
    """Observable controller boundary used by MainWindow component tests."""

    def __init__(self) -> None:
        self.connection = ConnectionSettings()
        self.strategy = StrategySettings(ticker="AAPL")
        self.signals = SimpleNamespace(
            snapshot_updated=SignalStub(),
            history_updated=SignalStub(),
            connection_changed=SignalStub(),
            ticker_search_updated=SignalStub(),
        )
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if name == "app_owned_unsold_position":
                return 0.0
            if name == "get_cycle_audit_details":
                return {}
            return None

        return call


def _cycle(stage: Stage = Stage.WAIT_INITIAL_DROP, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "cycle-1",
        "ticker": "AAPL",
        "cycle_number": 1,
        "stage": stage.value,
        "anchor_price": 100.0,
        "drop_trigger_price": 98.0,
        "last_price": 99.0,
        "budget": 1000.0,
        "buy_filled_qty": 0,
        "sell_filled_qty": 0,
        "protective_sell_filled_qty": 0,
        "avg_buy_price": None,
        "avg_sell_price": None,
        "gross_pnl": None,
        "net_pnl": None,
        "created_at": "2026-07-11T12:00:00+00:00",
        "updated_at": "2026-07-11T12:05:00+00:00",
        "error_message": "",
    }
    row.update(overrides)
    return row


def _snapshot(stage: Stage = Stage.WAIT_INITIAL_DROP, **overrides: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "connected": True,
        "status": "Connected",
        "active_cycle": _cycle(stage),
        "strategy": asdict(StrategySettings(ticker="AAPL")),
        "connection": asdict(ConnectionSettings()),
        "price_snapshot": {
            "price": 99.0,
            "source": "last",
            "status": "OK",
            "fields": {"bid": 98.99, "ask": 99.01, "last": 99.0},
            "rth_open": True,
            "rth_status": {
                "is_open": True,
                "source": "contract_liquid_hours",
                "message": "RTH open",
                "checked_at": "2026-07-11T12:00:00+00:00",
                "liquid_hours": "20260711:0930-20260711:1600",
                "time_zone": "US/Eastern",
            },
        },
        "broker_connectivity": {
            "local_connected": True,
            "upstream_connected": True,
            "state": "connected",
            "message": "ready",
        },
        "trading_status": {"summary": "Ready", "blockers": []},
        "broker_recovery": {"open_app_orders": []},
        "recent_events": [],
        "history_summary": {},
    }
    snapshot.update(overrides)
    return snapshot


def test_gui_module_imports_and_declares_expected_stage_order(gui_module):
    assert gui_module.STAGE_ORDER == [stage.value for stage in (
        Stage.WAIT_INITIAL_DROP,
        Stage.BUY_TRAIL_ACTIVE,
        Stage.WAIT_RISE_TRIGGER,
        Stage.SELL_TRAIL_ACTIVE,
        Stage.CYCLE_COMPLETE,
    )]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Stage.WAIT_INITIAL_DROP, Stage.WAIT_INITIAL_DROP.value),
        (Stage.CYCLE_COMPLETE.value, Stage.CYCLE_COMPLETE.value),
        (None, ""),
    ],
)
def test_stage_value_normalizes_enums_strings_and_blanks(gui_module, value, expected):
    assert gui_module._stage_value(value) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("RTH guard: market is closed", True),
        ("ATR guard: waiting for warmup bars", True),
        ("RECOVERY REQUIRED: mismatch", False),
        ("", False),
    ],
)
def test_guard_message_classification(gui_module, message, expected):
    assert gui_module._is_expected_guard_or_timing_blocker(message) is expected


@pytest.mark.parametrize(
    ("stage", "message", "expected"),
    [
        (Stage.WAIT_RISE_TRIGGER.value, "Waiting for a higher price.", True),
        (Stage.BUY_TRAIL_ACTIVE.value, "Waiting for broker fill.", True),
        (Stage.ERROR.value, "Database corruption", False),
        (Stage.WAIT_INITIAL_DROP.value, None, False),
    ],
)
def test_strategy_wait_message_classification(gui_module, stage, message, expected):
    assert gui_module._is_expected_strategy_wait_message(stage, message) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Stop selected: strategy stopped locally; no broker order was cancelled or submitted.", True),
        ("Operator stopped cycle", True),
        ("Order rejected", False),
    ],
)
def test_operator_stop_message_classification(gui_module, message, expected):
    assert gui_module._is_expected_operator_stop_message(message) is expected


def test_blocking_cycle_message_ignores_expected_waits_and_returns_real_blocker(gui_module):
    assert gui_module._blocking_cycle_message(_cycle(error_message="Waiting for the initial drop trigger.")) == ""
    assert gui_module._blocking_cycle_message(_cycle(error_message="ATR guard: 3/14 bars")) == "ATR guard: 3/14 bars"
    assert gui_module._blocking_cycle_message(_cycle(error_message="Spread blocked by configured limit")) == "Spread blocked by configured limit"


def test_recovery_error_classification_distinguishes_handled_stop(gui_module):
    assert gui_module._is_handled_recovery_stop_message("Recovery stop marked manually handled") is True
    assert gui_module._is_real_recovery_error_message("Recovery required: open order mismatch") is True
    assert gui_module._is_real_recovery_error_message("Stop selected: strategy stopped locally") is False


@pytest.mark.parametrize(
    ("later", "earlier", "tolerance", "expected"),
    [
        ("2026-07-11T12:00:00+00:00", "2026-07-11T12:00:05+00:00", 2.0, True),
        ("2026-07-11T12:00:00+00:00", "2026-07-11T12:00:01+00:00", 2.0, False),
        ("bad", "2026-07-11T12:00:00+00:00", 0.0, False),
    ],
)
def test_timestamp_after_is_tolerant_and_fail_closed(gui_module, later, earlier, tolerance, expected):
    assert gui_module._timestamp_after(later, earlier, tolerance_seconds=tolerance) is expected


def _current_recovery_snapshot(*, checked_at: str = "2026-07-11T12:00:00+00:00") -> dict[str, Any]:
    cycle = _cycle(Stage.BUY_TRAIL_ACTIVE, buy_order_ref="IBKRBOT|AAPL|BUY", buy_status="Submitted")
    return {
        "connected": True,
        "active_cycle": cycle,
        "broker_recovery": {
            "checked_at": checked_at,
            "last_successful_checked_at": checked_at,
            "connected": True,
            "cycle_id": cycle["id"],
            "local_cycle_signature": recovery_cycle_signature(cycle),
            "open_app_orders": [],
        },
    }


def test_recovery_refresh_status_accepts_current_matching_probe_and_ignores_price_only_updates(gui_module):
    snapshot = _current_recovery_snapshot()
    status = gui_module._recovery_refresh_status(snapshot, now_timestamp="2026-07-11T12:00:30+00:00")
    assert status["state"] == "current"
    assert status["is_current"] is True

    snapshot["active_cycle"]["last_price"] = 101.25
    snapshot["active_cycle"]["updated_at"] = "2026-07-11T12:00:40+00:00"
    still_current = gui_module._recovery_refresh_status(snapshot, now_timestamp="2026-07-11T12:00:45+00:00")
    assert still_current["is_current"] is True


def test_recovery_refresh_status_fails_closed_for_age_cycle_order_and_failed_probe(gui_module):
    aged = _current_recovery_snapshot()
    status = gui_module._recovery_refresh_status(aged, now_timestamp="2026-07-11T12:01:01+00:00")
    assert status["state"] == "stale"
    assert "older than 60 seconds" in status["reason"]

    changed = _current_recovery_snapshot()
    changed["active_cycle"]["buy_status"] = "Filled"
    status = gui_module._recovery_refresh_status(changed, now_timestamp="2026-07-11T12:00:10+00:00")
    assert status["state"] == "stale"
    assert "Local order, fill, stage, or recovery facts changed" in status["reason"]

    different_cycle = _current_recovery_snapshot()
    different_cycle["active_cycle"]["id"] = "cycle-2"
    status = gui_module._recovery_refresh_status(different_cycle, now_timestamp="2026-07-11T12:00:10+00:00")
    assert status["state"] == "stale"
    assert "different local cycle" in status["reason"]

    later_order = _current_recovery_snapshot()
    later_order["broker_recovery"]["order_state_updated_at"] = "2026-07-11T12:00:02+00:00"
    status = gui_module._recovery_refresh_status(later_order, now_timestamp="2026-07-11T12:00:10+00:00")
    assert status["state"] == "stale"
    assert "broker order update" in status["reason"]

    invalidated = _current_recovery_snapshot()
    invalidated["broker_recovery"].update(
        invalidated_at="2026-07-11T12:00:03+00:00",
        invalidation_reason="The API disconnected after this probe.",
    )
    status = gui_module._recovery_refresh_status(invalidated, now_timestamp="2026-07-11T12:00:10+00:00")
    assert status["state"] == "stale"
    assert status["reason"] == "The API disconnected after this probe."

    failed = _current_recovery_snapshot()
    failed["broker_recovery"].update(
        checked_at="2026-07-11T12:00:20+00:00",
        last_successful_checked_at="2026-07-11T11:59:00+00:00",
        error="API disconnected",
    )
    status = gui_module._recovery_refresh_status(failed, now_timestamp="2026-07-11T12:00:30+00:00")
    assert status["state"] == "failed"
    assert status["last_successful_checked_at"] == "2026-07-11T11:59:00+00:00"


def test_recovery_refresh_click_gate_and_broker_action_handlers(gui_module):
    window = object.__new__(gui_module.MainWindow)
    window.current_snapshot = _current_recovery_snapshot(checked_at=datetime.now(timezone.utc).isoformat())
    window.controller = ControllerStub()

    assert window._recovery_refresh_is_current_or_warn("testing") is True
    window._recovery_sell_market_clicked()
    window._recovery_leave_orders_clicked()
    stop_actions = [args[0] for name, args, _kwargs in window.controller.calls if name == "request_stop"]
    assert stop_actions == [StopAction.SELL_APP_POSITION_MARKET, StopAction.LEAVE_ORDERS_WORKING]


def test_app_owned_unsold_quantity_never_returns_negative(gui_module):
    assert gui_module._app_owned_unsold_quantity(_cycle(buy_filled_qty=10, sell_filled_qty=3, protective_sell_filled_qty=2)) == pytest.approx(7.0)
    assert gui_module._app_owned_unsold_quantity(_cycle(buy_filled_qty=2, sell_filled_qty=5)) == 0.0
    assert gui_module._app_owned_unsold_quantity({}) == 0.0


def test_order_identity_and_terminal_time_helpers(gui_module):
    cycle = _cycle(
        sell_order_ref="IBKRBOT|AAPL|SELL",
        sell_order_id=12,
        sell_perm_id=34,
        sell_status="Filled",
        sell_filled_at="2026-07-11T12:04:00+00:00",
    )
    order = {"order_ref": "IBKRBOT|AAPL|SELL", "order_id": 12, "perm_id": 34}
    assert gui_module._order_matches_local_identity(order, cycle, "sell") is True
    assert gui_module._order_matches_local_identity({"order_id": 12}, cycle, "sell") is True
    assert gui_module._order_matches_local_identity({"perm_id": 999}, cycle, "sell") is False
    assert gui_module._local_terminal_order_time(cycle, order) == "2026-07-11T12:04:00+00:00"


def test_reconciled_open_orders_suppresses_stale_terminal_probe(gui_module):
    cycle = _cycle(
        Stage.CYCLE_COMPLETE,
        buy_filled_qty=10,
        sell_filled_qty=10,
        sell_order_ref="IBKRBOT|AAPL|SELL",
        sell_order_id=12,
        sell_perm_id=34,
        sell_status="Filled",
        sell_filled_at="2026-07-11T12:04:00+00:00",
    )
    order = {"order_ref": "IBKRBOT|AAPL|SELL", "status": "Submitted", "remaining": 10}
    visible, stale = gui_module._reconciled_open_app_orders({
        "active_cycle": cycle,
        "broker_recovery": {"checked_at": "2026-07-11T12:03:00+00:00", "open_app_orders": [order]},
    })
    assert visible == []
    assert stale == [order]


@pytest.mark.parametrize(
    ("kwargs", "enabled"),
    [
        ({"has_cycle": True, "recovery_required": True}, {"can_resume", "can_stop_cycle", "can_mark_manual"}),
        ({"has_cycle": True, "open_order_count": 1, "has_working_local_order": True}, {"can_cancel_order", "can_leave_orders"}),
        ({"has_cycle": True, "open_qty": 5.0}, {"can_market_close"}),
    ],
)
def test_recovery_action_permissions_enable_only_actions_supported_by_facts(gui_module, kwargs, enabled):
    defaults = {
        "has_cycle": False,
        "startup_resume_required": False,
        "startup_resume_only": False,
        "recovery_required": False,
        "action_state": "",
        "expected_non_recovery_wait": False,
        "open_order_count": 0,
        "has_working_local_order": False,
        "open_qty": 0.0,
        "terminal_safe_stage": False,
        "broker_refresh_current": True,
    }
    defaults.update(kwargs)
    permissions = gui_module._recovery_action_permissions(**defaults)
    for key in enabled:
        assert permissions[key] is True


def test_recovery_action_permissions_require_current_refresh_for_broker_side_effects(gui_module):
    facts = {
        "has_cycle": True,
        "startup_resume_required": False,
        "startup_resume_only": False,
        "recovery_required": True,
        "action_state": "risk",
        "expected_non_recovery_wait": False,
        "open_order_count": 1,
        "has_working_local_order": True,
        "open_qty": 5.0,
        "terminal_safe_stage": False,
    }
    stale = gui_module._recovery_action_permissions(**facts, broker_refresh_current=False)
    assert stale["resume_supported"] is True
    assert stale["cancel_supported"] is True
    assert stale["market_close_supported"] is True
    for key in ("can_resume", "can_cancel_order", "can_market_close", "can_leave_orders"):
        assert stale[key] is False
    assert stale["can_stop_cycle"] is True
    assert stale["can_mark_manual"] is True

    current = gui_module._recovery_action_permissions(**facts, broker_refresh_current=True)
    for key in ("can_resume", "can_cancel_order", "can_market_close", "can_leave_orders"):
        assert current[key] is True


def test_stage_formatting_and_indexing(gui_module):
    assert gui_module._stage_display_name(Stage.WAIT_INITIAL_DROP.value).startswith("1.")
    assert gui_module._stage_display_name("UNKNOWN") == "UNKNOWN"
    assert gui_module._stage_index(Stage.SELL_TRAIL_ACTIVE.value) == 4
    assert gui_module._stage_index("UNKNOWN") is None


def test_time_and_rth_formatting_helpers(gui_module):
    assert gui_module._format_utc_timestamp("2026-07-11T12:34:56+00:00").startswith("2026-07-11")
    assert gui_module._format_utc_timestamp("bad") == "bad"
    assert gui_module._human_duration(0) == "<1m"
    assert gui_module._human_duration(125) == "2m"
    zone, _ = gui_module._rth_zone("US/Eastern")
    assert zone is not None
    assert gui_module._parse_rth_endpoint("20260711:0930", "20260711", zone) is not None
    assert gui_module._parse_rth_endpoint("CLOSED", "20260711", zone) is None

    snapshot = _snapshot()["price_snapshot"]
    window = gui_module._rth_window_from_status(
        snapshot["rth_status"], snapshot["rth_status"]["checked_at"]
    )
    assert window is not None
    assert "RTH open" in gui_module._format_rth_status(snapshot, short=True)
    assert "Regular hours" in gui_module._format_rth_status(snapshot)
    assert isinstance(gui_module._current_time_status_text(), str)


def test_value_formatting_helpers_cover_empty_currency_percent_and_text(gui_module):
    assert gui_module._is_empty_value(None) is True
    assert gui_module._is_empty_value(float("nan")) is False
    assert gui_module._is_empty_value(0) is False
    assert gui_module._empty_display_for_label("Price") == "Not applicable in this stage"
    assert gui_module._semantic_state_for_text("Connected") == "success"
    assert gui_module._semantic_state_for_text("Error: rejected") == "risk"
    assert gui_module._looks_like_currency_label("Current price") is True
    assert gui_module._format_currency(12.3456, decimals=2) == "$12.35"
    assert gui_module._currency_decimals_for_label("commission") >= 2
    assert "$" in gui_module._format_field_value("Current price", 12.345)
    assert gui_module._format_field_value("Enabled", True) == "True"


def test_table_layout_helpers_accept_empty_and_populated_tables(gui_module):
    table = Dummy()
    table.setRowCount(3)
    table.setColumnCount(2)
    gui_module._polish_table_widget(table)
    gui_module._fit_table_height_to_rows(table, min_rows=1, max_visible_rows=5)
    gui_module._fit_table_height_to_all_rows(table)
    gui_module._auto_size_table_columns(table)
    gui_module._resize_table_columns_for_available_width(table)
    gui_module._cap_table_columns_for_horizontal_scroll(table)


def test_small_status_widgets_construct_and_update(gui_module):
    metric = gui_module.MetricCard("Price", "$0")
    metric.set_value("$100")
    ribbon = gui_module.StageRibbon()
    ribbon.set_stage(Stage.WAIT_RISE_TRIGGER.value)
    pill = gui_module.StatusPill("Connection")
    pill.set_value("Connected", "ready")
    pill.set_state("ok")
    status = gui_module.LiveStatusBar()
    status.set_input_lock_state(True)
    status.update_data(_snapshot())
    command = gui_module.CommandStepCard("Step", Dummy())
    command.set_state("done", "Complete")
    current = gui_module.CurrentStagePanel()
    current.update_data(_cycle(), _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL"))
    why = gui_module.WhyNotMovingPanel()
    why.update_data(_cycle(error_message="ATR warmup: waiting"), _snapshot()["price_snapshot"])


def test_timeline_scalar_helpers(gui_module):
    assert gui_module._format_price(1.23456).startswith("$1.234")
    assert gui_module._format_price(None) == "-"
    assert gui_module._pct_progress(50) == 50
    assert gui_module._float_or_none("1.5") == 1.5
    assert gui_module._float_or_none("bad") is None
    assert gui_module._parse_jsonish('{"a": 1}') == {"a": 1}
    assert gui_module._parse_jsonish("plain") == {}
    assert gui_module._parse_timestamp("2026-07-11T12:00:00+00:00") is not None
    assert gui_module._timeline_time({"timestamp": "2026-07-11T12:00:00+00:00"}, "timestamp") is not None
    assert gui_module._is_audit_risk_block_event({"event_type": "RISK_BLOCK", "message": "spread"}) is True
    assert gui_module._compact_text("x" * 50, 10).endswith("…")
    gui_module._draw_text_box(Dummy(), RectStub(), "text", Dummy(), Dummy())


def test_cycle_timeline_reconstructs_path_markers_transitions_and_positions(gui_module):
    row = _cycle(
        Stage.CYCLE_COMPLETE,
        buy_filled_qty=10,
        sell_filled_qty=10,
        avg_buy_price=98.5,
        avg_sell_price=102.0,
        buy_filled_at="2026-07-11T12:01:00+00:00",
        sell_filled_at="2026-07-11T12:04:00+00:00",
        net_pnl=35.0,
    )
    details = {
        "cycle": row,
        "market_capture_rows": [
            {"captured_at_utc": "2026-07-11T12:00:00+00:00", "price": 100.0},
            {"captured_at_utc": "2026-07-11T12:01:00+00:00", "price": 98.5},
            {"captured_at_utc": "2026-07-11T12:04:00+00:00", "price": 102.0},
        ],
        "orders": [{"action": "BUY", "created_at": "2026-07-11T12:00:30+00:00", "initial_stop_price": 98.5}],
        "executions": [{"side": "BOT", "time": "2026-07-11T12:01:00+00:00", "price": 98.5}],
        "decision_events": [{"event_type": "RISK_BLOCK", "timestamp": "2026-07-11T12:00:15+00:00", "message": "spread", "stage_before": Stage.WAIT_INITIAL_DROP.value, "stage_after": Stage.BUY_TRAIL_ACTIVE.value}],
    }
    widget = gui_module.CycleTimelineWidget(row, details)
    assert widget._cycle() == row
    assert widget.sizeHint().width() >= 0
    assert widget._first_order_time("BUY") is not None
    assert widget._build_price_path()
    assert widget._build_markers()
    assert widget._build_stage_transitions()
    assert widget._build_risk_blocks()
    assert widget._all_prices()
    assert widget._important_prices()
    assert widget._compute_axis_time_window() is not None
    assert widget.zoom_factor() >= 1.0
    widget.set_zoom(1.5)
    widget.reset_zoom()
    assert 0.0 <= widget._marker_position(widget._build_markers()[0]) <= 1.0
    assert widget._path_position_span()[0] <= widget._path_position_span()[1]
    assert isinstance(widget._format_axis_time(widget._axis_time_for_position(0.5)), str)
    widget._draw_small_label(Dummy(), 10, 10, "label", Dummy())
    widget._draw_marker_label(Dummy(), RectStub(), 10, 10, "marker", 0, [])
    widget._draw_hover_overlay(Dummy(), RectStub(), [], 90.0, 110.0)
    widget.paintEvent(Dummy())
    widget.mousePressEvent(SimpleNamespace(button=lambda: 0, position=lambda: PointStub(), accept=lambda: None))
    widget.mouseMoveEvent(SimpleNamespace(position=lambda: PointStub(), accept=lambda: None))
    widget.mouseReleaseEvent(Dummy())
    widget.leaveEvent(Dummy())
    widget.wheelEvent(SimpleNamespace(angleDelta=lambda: PointStub(0, 120), position=lambda: PointStub(), accept=lambda: None, ignore=lambda: None))


def test_profit_strategy_graph_and_flowchart_widgets_update_and_paint(gui_module):
    profit = gui_module.ProfitGuardWidget()
    profit.set_values(2.0, 1.0, 3.0, 1.0, reference_anchor=100.0, protective_sell_enabled=True, protective_sell_trail_pct=0.5)
    assert profit._pct_vs(101.0, 100.0) == "+1.00%"
    assert profit._onoff(True) == "ON"
    profit._draw_arrow(Dummy(), 10.0, 20.0, 30.0, Dummy())
    profit._draw_block(Dummy(), RectStub(), "block", "value", "small", Dummy())
    profit._draw_lane_label(Dummy(), 10.0, 20.0, "lane")
    profit._hover_pos = None
    profit._draw_hover_overlay(Dummy(), RectStub(), [], 90.0, 110.0)
    profit.paintEvent(Dummy())

    graph = gui_module.StrategyGraphWidget()
    graph.update_data(_cycle(), _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL"))
    graph.update_data(_cycle(), _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL"), repaint=False)
    graph._prune_history()
    assert isinstance(graph._levels(), list)
    graph._hover_pos = None
    graph._draw_hover_overlay(Dummy(), RectStub(), [], 90.0, 110.0)
    graph.paintEvent(Dummy())
    graph.mouseMoveEvent(SimpleNamespace(position=lambda: PointStub()))
    graph.leaveEvent(Dummy())

    flow = gui_module.StrategyFlowchartWidget()
    flow.update_data(_cycle(), _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL"))
    assert flow.update_data(_cycle(), _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL")) is False
    assert flow.sizeHint().height() >= 0
    flow.set_view_mode("Full strategy")
    flow.set_compact_mode(True)
    assert flow._active_stage_index() == 0
    assert flow._filtered_cards()
    assert flow._status_for_card(flow._filtered_cards()[0]) in {"Current", "Complete", "Pending"}
    flow._draw_round_box(Dummy(), RectStub(), Dummy(), Dummy())
    flow._draw_text(Dummy(), RectStub(), "text", Dummy())
    flow._draw_arrow(Dummy(), 10.0, 20.0, 30.0, Dummy())
    flow._hover_pos = None
    flow._draw_hover_overlay(Dummy(), RectStub(), [], 90.0, 110.0)
    flow.paintEvent(Dummy())


def test_panels_update_with_history_and_price_data(gui_module):
    flow_panel = gui_module.FlowchartPanel()
    flow_panel.resizeEvent(Dummy())
    flow_panel._sync_flowchart_canvas()
    flow_panel.set_compact_mode(True)
    assert flow_panel.history_combo.isVisible() is True

    historical_cycle = _cycle(
        Stage.CYCLE_COMPLETE,
        id="historical-cycle",
        ticker="MSFT",
        cycle_number=7,
    )
    flow_panel.set_history_rows([historical_cycle])
    assert flow_panel.history_combo.count() == 2
    flow_panel.history_combo.setCurrentIndex(1)
    flow_panel._redraw()

    active_cycle = _cycle(Stage.WAIT_RISE_TRIGGER, ticker="AAPL")
    flow_panel.update_data(active_cycle, _snapshot()["price_snapshot"], StrategySettings(ticker="AAPL"))
    assert flow_panel.history_combo.currentIndex() == 1
    assert flow_panel.flowchart._cycle["id"] == "historical-cycle"
    assert flow_panel._current_cycle["ticker"] == "AAPL"

    flow_panel.set_compact_mode(False)
    assert flow_panel.history_combo.isVisible() is True
    assert isinstance(flow_panel._strategy_from_history_row(_cycle()), StrategySettings)
    flow_panel._redraw()

    price_panel = gui_module.PricePanel()
    price_panel._set_raw_table_visible(True)
    price_panel.set_debug_mode(True)
    price_panel.update_data(_cycle(), _snapshot()["price_snapshot"])
    price_panel._update_summary_cards(_snapshot()["price_snapshot"])
    assert isinstance(price_panel._format_age(1.2), str)
    price_panel._update_api_indicator(_snapshot()["price_snapshot"])
    price_panel._update_progress(_cycle(), 99.0, _snapshot()["price_snapshot"])
    price_panel._update_field_table(_snapshot()["price_snapshot"])


def test_stop_dialog_and_cycle_audit_helpers(gui_module, tmp_path):
    dialog = gui_module.StopDialog(
        parent=None,
        show_tws_order_actions=True,
        open_order_count=1,
        show_position_close_action=True,
        unsold_quantity=3.0,
    )
    dialog._choose(gui_module.StopAction.CANCEL_OPEN_BOT_ORDERS)
    dialog._choose_exit_only()

    audit = object.__new__(gui_module.CycleAuditDialog)
    audit.row = _cycle(Stage.CYCLE_COMPLETE, net_pnl=10.0, buy_filled_qty=1, sell_filled_qty=1)
    audit.details = {"cycle": audit.row, "decision_events": []}
    audit._debug_capture_root = tmp_path
    enriched = gui_module.CycleAuditDialog._enriched_details(audit.row, audit.details)
    assert enriched["cycle"] == audit.row
    assert gui_module.CycleAuditDialog._capture_ids_from_decisions(audit.details) == set()
    capture_path = tmp_path / "AAPL" / "cycle_1" / "x.zip"
    assert gui_module.CycleAuditDialog._capture_path_matches_expected(
        capture_path,
        ticker="AAPL",
        cycle_number="1",
        cycle_id="cycle-1",
        capture_ids=set(),
    ) is True
    assert gui_module.CycleAuditDialog._capture_row_has_identity({"cycle_id": "cycle-1"}) is True
    assert gui_module.CycleAuditDialog._capture_row_matches_expected(
        {"cycle_id": "cycle-1"}, ticker="AAPL", cycle_number="1", cycle_id="cycle-1"
    ) is True
    assert gui_module.CycleAuditDialog._identifier_equal("1", 1) is True
    assert gui_module.CycleAuditDialog._capture_exact_cycle_folder(capture_path, "AAPL", 1) is True
    assert gui_module.CycleAuditDialog._market_capture_row_matches_cycle(
        {"cycle_id": "cycle-1"}, audit.row, audit.details
    ) is True
    assert gui_module.CycleAuditDialog._cycle_capture_time_window(audit.row, audit.details) is not None
    assert gui_module.CycleAuditDialog._outcome_badge(audit.row, audit.details) in {"PROFIT EXIT", "COMPLETED"}
    assert gui_module.CycleAuditDialog._money(12.3) == "$12.3000"
    assert isinstance(gui_module.CycleAuditDialog._format(audit.row, audit.details), str)
    assert "EXAMPLE" in gui_module.CycleAuditDialog._example_text(audit.row)

    gui_module.CycleAuditDialog._candidate_capture_files(audit.row, audit.details)
    gui_module.CycleAuditDialog._capture_manifest_or_path_matches_cycle(capture_path, {}, audit.row, audit.details)
    gui_module.CycleAuditDialog._capture_file_is_exact_cycle_match(capture_path, {}, audit.row, audit.details)
    rows, sources = gui_module.CycleAuditDialog._load_market_capture_rows(audit.row, audit.details)
    assert rows == [] and sources == []
    gui_module.CycleAuditDialog._timeline_tab(audit.row, audit.details)
    gui_module.CycleAuditDialog._scrollable_tab(Dummy())
    gui_module.CycleAuditDialog._summary_tab(audit.row, audit.details)
    gui_module.CycleAuditDialog._key_value_table([("a", 1)])
    gui_module.CycleAuditDialog._market_capture_tab(audit.row, audit.details)
    gui_module.CycleAuditDialog._records_table([{"a": 1}], [("A", "a")], "empty")


def test_main_window_constructs_and_exercises_command_recovery_and_history_paths(gui_module):
    controller = ControllerStub()
    window = gui_module.MainWindow(controller)
    assert controller.calls[:2] == [("start_thread", (), {}), ("refresh_history", (), {})]

    assert window._price_feed_group() is not None
    window._applying_snapshot_to_inputs = False
    window.profile_combo.clear()
    window.profile_combo.addItem(
        "Gateway live",
        {
            "key": "gateway_live",
            "platform": "gateway",
            "trading_mode": "live",
            "host": "127.0.0.1",
            "port": 4001,
        },
    )
    window.profile_combo.addItem("Custom", {"key": "custom"})
    window.platform_combo.clear()
    window.platform_combo.addItem("Gateway", "gateway")
    window.host_edit.setText("127.0.0.1")
    window.port_spin.setValue(4001)
    window._manual_trading_mode = "live"
    window._set_custom_profile_if_needed()
    window._on_profile_changed(0)
    window._on_profile_changed(1)
    window._on_platform_changed()
    window.con_id_edit.setText("123")
    window._clear_selected_contract_con_id()
    assert window.con_id_edit.text() == ""

    snapshot = _snapshot()
    window._on_connection_changed(True, "Connected")
    window._on_snapshot(snapshot)
    window._manual_input_lock_toggled(True)
    assert all(not button.isEnabled() for button in window.command_step_buttons.values())
    window._manual_input_lock_toggled(False)
    window._update_command_bar_states(snapshot)
    window._apply_view_mode()
    window._refresh_live_tab_layout()
    window._on_tab_changed(0)
    window._schedule_history_filter()
    window._run_visual_refresh()
    window.tabs.setCurrentIndex(1)
    window._on_tab_changed(1)
    window._on_snapshot(snapshot)
    window.tabs.setCurrentIndex(2)
    window._on_tab_changed(2)
    window._on_snapshot(snapshot)
    window.tabs.setCurrentIndex(0)
    window._on_tab_changed(0)

    window._recovery_export_bundle_clicked()
    window._recovery_resume_clicked()
    window._recovery_refresh_broker_clicked()
    window._recovery_cancel_app_order_clicked()
    window._recovery_mark_manual_clicked()
    window._update_recovery_panel(snapshot)

    # Exercise the full recovery comparison helpers with a locally expected and
    # broker-visible order, followed by an older/missing broker probe.
    buy_cycle = _cycle(
        Stage.BUY_TRAIL_ACTIVE,
        buy_order_ref="IBKRBOT|AAPL|BUY",
        buy_order_id=101,
        buy_perm_id=202,
        buy_status="Submitted",
        buy_filled_qty=0,
    )
    matching_order = {
        "order_ref": "IBKRBOT|AAPL|BUY",
        "order_id": 101,
        "perm_id": 202,
        "status": "Submitted",
        "filled": 0,
        "remaining": 10,
        "raw": {"action": "BUY", "orderType": "TRAIL", "trailingPercent": 1.0, "trailStopPrice": 98.0},
    }
    window._update_recovery_panel(
        _snapshot(
            Stage.BUY_TRAIL_ACTIVE,
            active_cycle=buy_cycle,
            broker_recovery={
                "checked_at": "2026-07-11T12:06:00+00:00",
                "connected": True,
                "open_app_orders": [matching_order],
            },
        )
    )
    window._update_recovery_panel(
        _snapshot(
            Stage.BUY_TRAIL_ACTIVE,
            active_cycle=buy_cycle,
            broker_recovery={
                "checked_at": "2026-07-11T12:00:00+00:00",
                "connected": True,
                "open_app_orders": [],
            },
        )
    )
    window._set_recovery_details_text_preserve_scroll("details")
    window._update_history_summary({"completed": 1, "net_pnl": 10.0})

    window._schedule_settings_autosave()
    window._strategy_visual_inputs_changed()
    window._update_strategy_previews()
    window._update_input_change_indicators(snapshot["active_cycle"])
    window._apply_profit_guard_bounds()
    assert window._strategy_map_reference()[0] > 0
    assert isinstance(window._risk_summary_for_map(), str)
    window._update_dynamic_graphs()
    window._autosave_settings()
    assert isinstance(window._connection_from_ui(), ConnectionSettings)
    assert isinstance(window._strategy_from_ui(), StrategySettings)

    window._connect_clicked()
    window._start_platform_clicked()
    window._browse_platform_path()
    window._search_ticker_clicked()
    window._on_ticker_search_results([{"label": "AAPL", "symbol": "AAPL", "supported": True, "con_id": 123}])
    window._selected_ticker_match()
    window._use_selected_ticker_match()
    window._confirm_ticker_price_clicked()
    window._start_clicked()
    assert window._visible_tws_open_app_orders() == []
    assert window._persisted_app_unsold_quantity(snapshot) == 0.0
    window._request_stop_action(gui_module.StopAction.STOP_NOW_NO_BROKER_ACTION)
    window._stop_clicked()

    window._apply_atr_adaptive_snapshot_to_inputs(snapshot)
    assert isinstance(window._atr_config_widgets(), list)
    window._set_atr_percentage_field_state(True)
    window._apply_snapshot_to_inputs(snapshot["connection"], snapshot["strategy"])
    assert window._is_active_stage(Stage.WAIT_INITIAL_DROP.value) is True
    window._set_widgets_enabled([Dummy()], True)
    window._update_input_locks(Stage.WAIT_INITIAL_DROP.value)
    window._update_metrics(snapshot["active_cycle"])
    window._update_price_feed(snapshot)
    window._update_event_log([{"created_at": "2026-07-11T12:00:00+00:00", "level": "INFO", "message": "event"}])

    history = [_cycle(Stage.CYCLE_COMPLETE, net_pnl=10.0, buy_filled_qty=1, sell_filled_qty=1)]
    assert window._format_history_value("net_pnl", 10.0).startswith("$")
    assert isinstance(window._history_hover_graph(history[0]), str)
    assert isinstance(window._history_tooltip(history[0]), str)
    assert window._example_history_row()["ticker"] == "EXAMPLE"
    window._visible_history_rows = history
    window._history_row_clicked(0, 0)
    assert window._history_outcome_badge(history[0])
    assert window._history_row_date(history[0])
    assert window._history_row_matches_filters(history[0]) is True
    window._all_history_rows = history
    window._visible_history_rows = []
    window.tabs.setCurrentIndex(0)
    window._apply_history_filters()
    assert window._history_table_refresh_pending is True
    assert window._flowchart_history_refresh_pending is True
    window.tabs.setCurrentIndex(2)
    window._on_tab_changed(2)
    assert window._history_table_refresh_pending is False
    window.tabs.setCurrentIndex(1)
    window._on_tab_changed(1)
    assert window._flowchart_history_refresh_pending is False
    window.tabs.setCurrentIndex(0)
    window._on_history(history)
    window._export_history()
    window.closeEvent(SimpleNamespace(ignore=lambda: None, accept=lambda: None))
    window._apply_styles()


def test_no_wheel_filter_blocks_wheel_only_for_edit_controls(gui_module):
    filter_object = gui_module.NoWheelEditFilter()
    watched = Dummy()
    assert filter_object.eventFilter(watched, EventStub(EventStub.Wheel)) in {True, False}
    assert filter_object.eventFilter(watched, EventStub(EventStub.FocusIn)) in {True, False}
