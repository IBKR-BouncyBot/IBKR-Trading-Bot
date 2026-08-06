"""v3.9.0 audit-diagnostic coalescing and operator-status regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models import Stage
from app.strategy import StrategyEngine, make_order_ref
from tests.support.qt_stubs import imported_gui_with_stubs
from tests.test_v370_stage3_market_data_guard import _stage3_controller


def _capture_events(controller: Any) -> list[tuple[str, str, dict[str, Any] | None]]:
    events: list[tuple[str, str, dict[str, Any] | None]] = []

    def record(
        level: str,
        message: str,
        cycle: Any = None,
        *,
        raw: dict[str, Any] | None = None,
    ) -> None:
        events.append((str(level), str(message), raw))

    controller._log = record
    return events


def _guard_context(cycle: Any, *, bid: float = 100.0, trigger: float = 102.0) -> dict[str, Any]:
    return {
        "cycle_id": cycle.id,
        "ticker": cycle.ticker,
        "quote_kind": "live",
        "bid": bid,
        "ask": bid + 0.1,
        "reference_price": bid,
        "trigger_price": trigger,
        "trigger_distance_pct": ((bid - trigger) / trigger) * 100.0,
        "near_trigger": bid >= trigger * 0.995,
        "spread_pct": 0.1,
        "quote_age_seconds": 3.5,
        "bid_age_seconds": 3.5,
        "ask_age_seconds": 3.6,
        "max_quote_age_seconds": 3.0,
        "max_spread_pct": 0.5,
        "confirmation_count": 0,
        "confirmation_required": 2,
    }


def test_stage3_changing_age_text_is_coalesced_and_summarized(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    context = _guard_context(cycle)
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="STALE_ASK",
        message="The current live ask 3.6s exceeds the configured 3.0s maximum.",
        context=context,
    )
    clock["value"] = 1.0
    context = {**context, "ask_age_seconds": 4.1}
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="STALE_ASK",
        message="The current live ask 4.1s exceeds the configured 3.0s maximum.",
        context=context,
    )

    assert [level for level, _message, _raw in events] == ["INFO"]
    assert controller._audit_conditions[f"stage3_sell_quote_guard|{cycle.id}"]["occurrence_count"] == 2

    clock["value"] = 61.0
    context = {**context, "ask_age_seconds": 7.2}
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="STALE_ASK",
        message="The current live ask 7.2s exceeds the configured 3.0s maximum.",
        context=context,
    )

    assert [level for level, _message, _raw in events] == ["INFO", "WARN"]
    assert "Observed 3 times" in events[-1][1]
    assert "suppressed" in events[-1][1]
    assert events[-1][2] is not None
    assert events[-1][2]["diagnostic_condition"]["reason_code"] == "STALE_ASK"
    assert events[-1][2]["diagnostic_condition"]["suppressed_count"] == 1

    clock["value"] = 62.0
    controller._clear_stage3_quote_guard(
        cycle,
        state="waiting_below_trigger",
        message="The executable SELL bid remains below the rise trigger.",
        context=_guard_context(cycle),
    )
    assert [level for level, _message, _raw in events] == ["INFO", "WARN", "INFO"]
    assert "recovered" in events[-1][1]
    assert events[-1][2] is not None
    assert events[-1][2]["diagnostic_condition_recovery"]["suppressed_count"] == 1
    assert controller._stage3_quote_guard_status["reason_code"] == "TRIGGER_NOT_REACHED"


def test_stage3_brief_recurring_quote_gaps_do_not_reopen_audit_flood(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)
    context = _guard_context(cycle)

    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="STALE_ASK",
        message="The current live ask 3.6s exceeds the configured 3.0s maximum.",
        context=context,
    )
    clock["value"] = 1.0
    controller._clear_stage3_quote_guard(
        cycle,
        state="waiting_below_trigger",
        message="Fresh quote supervision is available again.",
        context=context,
    )
    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]

    # The same cycle can alternate between fresh and stale quotes many times.
    # The GUI still receives each current condition, but another brief episode
    # does not create a second entry/recovery pair in SQLite.
    clock["value"] = 2.0
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="STALE_ASK",
        message="The current live ask 3.8s exceeds the configured 3.0s maximum.",
        context={**context, "ask_age_seconds": 3.8},
    )
    clock["value"] = 3.0
    controller._clear_stage3_quote_guard(
        cycle,
        state="waiting_below_trigger",
        message="Fresh quote supervision is available again.",
        context=context,
    )

    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]



def test_stage3_nb_is_style_flood_is_reduced_to_bounded_summaries(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    for index in range(705):
        clock["value"] = index * (52.0 * 60.0 / 704.0)
        age = 3.1 + index / 100.0
        controller._observe_stage3_quote_guard(
            cycle,
            reason_code="STALE_ASK",
            message=(
                f"The current live ask {age:.1f}s exceeds the configured "
                "3.0s maximum."
            ),
            context={**_guard_context(cycle), "ask_age_seconds": age},
        )

    condition = controller._audit_conditions[f"stage3_sell_quote_guard|{cycle.id}"]
    assert condition["occurrence_count"] == 705
    # One INFO when the condition begins, one WARN after a minute, and then at
    # most one WARN per five minutes. The former implementation wrote 705 rows.
    assert len(events) <= 12
    assert [level for level, _message, _raw in events].count("INFO") == 1
    assert [level for level, _message, _raw in events].count("WARN") >= 10
    assert events[-1][2] is not None
    assert events[-1][2]["diagnostic_condition"]["suppressed_count"] > 680


def test_stage3_non_quote_callbacks_remain_gui_only(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    events = _capture_events(controller)

    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="NO_DISTINCT_UPDATE",
        message="No distinct market-data update is available for SELL confirmation.",
        context=_guard_context(cycle),
    )
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="NO_PRICE_FIELD_UPDATE",
        message="The latest market-data event did not update the live bid or ask.",
        context=_guard_context(cycle),
    )

    assert events == []
    assert controller._audit_conditions == {}
    assert controller._stage3_quote_guard_status["reason_code"] == "NO_PRICE_FIELD_UPDATE"


def test_stage3_near_trigger_invalid_quote_is_immediate_warning(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    events = _capture_events(controller)
    context = _guard_context(cycle, bid=101.60, trigger=102.0)

    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="SPREAD_TOO_WIDE",
        message="The current live spread 0.75% exceeds the configured maximum 0.50%.",
        context=context,
    )
    controller._observe_stage3_quote_guard(
        cycle,
        reason_code="SPREAD_TOO_WIDE",
        message="The current live spread 0.82% exceeds the configured maximum 0.50%.",
        context={**context, "spread_pct": 0.82},
    )

    assert [level for level, _message, _raw in events] == ["WARN"]
    assert "near or above the profit trigger" in events[0][1]


def test_native_order_wait_is_summary_only_until_operationally_relevant(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "SELL_TRAIL")
    controller.active_cycle = cycle
    controller.price_snapshot = {
        "native_order_trigger": {
            "active": True,
            "side": "SELL",
            "selected_price": 100.0,
            "raw_last_value": 100.0,
            "displayed_initial_stop": 101.0,
            "selected_crossed_displayed_initial_stop": False,
            "raw_last_crossed_displayed_initial_stop": False,
            "message": "Waiting for the broker-side Last trigger.",
        }
    }

    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    clock["value"] = 899.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert events == []

    clock["value"] = 901.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO"]
    assert "remained active" in events[0][1]

    clock["value"] = 902.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert len(events) == 1

    controller.price_snapshot["native_order_trigger"].update(
        {
            "selected_price": 101.2,
            "selected_crossed_displayed_initial_stop": True,
        }
    )
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]

    controller.price_snapshot["native_order_trigger"].update(
        {
            "raw_last_value": 101.2,
            "raw_last_crossed_displayed_initial_stop": True,
        }
    )
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO", "INFO", "WARN"]


def test_native_wait_escalation_restarts_summary_interval_without_duplicate_event(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "SELL_TRAIL")
    controller.active_cycle = cycle
    controller.price_snapshot = {
        "native_order_trigger": {
            "active": True,
            "side": "SELL",
            "selected_price": 100.0,
            "raw_last_value": 100.0,
            "displayed_initial_stop": 101.0,
            "selected_crossed_displayed_initial_stop": False,
            "raw_last_crossed_displayed_initial_stop": False,
            "message": "Waiting for the broker-side Last trigger.",
        }
    }
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")

    clock["value"] = 400.0
    controller.price_snapshot["native_order_trigger"].update(
        {
            "selected_price": 101.2,
            "selected_crossed_displayed_initial_stop": True,
        }
    )
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO"]

    key = f"native_trailing_anomaly|{cycle.id}|SELL|{cycle.sell_order_ref}"
    state = controller._audit_conditions[key]
    assert state["initial_delay_seconds"] == controller.DIAGNOSTIC_REPEAT_SUMMARY_SECONDS
    assert state["summary_level"] == "WARN"

    clock["value"] = 401.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO"]

    clock["value"] = 701.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO", "WARN"]


def test_native_wait_anomaly_recovery_restores_normal_info_cadence(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "SELL_TRAIL")
    controller.active_cycle = cycle
    controller.price_snapshot = {
        "native_order_trigger": {
            "active": True,
            "side": "SELL",
            "selected_price": 101.2,
            "raw_last_value": 100.0,
            "displayed_initial_stop": 101.0,
            "selected_crossed_displayed_initial_stop": True,
            "raw_last_crossed_displayed_initial_stop": False,
            "message": "Selected price crossed while raw Last did not.",
        }
    }

    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO"]

    clock["value"] = 1.0
    controller.price_snapshot["native_order_trigger"].update(
        {
            "selected_price": 100.0,
            "selected_crossed_displayed_initial_stop": False,
            "message": "Waiting for the broker-side Last trigger.",
        }
    )
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]
    anomaly_key = f"native_trailing_anomaly|{cycle.id}|SELL|{cycle.sell_order_ref}"
    wait_key = f"native_trailing_wait|{cycle.id}|SELL|{cycle.sell_order_ref}"
    assert anomaly_key not in controller._audit_conditions
    assert controller._audit_conditions[wait_key]["summary_level"] == "INFO"

    clock["value"] = 302.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    # The recovered anomaly must not leave behind a five-minute WARN summary.
    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]

    clock["value"] = 902.0
    controller._log_native_order_wait_diagnostic(cycle, "Submitted")
    assert [level for level, _message, _raw in events] == ["INFO", "INFO", "INFO"]


@pytest.mark.parametrize(
    ("message", "reason_code"),
    [
        ("Field-level market-data freshness is unavailable.", "FIELD_TRACKING_UNAVAILABLE"),
        ("No distinct market-data update is available for SELL confirmation.", "NO_DISTINCT_UPDATE"),
        ("IBKR upstream connectivity is not confirmed.", "UPSTREAM_UNCONFIRMED"),
        ("Market-data event identity is unavailable.", "EVENT_IDENTITY_UNAVAILABLE"),
        ("The current live bid/ask pair is incomplete.", "INCOMPLETE_QUOTE"),
        ("The current live bid/ask pair is crossed.", "CROSSED_QUOTE"),
        ("The current live bid/ask update time is unavailable.", "QUOTE_TIME_UNAVAILABLE"),
        (
            "The latest market-data event did not update the current live bid or ask, so it cannot count as a SELL confirmation.",
            "NO_PRICE_FIELD_UPDATE",
        ),
        ("The current live bid/ask pair is 4.0s old, above the configured 3.0s maximum.", "STALE_QUOTE"),
        ("Independent live bid/ask field timestamps are unavailable.", "FIELD_TIMESTAMPS_UNAVAILABLE"),
        ("The current live bid 3.5s and ask 3.7s exceed the configured 3.0s maximum.", "STALE_BID_ASK"),
        ("The current live bid 3.5s exceeds the configured 3.0s maximum.", "STALE_BID"),
        ("The current live ask 3.7s exceeds the configured 3.0s maximum.", "STALE_ASK"),
        ("The current live spread 0.75% exceeds the configured maximum 0.50%.", "SPREAD_TOO_WIDE"),
        ("The minimum-profit rise trigger is unavailable.", "TRIGGER_UNAVAILABLE"),
        ("The executable SELL bid 100.0000 does not confirm the rise trigger 102.0000.", "TRIGGER_NOT_REACHED"),
    ],
)
def test_stage3_guard_messages_map_to_stable_reason_codes(message, reason_code) -> None:
    from app.controller import TradingController

    assert TradingController._stage3_sell_guard_reason_code(message) == reason_code


def test_reconnect_attempts_are_aggregated_and_recovery_is_logged_once(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    controller._handle_broker_connection_problem("socket closed")
    assert [level for level, _message, _raw in events] == ["WARN"]

    for attempt, at in enumerate((10.0, 20.0, 30.0), start=1):
        clock["value"] = at
        controller._update_audit_condition(
            key="broker_reconnect",
            family="IBKR connection outage",
            reason_code="RECONNECT_ATTEMPT_FAILED",
            message=f"Reconnect failed on attempt {attempt}.",
            cycle=cycle,
            initial_delay_seconds=60.0,
            repeat_interval_seconds=300.0,
            summary_level="WARN",
            context={"attempt_count": attempt},
            kind="broker_reconnect",
        )
    assert len(events) == 1

    clock["value"] = 61.0
    controller._update_audit_condition(
        key="broker_reconnect",
        family="IBKR connection outage",
        reason_code="RECONNECT_ATTEMPT_FAILED",
        message="Reconnect failed on attempt 4.",
        cycle=cycle,
        initial_delay_seconds=60.0,
        repeat_interval_seconds=300.0,
        summary_level="WARN",
        context={"attempt_count": 4},
        kind="broker_reconnect",
    )
    assert [level for level, _message, _raw in events] == ["WARN", "INFO"]

    clock["value"] = 361.0
    controller._update_audit_condition(
        key="broker_reconnect",
        family="IBKR connection outage",
        reason_code="RECONNECT_ATTEMPT_FAILED",
        message="Reconnect failed on attempt 5.",
        cycle=cycle,
        initial_delay_seconds=60.0,
        repeat_interval_seconds=300.0,
        summary_level="WARN",
        first_summary_level="INFO",
        context={"attempt_count": 5},
        kind="broker_reconnect",
    )
    assert [level for level, _message, _raw in events] == ["WARN", "INFO", "WARN"]

    clock["value"] = 362.0
    controller._clear_audit_condition(
        "broker_reconnect",
        cycle=cycle,
        recovery_message="IBKR connectivity is restored after 5 failed attempts.",
    )
    assert [level for level, _message, _raw in events] == ["WARN", "INFO", "WARN", "INFO"]
    assert "5 failed attempts" in events[-1][1]


def test_buy_preflight_repeated_messages_are_bounded_and_clear_on_stage_change(tmp_path, monkeypatch) -> None:
    controller, _adapter, _cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    cycle = StrategyEngine.start_cycle(controller.strategy, 9, "DU_TEST", 100.0, 0.0)
    controller.active_cycle = cycle
    controller.storage.upsert_cycle(cycle)

    controller._apply_buy_preflight_block(cycle, "RTH closed; opens in 32 minutes.", "rth_closed")
    clock["value"] = 1.0
    controller._apply_buy_preflight_block(
        controller.active_cycle,
        "RTH closed; opens in 31 minutes.",
        "rth_closed",
    )
    assert [level for level, _message, _raw in events] == ["INFO"]

    clock["value"] = 61.0
    controller._apply_buy_preflight_block(
        controller.active_cycle,
        "RTH closed; opens in 30 minutes.",
        "rth_closed",
    )
    assert [level for level, _message, _raw in events] == ["INFO"]

    clock["value"] = 901.0
    controller._apply_buy_preflight_block(
        controller.active_cycle,
        "RTH closed; opens in 15 minutes.",
        "rth_closed",
    )
    assert [level for level, _message, _raw in events] == ["INFO", "INFO"]

    controller.active_cycle.stage = Stage.BUY_TRAIL_ACTIVE
    clock["value"] = 902.0
    controller._prune_inactive_audit_conditions()
    assert [level for level, _message, _raw in events] == ["INFO", "INFO", "INFO"]

    # A condition that begins as an expected wait can later become a hard
    # broker/data blocker. The hard blocker logs immediately and its first
    # persistence summary must also be WARN rather than retaining the earlier
    # informational summary policy.
    second = StrategyEngine.start_cycle(controller.strategy, 10, "DU_TEST", 100.0, 0.0)
    controller.active_cycle = second
    controller.storage.upsert_cycle(second)
    clock["value"] = 1000.0
    controller._apply_buy_preflight_block(second, "RTH closed.", "rth_closed")
    clock["value"] = 1010.0
    controller._apply_buy_preflight_block(
        controller.active_cycle,
        "IBKR upstream connectivity is unavailable.",
        "connectivity",
    )
    clock["value"] = 1071.0
    controller._apply_buy_preflight_block(
        controller.active_cycle,
        "IBKR upstream connectivity remains unavailable.",
        "connectivity",
    )
    assert [level for level, _message, _raw in events][-3:] == ["INFO", "WARN", "WARN"]


def test_stage4_unavailable_session_boundary_is_coalesced_and_recovers(tmp_path, monkeypatch) -> None:
    controller, _adapter, cycle = _stage3_controller(tmp_path, monkeypatch)
    controller_module = __import__(controller.__class__.__module__, fromlist=["time"])
    clock = {"value": 0.0}
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock["value"])
    events = _capture_events(controller)

    cycle.stage = Stage.SELL_TRAIL_ACTIVE
    cycle.sell_order_ref = make_order_ref(cycle.ticker, cycle.cycle_number, cycle.id, "SELL_TRAIL")
    cycle.cancel_sell_and_liquidate_before_close_enabled = True
    cycle.liquidate_before_close_minutes = 15
    controller.active_cycle = cycle
    monkeypatch.setattr(
        controller,
        "_session_minutes_from_rth_status",
        lambda: {"available": False, "message": "session metadata missing"},
    )

    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    clock["value"] = 1.0
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert [level for level, _message, _raw in events] == ["WARN"]

    clock["value"] = 61.0
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert [level for level, _message, _raw in events] == ["WARN", "WARN"]

    monkeypatch.setattr(
        controller,
        "_session_minutes_from_rth_status",
        lambda: {
            "available": True,
            "minutes_to_close": 120.0,
            "session_close_display": "16:00",
        },
    )
    clock["value"] = 62.0
    controller._cancel_sell_and_liquidate_before_close_if_needed(cycle)
    assert [level for level, _message, _raw in events] == ["WARN", "WARN", "INFO"]


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(Path.cwd()) as module:
        yield module


def test_price_monitor_displays_current_stage3_guard_without_audit_spam(gui_module) -> None:
    panel = gui_module.PricePanel()
    cycle = {
        "stage": Stage.WAIT_RISE_TRIGGER.value,
        "avg_buy_price": 100.0,
        "rise_trigger_price": 102.0,
        "ticker": "NBIS",
    }
    snapshot = {
        "price": 100.0,
        "status": "OK",
        "source": "marketPrice",
        "fields": {"bid": 99.95, "ask": 100.05},
        "stage3_sell_quote_status": {
            "state": "blocked",
            "reason_code": "STALE_ASK",
            "message": "The current live ask is older than the configured maximum.",
            "bid": 99.95,
            "trigger_price": 102.0,
            "trigger_distance_pct": -2.01,
            "occurrence_count": 143,
            "suppressed_count": 141,
        },
    }

    panel.update_data(cycle, snapshot)
    assert panel.stage3_guard_status.isVisible() is True
    text = panel.stage3_guard_status.text()
    assert "Stage 3 SELL evidence" in text
    assert "STALE_ASK" in text
    assert "trigger" in text

    panel._set_raw_table_visible(True)
    panel._update_field_table(snapshot)
    flattened = " ".join(
        str(panel.fields_table.item(row, column).text())
        for row in range(panel.fields_table.rowCount())
        for column in range(panel.fields_table.columnCount())
        if panel.fields_table.item(row, column) is not None
    )
    assert "Stage 3 SELL evidence reason" in flattened
    assert "suppressed audit rows" in flattened
