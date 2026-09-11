"""v4.0.0 GUI changes with observable widgets, not broker side effects."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Stage
from app.order_edit_policy import NEXT_ORDER_QUOTE_GUARD_FIELDS, NEXT_ORDER_RISK_FIELDS
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs
from tests.test_comprehensive_gui_contracts import ControllerStub

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gui():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


class Font:
    def __init__(self):
        self.bold = False

    def setBold(self, value):
        self.bold = value


class Button(Dummy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default = None
        self.auto_default = None
        self.focus = False
        self.applied_font = Font()

    def font(self):
        return self.applied_font

    def setFont(self, value):
        self.applied_font = value

    def setDefault(self, value):
        self.default = value

    def setAutoDefault(self, value):
        self.auto_default = value

    def setFocus(self):
        self.focus = True


class Layout(Dummy):
    def __init__(self, parent):
        super().__init__()
        self.widgets = []
        parent.test_layout = self

    def addWidget(self, widget, *args):
        self.widgets.append(widget)


@pytest.mark.parametrize("open_orders", [False, True])
def test_exit_choices_come_before_sell_and_cancel_is_only_default(gui, monkeypatch, open_orders):
    monkeypatch.setattr(gui, "QPushButton", Button)
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    dialog = gui.StopDialog(
        show_tws_order_actions=open_orders, open_order_count=int(open_orders),
        show_position_close_action=True, unsold_quantity=100,
        exit_context=True, show_resume_later_exit_action=True,
    )
    widgets = dialog.test_layout.widgets
    assert widgets.index(dialog.exit_resume_later_btn) < widgets.index(dialog.sell_market_btn)
    assert widgets.index(dialog.after_btn) < widgets.index(dialog.sell_market_btn)
    assert widgets.index(dialog.close_btn) < widgets.index(dialog.sell_market_btn)
    assert "Optional market SELL" in widgets[-2].text()
    assert dialog.close_btn.text() == "Cancel"
    assert dialog.close_btn.default is True and dialog.close_btn.focus is True
    assert dialog.sell_market_btn.default is False
    assert dialog.sell_market_btn.auto_default is False
    assert dialog.exit_resume_later_btn.applied_font.bold is True
    assert all(widget.default is False for widget in widgets if isinstance(widget, Button) and widget is not dialog.close_btn)
    # The signal is connected to reject, never to a StopAction.
    dialog.close_btn.clicked.emit()
    assert dialog.selected_action is None and dialog.exit_app_after_action is False
    dialog._choose_exit_only()
    assert dialog.selected_action is None and dialog.exit_app_after_action is True


def test_idle_exit_dialog_still_has_no_liquidation_option(gui, monkeypatch):
    monkeypatch.setattr(gui, "QPushButton", Button)
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    dialog = gui.StopDialog(show_tws_order_actions=False, safe_to_exit=True)
    assert dialog.sell_market_btn not in dialog.test_layout.widgets
    assert dialog.exit_only_btn in dialog.test_layout.widgets
    assert dialog.close_btn.text() == "Cancel" and dialog.close_btn.default


def test_live_profile_is_amber_but_connection_failure_remains_red(gui):
    bar = gui.LiveStatusBar()
    bar.update_data({"connection": {"trading_mode": "live"}, "connected": False})
    assert bar.pills["Profile"]._state == "waiting"
    assert bar.pills["Connection"]._state == "risk"
    bar.update_data({"connection": {"trading_mode": "paper"}, "connected": False})
    assert bar.pills["Profile"]._state == "success"
    bar.pills["Profile"].refresh_theme()
    assert bar.pills["Profile"]._state == "success"


@pytest.mark.parametrize("stage", [Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE, Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE])
def test_reviewed_risk_widgets_can_be_edited_without_unlocking_order_inputs(gui, stage):
    controller = ControllerStub()
    window = gui.MainWindow(controller)
    window._update_input_locks(stage.value)
    for key in NEXT_ORDER_RISK_FIELDS:
        assert window._field_change_widgets[key].isEnabled(), key
        label, explanation = window._field_applicability(key, stage.value)
        assert label == ("Current cycle" if stage == Stage.WAIT_INITIAL_DROP else "Next order")
        if key not in NEXT_ORDER_QUOTE_GUARD_FIELDS and stage != Stage.WAIT_INITIAL_DROP:
            assert "next BUY" in explanation
    assert not window.ticker_edit.isEnabled()
    if stage in {Stage.BUY_TRAIL_ACTIVE, Stage.SELL_TRAIL_ACTIVE}:
        assert not window.investment_spin.isEnabled()
    if stage == Stage.SELL_TRAIL_ACTIVE:
        assert not window.sell_trail_spin.isEnabled()
        assert not window.cancel_sell_and_liquidate_before_close_check.isEnabled()
    # New editability must not punch through the top-bar accidental-input lock.
    window._manual_input_lock_enabled = True
    window._manual_input_lock_widget_ids = {id(window._field_change_widgets[key]) for key in NEXT_ORDER_RISK_FIELDS}
    window._update_input_locks(stage.value)
    assert all(not window._field_change_widgets[key].isEnabled() for key in NEXT_ORDER_RISK_FIELDS)


def test_green_banner_is_removed_but_profit_guard_inputs_and_graph_remain(gui):
    source = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
    assert 'self.profit_guard_label = QLabel' not in source
    window = gui.MainWindow(ControllerStub())
    bounds = {}
    window.rise_trigger_spin.setMinimum = lambda value: bounds.update(profit=value)
    window.sell_trail_spin.setMinimum = lambda value: bounds.update(trail=value)
    window._apply_profit_guard_bounds()
    assert window.profit_guard_graph is not None
    assert bounds["profit"] > 0
    assert bounds["trail"] == 0


def test_atr_status_distinguishes_saved_estimate_from_current_session_bars(gui):
    window = gui.MainWindow(ControllerStub())
    window._apply_atr_adaptive_snapshot_to_inputs({
        "strategy": {"atr_adaptive_enabled": True},
        "price_snapshot": {
            "atr_ready": True, "atr_pct": 2.0,
            "atr": {"ready": True, "seeded": True, "seed_observed_at": "2026-08-07T19:59:00+00:00", "live_bars_available": 1, "bars_required": 15},
        },
    })
    text = window.atr_status_label.text()
    assert "saved RTH estimate" in text and "2026-08-07" in text
    assert "current-session bars 1/15" in text
