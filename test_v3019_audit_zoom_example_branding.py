"""v3.0.19 audit, sample-data, branding, and stop-confirmation regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Stage, StopAction
from tests.support.qt_stubs import Dummy, RectStub, imported_gui_with_stubs


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(Path.cwd()) as module:
        yield module


class _TextMetrics:
    @staticmethod
    def horizontalAdvance(text: object) -> int:
        return max(1, len(str(text)) * 7)


class _RecordingPainter(Dummy):
    Antialiasing = 1
    instances: list[_RecordingPainter] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.texts: list[str] = []
        type(self).instances.append(self)

    def fontMetrics(self) -> _TextMetrics:
        return _TextMetrics()

    def drawText(self, *args: object) -> None:
        self.texts.append(str(args[-1]))


def _rendered_timeline_texts(gui_module, monkeypatch, timeline) -> list[str]:
    _RecordingPainter.instances.clear()
    monkeypatch.setattr(gui_module, "QPainter", _RecordingPainter)
    timeline.rect = lambda: RectStub(0, 0, 1200, 800)
    timeline.paintEvent(None)
    assert _RecordingPainter.instances
    return list(_RecordingPainter.instances[-1].texts)


def test_audit_summary_uses_only_the_app_actions_graph(gui_module, monkeypatch) -> None:
    row = gui_module.MainWindow._example_history_row()
    details = gui_module.MainWindow._example_audit_details(row)

    summary_timeline = gui_module.CycleTimelineWidget(
        row,
        details,
        compact=True,
        show_market_graph=False,
    )
    detailed_timeline = gui_module.CycleTimelineWidget(
        row,
        details,
        compact=False,
        show_market_graph=True,
    )

    assert summary_timeline.show_market_graph is False
    assert summary_timeline._axis_buckets[0] == []
    assert detailed_timeline.show_market_graph is True
    assert detailed_timeline._path_points
    assert detailed_timeline._axis_buckets[0] == detailed_timeline._path_points

    summary_texts = _rendered_timeline_texts(gui_module, monkeypatch, summary_timeline)
    detailed_texts = _rendered_timeline_texts(gui_module, monkeypatch, detailed_timeline)

    assert "App actions timeline per cycle" in summary_texts
    assert "App actions graph - orders, fills, stages and guards" in summary_texts
    assert "Market data graph - captured selected prices" not in summary_texts
    assert "Visual buy/sell timeline per cycle" in detailed_texts
    assert "Market data graph - captured selected prices" in detailed_texts
    assert "App actions graph - orders, fills, stages and guards" in detailed_texts

    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "compact=True, show_market_graph=False" in source
    assert "compact=False, show_market_graph=True" in source


def test_timeline_transition_labels_are_hover_only_and_not_drawn_below_graph(
    gui_module,
    monkeypatch,
) -> None:
    row = gui_module.MainWindow._example_history_row()
    details = gui_module.MainWindow._example_audit_details(row)
    timeline = gui_module.CycleTimelineWidget(
        row,
        details,
        compact=False,
        show_market_graph=True,
    )
    hover_texts: list[str] = []

    def capture_hover(_painter, _plot, targets, _minimum, _maximum) -> None:
        hover_texts.extend(str(target[2]) for target in targets)

    timeline._draw_hover_overlay = capture_hover
    rendered_texts = _rendered_timeline_texts(gui_module, monkeypatch, timeline)

    transition_event_types = {
        str(transition.get("event_type"))
        for transition in timeline._transitions
        if transition.get("event_type")
    }
    assert transition_event_types
    assert not any(
        event_type in rendered_text
        for event_type in transition_event_types
        for rendered_text in rendered_texts
    )
    assert sum(text.startswith("Stage transition\n") for text in hover_texts) == len(timeline._transitions)

    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "action_plot.bottom() + 8 + (idx % 2) * 31" not in source
    assert "Hover markers or dashed transition lines for details" in source


def test_audit_timeline_zoom_is_not_capped_at_six_times(gui_module) -> None:
    row = gui_module.MainWindow._example_history_row()
    details = gui_module.MainWindow._example_audit_details(row)
    timeline = gui_module.CycleTimelineWidget(row, details)

    timeline.set_zoom(25.0)
    assert timeline.zoom_factor() == pytest.approx(25.0)

    # An extreme finite request must remain safe and clamp only at Qt's absolute
    # maximum widget width, not at a product-defined multiplier.
    timeline.set_zoom(1e308)
    expected_qt_limit = gui_module.QT_WIDGET_SIZE_MAX / timeline._base_canvas_width
    assert timeline.zoom_factor() == pytest.approx(expected_qt_limit)

    previous = timeline.zoom_factor()
    timeline.set_zoom(float("inf"))
    assert timeline.zoom_factor() == previous

    timeline.set_zoom(0.25)
    assert timeline.zoom_factor() == pytest.approx(1.0)

    source = Path("app/gui.py").read_text(encoding="utf-8")
    assert "compact_timeline_scroll.setWidget(compact_timeline)" in source
    assert "compact_timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in source


def test_default_example_models_a_consistent_realistic_trade(gui_module) -> None:
    row = gui_module.MainWindow._example_history_row()
    details = gui_module.MainWindow._example_audit_details(row)

    assert row["__example"] is True
    assert row["ticker"] == "AAPL"
    assert row["trading_mode"] == "paper"
    assert row["protective_sell_enabled"] is True
    assert row["slippage_buffer_enabled"] is True
    assert row["hard_risk_limits_enabled"] is True

    quantity = int(row["buy_filled_qty"])
    assert quantity == int(row["sell_filled_qty"]) == 47
    expected_gross = (float(row["avg_sell_price"]) - float(row["avg_buy_price"])) * quantity
    expected_net = expected_gross - float(row["buy_commission"]) - float(row["sell_commission"])
    assert float(row["gross_pnl"]) == pytest.approx(expected_gross)
    assert float(row["net_pnl"]) == pytest.approx(expected_net)

    expected_drop = float(row["anchor_price"]) * (1.0 - float(row["initial_drop_pct"]) / 100.0)
    expected_rise = (
        float(row["avg_buy_price"])
        * (1.0 + float(row["rise_trigger_pct"]) / 100.0)
        / (1.0 - float(row["sell_trailing_stop_pct"]) / 100.0)
    )
    expected_protective = float(row["avg_buy_price"]) * (
        1.0 - float(row["protective_sell_trailing_stop_pct"]) / 100.0
    )
    assert float(row["drop_trigger_price"]) == pytest.approx(expected_drop, abs=0.0001)
    assert float(row["rise_trigger_price"]) == pytest.approx(expected_rise, abs=0.0001)
    assert float(row["protective_sell_initial_stop_price"]) == pytest.approx(expected_protective, abs=0.0001)

    assert [order["status"] for order in details["orders"]] == ["Filled", "Cancelled", "Filled"]
    assert sum(float(execution["shares"]) for execution in details["executions"] if "BUY" in execution["side"]) == quantity
    assert sum(float(execution["shares"]) for execution in details["executions"] if "SELL" in execution["side"]) == quantity
    assert len(details["decision_events"]) >= 9
    assert len(details["events"]) >= 7
    assert len(details["market_capture_rows"]) >= 25
    assert {capture["stage"] for capture in details["market_capture_rows"]} == {
        Stage.WAIT_INITIAL_DROP.value,
        Stage.BUY_TRAIL_ACTIVE.value,
        Stage.WAIT_RISE_TRIGGER.value,
        Stage.SELL_TRAIL_ACTIVE.value,
        Stage.CYCLE_COMPLETE.value,
    }

    log_text = gui_module.CycleAuditDialog._example_text(row, details)
    assert "BUILT-IN EXAMPLE CYCLE" in log_text
    assert "synthetic v3.1.0 paper-trading example data" in log_text
    assert "AAPL" in log_text
    assert "PROTECTIVE_SELL_SUBMITTED" in log_text
    assert "SELL_FILL" in log_text


def test_current_product_brand_is_consistent_and_internal_executable_name_is_stable() -> None:
    product_name = "BouncyBot - IBKR Portable Trading Bot"
    gui = Path("app/gui.py").read_text(encoding="utf-8")
    main = Path("main.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert f'{product_name} v3.1.0' in gui
    assert f'{product_name} already running' in main
    assert readme.startswith("# BouncyBot - an IBKR Portable Trading Bot \n")
    assert "**Current release: v3.1.0**" in readme
    assert f"{product_name} $version" in build
    assert 'name = "bouncybot-ibkr-portable-trading-bot"' in pyproject

    # Preserve the established executable/folder identifier so upgrades do not
    # unexpectedly break shortcuts, scripts, or portable-data migration steps.
    assert '$appName = "IBKRTradingBot"' in build


def test_stop_market_sell_requires_explicit_ok_confirmation(gui_module, monkeypatch) -> None:
    responses = [gui_module.QMessageBox.Cancel, gui_module.QMessageBox.Ok]
    calls: list[tuple[object, ...]] = []

    def question(*args: object) -> object:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(gui_module.QMessageBox, "question", staticmethod(question))
    dialog = gui_module.StopDialog(
        parent=None,
        show_tws_order_actions=True,
        open_order_count=1,
        show_position_close_action=True,
        unsold_quantity=47.0,
    )
    assert dialog.sell_market_btn.clicked.callbacks == [dialog._confirm_sell_market]

    dialog._confirm_sell_market()
    assert dialog.selected_action is None

    _parent, title, message, buttons, default_button = calls[0]
    assert title == "Confirm potential-loss market SELL"
    assert "all 47 app-bought unsold share(s)" in str(message)
    assert "may realize a loss" in str(message)
    assert "unrelated account positions are not included" in str(message)
    assert buttons == gui_module.QMessageBox.Ok | gui_module.QMessageBox.Cancel
    assert default_button == gui_module.QMessageBox.Cancel

    dialog._confirm_sell_market()
    assert dialog.selected_action == StopAction.SELL_APP_POSITION_MARKET
    assert len(calls) == 2
