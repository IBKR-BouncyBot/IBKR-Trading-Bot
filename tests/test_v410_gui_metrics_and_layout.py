"""Read-only v4.1.0 metrics and observable layout/crosshair regressions.

These are Qt contract doubles, not a native Windows pixel-layout test.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.models import Stage
from tests.support.qt_stubs import Dummy, PointStub, RectStub, imported_gui_with_stubs
from tests.test_comprehensive_gui_contracts import ControllerStub

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gui():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


class Layout(Dummy):
    def __init__(self, parent=None):
        super().__init__()
        self.entries = []
        self.stretches = {}
        if parent is not None:
            parent.test_layout = self

    def addWidget(self, widget, *args):
        self.entries.append((widget, args))

    def addLayout(self, layout, *args):
        self.entries.append((layout, args))

    def addStretch(self, stretch):
        self.entries.append((None, (stretch,)))

    def setColumnStretch(self, column, stretch):
        self.stretches[column] = stretch


class Header(Dummy):
    def __init__(self):
        super().__init__()
        self.modes = {}

    def setSectionResizeMode(self, *args):
        self.modes[args[0] if len(args) > 1 else "all"] = args[-1]

    def height(self):
        return 24


class Table(Dummy):
    def __init__(self, rows=0, columns=0, *args):
        super().__init__(*args)
        self.setRowCount(rows)
        self.setColumnCount(columns)
        self.headers = []
        self.header = Header()
        self.row_header = Header()
        self.min_height = 0
        self.max_height = 16777215
        self.min_history = []
        self.sizing = None
        self.hscroll = None
        self.vscroll = None

    def setHorizontalHeaderLabels(self, labels):
        self.headers = list(labels)

    def horizontalHeader(self):
        return self.header

    def verticalHeader(self):
        return self.row_header

    def setMinimumHeight(self, height):
        self.min_height = height
        self.min_history.append(height)

    def setMaximumHeight(self, height):
        self.max_height = height

    def setFixedHeight(self, height):
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)

    def setSizePolicy(self, *args):
        self.sizing = args

    def setHorizontalScrollBarPolicy(self, policy):
        self.hscroll = policy

    def setVerticalScrollBarPolicy(self, policy):
        self.vscroll = policy

    def frameWidth(self):
        return 1

    def rowHeight(self, _row):
        return 28


class TextArea(Dummy):
    def __init__(self, *args):
        super().__init__(*args)
        self.plain_text = ""
        self.plain_writes = 0
        self.read_only = False
        self.wrap = None

    def setReadOnly(self, value):
        self.read_only = value

    def setPlainText(self, value):
        self.plain_text = value
        self.plain_writes += 1

    def toPlainText(self):
        return self.plain_text

    def setWordWrapMode(self, mode):
        self.wrap = mode


class Painter(Dummy):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.labels = []

    def drawLine(self, a, b):
        self.lines.append((a.x(), a.y(), b.x(), b.y()))

    def drawText(self, *args):
        self.labels.append(str(args[-1]))


def buy_cycle(**overrides):
    cycle = {
        "id": "gui-only", "ticker": "NBIS", "currency": "USD",
        "stage": Stage.WAIT_RISE_TRIGGER.value, "cycle_number": 3,
        "buy_filled_qty": 40, "avg_buy_price": 100.25, "buy_commission": 2.5,
        "sell_filled_qty": 0, "protective_sell_filled_qty": 0,
        "rise_trigger_price": 103.5, "budget": 15000, "net_pnl": 15.0,
    }
    cycle.update(overrides)
    return cycle


@pytest.mark.parametrize("overrides,total,remaining", [
    ({}, 4012.5, 4012.5),
    ({"buy_filled_qty": 0, "avg_buy_price": None}, 0.0, 0.0),
    ({"buy_filled_qty": 2}, 203.0, 203.0),
    ({"sell_filled_qty": 10}, 4012.5, 3009.375),
    ({"protective_sell_filled_qty": 10}, 4012.5, 3009.375),
    ({"sell_filled_qty": 10, "protective_sell_filled_qty": 10}, 4012.5, 3009.375),
    ({"sell_filled_qty": 40}, 4012.5, 0.0),
    ({"sell_filled_qty": 50}, 4012.5, 0.0),
    ({"buy_commission": -0.5}, 4009.5, 4009.5),
    ({"buy_commission": 0}, 4010.0, 4010.0),
    ({"buy_commission": None}, None, None),
    ({"buy_commission": "bad"}, None, None),
    ({"avg_buy_price": None}, None, None),
    ({"avg_buy_price": 0}, None, None),
    ({"avg_buy_price": float("nan")}, None, None),
    ({"avg_buy_price": float("inf")}, None, None),
    ({"buy_filled_qty": -1}, None, None),
    ({"buy_filled_qty": "bad"}, None, None),
    ({"buy_filled_qty": 1e308, "avg_buy_price": 1e308}, None, None),
    ({"buy_commission": -10000}, None, None),
    ({"avg_buy_price": None, "sell_filled_qty": 40}, None, 0.0),
])
def test_actual_cost_is_fill_based_and_partial_exit_aware(gui, overrides, total, remaining):
    cycle = buy_cycle(**overrides)
    original = deepcopy(cycle)
    assert gui._cycle_purchase_cost(cycle) == total
    assert gui._cycle_purchase_cost(cycle, remaining_only=True) == remaining
    assert cycle == original


def test_cost_no_cycle_or_old_row_with_no_fee_does_not_invent_a_budget(gui):
    assert gui._cycle_purchase_cost(None) is None
    assert gui._cycle_purchase_cost({"budget": 9999}) is None
    row = buy_cycle()
    del row["buy_commission"]
    assert gui._cycle_purchase_cost(row) == 4010.0


@pytest.mark.parametrize("symbol", ["$", "\u20ac"])
def test_status_details_are_current_display_price_trigger_and_remaining_cost(gui, monkeypatch, symbol):
    monkeypatch.setattr(gui, "CURRENCY_SYMBOL", symbol)
    status = gui.LiveStatusBar()
    snapshot = {
        "connected": True,
        "active_cycle": buy_cycle(sell_filled_qty=10, protective_sell_filled_qty=10),
        "price_snapshot": {"price": 105.678, "fields": {"last": 999}, "subscription_market_data_type": 1},
    }
    original = deepcopy(snapshot)
    status.update_data(snapshot)
    assert len(status.pills) == 10
    assert {name for name, pill in status.pills.items() if pill.detail is not None} == {"Trading", "Position"}
    assert status.pills["Trading"].detail.text() == f"{symbol}105.68 / {symbol}103.50"
    assert "does not bypass" in status.pills["Trading"].detail.toolTip()
    assert status.pills["Position"].value.text() == "30 shares"
    assert status.pills["Position"].detail.text() == f"{symbol}3,009.38"
    assert snapshot == original
    for pill in status.pills.values():
        pill.refresh_theme()
    assert status.pills["Trading"].detail.text() == f"{symbol}105.68 / {symbol}103.50"
    status.update_data({})
    assert status.pills["Trading"].detail.text() == "- / -"
    assert status.pills["Position"].detail.text() == f"{symbol}0.00"
    status.pills["Data"].set_detail("unused")  # No detail widget on ordinary pills.


@pytest.mark.parametrize("price,trigger", [(None, None), (float("nan"), -2), (0, 0)])
def test_header_never_formats_invalid_prices_as_real_prices(gui, price, trigger):
    status = gui.LiveStatusBar()
    status.update_data({"active_cycle": buy_cycle(rise_trigger_price=trigger), "price_snapshot": {"price": price}})
    assert status.pills["Trading"].detail.text() == "- / -"


def test_merged_price_cards_keep_event_freshness_and_clock(gui, monkeypatch):
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    monkeypatch.setattr(gui, "QHBoxLayout", Layout)
    monkeypatch.setattr(gui, "QGridLayout", Layout)
    monkeypatch.setattr(gui, "_current_time_status_text", lambda: "UTC 12:00 / Local 14:00")
    panel = gui.PricePanel()
    assert list(panel.price_summary_cards) == ["Selected price", "Source", "Data mode", "Bid / Ask / Spread", "RTH status"]
    panel.update_data(None, {"price": 101, "subscription_market_data_type": 1,
                           "api_data_age_seconds": 4.5, "market_data_event_tracking": True,
                           "rth_open": True, "fields": {"bid": 100.99, "ask": 101.01}})
    assert panel.price_summary_cards["Data mode"].value.text() == "Live | 4.5s ago"
    assert "UTC 12:00 / Local 14:00" in panel.price_summary_cards["RTH status"].value.text()
    grids = [obj for obj, _args in panel.test_layout.entries if isinstance(obj, Layout) and obj.stretches]
    grid = grids[0]
    assert grid.stretches == {0: 1, 1: 1, 2: 1}
    positions = {card.title_text: args for card, args in grid.entries}
    assert positions["RTH status"] == (1, 1, 1, 2)
    assert positions["Data mode"] == (0, 2, 1, 1)
    panel._update_summary_cards({"market_data_event_tracking": True, "age_seconds": 0.1})
    assert "No actual update yet" in panel.price_summary_cards["Data mode"].value.text()
    panel._update_summary_cards({"age_seconds": 2})
    assert "2.0s ago" in panel.price_summary_cards["Data mode"].value.text()
    panel._set_raw_table_visible(True)
    assert panel.fields_table.isVisible() and panel.raw_api_toggle.text() == "Hide raw API fields"
    source = (ROOT / "app/gui.py").read_text(encoding="utf-8")
    assert "Raw IBKR fields remain available" not in source


def test_collapsible_diagnostics_and_simple_mode_remain_read_only(gui):
    controller = ControllerStub()
    window = gui.MainWindow(controller)
    before = list(controller.calls)
    assert "Stage" not in window.metrics
    assert not window.stage_details_container.isVisible()
    window.stage_details_toggle.toggled.emit(True)
    assert window.stage_details_container.isVisible()
    assert window.stage_details_toggle.text() == "Hide stage details"
    window.stage_details_toggle.toggled.emit(False)
    assert not window.stage_details_container.isVisible()
    assert window.stage_details_toggle.text() == "Show stage details"
    for mode, visible in [("Simple", False), ("Advanced", True), ("Debug", True), ("Simple", False)]:
        window.view_mode_combo.setCurrentIndex(["Simple", "Advanced", "Debug"].index(mode))
        window._apply_view_mode()
        assert window.event_log_box.isVisible() is visible
    assert controller.calls == before


def test_order_cost_location_and_reference_is_unmodified_copyable_plain_text(gui, monkeypatch):
    monkeypatch.setattr(gui, "QTextEdit", TextArea)
    window = gui.MainWindow(ControllerStub())
    names = list(window.metrics)
    assert names.index("Buy filled qty") + 1 == names.index("Total buy cost")
    assert names.index("Total buy cost") + 1 == names.index("Sell filled qty")
    ref = 'IBKRBOT|LONGTICKER|CYCLE-000003_' + "a" * 120 + '_SELL_TRAIL <not-html>'
    cycle = buy_cycle(buy_order_ref=ref)
    window._update_metrics(cycle)
    assert window.metrics["Total buy cost"].value.text() == "$4,012.50"
    card = window.metrics["OrderRef"]
    assert card.value.read_only and card.value.wrap == gui.QTextOption.WrapAnywhere
    assert card.value.toPlainText() == ref
    assert card.value.toolTip() == ref
    writes = card.value.plain_writes
    window._update_metrics(cycle)
    assert card.value.plain_writes == writes  # Do not destroy selection every snapshot.
    window._update_metrics(None)
    assert card.value.toPlainText() != ref


def test_history_has_twelve_cards_in_four_rows_of_three_and_mean_net_profit(gui, monkeypatch):
    monkeypatch.setattr(gui, "QGridLayout", Layout)
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    window = gui.MainWindow(ControllerStub())
    assert len(window.history_summary_cards) == 12
    box = next(obj for obj, _args in window.history_tab.test_layout.entries if obj is not None and obj.text() == "Completed trade summary")
    assert [args for _obj, args in box.test_layout.entries] == [(i // 3, i % 3) for i in range(12)]
    window._update_history_summary({"cycles": 4, "total_net_pnl": -10})
    assert window.history_summary_cards["Average net P/L"].value.text() == "-$2.5000"
    window._update_history_summary({"cycles": 0, "total_net_pnl": 0})
    assert window.history_summary_cards["Average net P/L"].value.text() == "-"
    window._update_history_summary({"cycles": 2, "total_net_pnl": None})
    assert window.history_summary_cards["Average net P/L"].value.text() == "-"


@pytest.mark.parametrize("row_count", [0, 1, 19, 500])
def test_history_many_rows_never_increase_viewport_minimum(gui, monkeypatch, row_count):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui.Qt, "ScrollBarAlwaysOn", 1)
    monkeypatch.setattr(gui.Qt, "ScrollBarAsNeeded", 2)
    window = gui.MainWindow(ControllerStub())
    table = window.history_table
    rows = [buy_cycle(id=f"row-{i}", cycle_number=i, buy_filled_qty=i + 1) for i in range(row_count)]
    original = deepcopy(rows)
    window._all_history_rows = rows
    minimums_before = list(table.min_history)
    window._apply_history_filters(force_table=True)
    assert table.min_height == 120 and table.max_height == 16777215
    assert table.min_history == minimums_before
    assert table.hscroll == 1 and table.vscroll == 2
    assert table.columnCount() == 29
    assert table.headers[13:16] == ["Budget", "Invested", "Reinvested"]
    assert table.rowCount() == row_count
    assert rows == original
    if row_count:
        cell = table.item(0, 14)
        assert cell.text() == "$102.75"
        assert cell._sort_value == 102.75
        assert cell.data(gui.Qt.UserRole) == 0
        assert "not the configured budget" in cell.toolTip()
    window._all_history_rows = []
    window._apply_history_filters(force_table=True)
    assert table.min_height == 120 and table.rowCount() == 0


def test_invested_column_sorts_numerically_and_does_not_change_row_identity(gui, monkeypatch):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    window = gui.MainWindow(ControllerStub())
    window._all_history_rows = [buy_cycle(id="large", buy_filled_qty=10), buy_cycle(id="small", buy_filled_qty=2)]
    window._apply_history_filters(force_table=True)
    large = window.history_table.item(0, 14)
    small = window.history_table.item(1, 14)
    assert small < large and not large < small
    assert large.data(gui.Qt.UserRole) == 0 and small.data(gui.Qt.UserRole) == 1


@pytest.mark.parametrize("events", [[], [{"event_type": "RISK_BLOCK", "message": "x", "stage_before": "3", "stage_after": "3"}] * 100])
def test_decision_tab_expands_with_zero_or_many_rows(gui, monkeypatch, events):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui.QSizePolicy, "Expanding", 10, raising=False)
    monkeypatch.setattr(gui.QHeaderView, "Stretch", 20, raising=False)
    dialog = object.__new__(gui.CycleAuditDialog)
    dialog.details = {"decision_events": events}
    # Build against the same plain-row input the background reader supplies.
    dialog._decision_rows = [
        ([gui._format_field_value(key, event.get(key))
          for key, _label in dialog._DECISION_COLUMNS], str(event.get("raw_json") or ""))
        for event in events
    ]
    dialog._audit_timer = gui.QTimer()
    table = dialog._build_decision_events_tab()
    assert isinstance(table, Table)  # No top-aligned shrinking wrapper.
    assert table.min_height == 120 and table.max_height == 16777215
    assert table.sizing == (10, 10)
    assert table.header.modes[7] == 20


def test_timeline_tables_have_identical_horizontal_edges_even_when_row_counts_differ(gui, monkeypatch):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    monkeypatch.setattr(gui, "QHBoxLayout", Layout)
    tab = gui.CycleAuditDialog._timeline_tab(buy_cycle(), {"events": [{"level": "WARN", "message": "spread risk"}] * 80})
    row_layout = tab.test_layout.entries[-1][0]
    tables = [(obj, args) for obj, args in row_layout.entries if isinstance(obj, Table)]
    assert len(tables) == 2
    assert [args for _obj, args in tables] == [(3,), (2,)]
    assert {(obj.min_height, obj.max_height) for obj, _args in tables} == {(210, 210)}


@pytest.mark.parametrize("source_index", [0, 1])
def test_local_overlay_only_draws_inside_the_hovered_plot(gui, source_index):
    widget = gui.CycleTimelineWidget({}, {})
    widget._axis_time_window = (1000.0, 2000.0)
    market = RectStub(10, 10, 800, 200)
    actions = RectStub(10, 300, 800, 200)
    plots = [(market, (90.0, 110.0), [(210, 60, "market record")]),
             (actions, (100.0, 200.0), [(260, 460, "real action")])]
    widget._hover_pos = PointStub(250, 75 if source_index == 0 else 375)
    painter = Painter()
    for plot, bounds, targets in plots:
        widget._draw_hover_overlay(painter, plot, targets, *bounds)
    source = plots[source_index][0]
    assert len(painter.lines) == 2
    assert painter.lines[0] == (250, source.top(), 250, source.bottom())
    assert painter.lines[1] == (source.left(), widget._hover_pos.y(), source.right(), widget._hover_pos.y())
    assert any(plots[source_index][2][0][2] in text for text in painter.labels)
    assert not any(plots[1 - source_index][2][0][2] in text for text in painter.labels)


def test_crosshair_clears_on_leave_and_outside_both_plots(gui):
    widget = gui.CycleTimelineWidget({}, {})
    plots = [RectStub(10, 10, 800, 200), RectStub(10, 300, 800, 200)]
    painter = Painter()
    for position in (None, PointStub(5, 5), PointStub(100, 250)):
        widget._hover_pos = position
        for plot in plots:
            widget._draw_hover_overlay(painter, plot, [], 90, 110)
    assert not painter.lines
    widget._hover_pos = PointStub(250, 375)
    widget.leaveEvent(Dummy())
    assert widget._hover_pos is None
    widget._draw_hover_overlay(painter, plots[1], [], 90, 110)
    assert not painter.lines


def test_independent_overlay_uses_local_cursor_and_record_tooltip(gui):
    widget = gui.CycleTimelineWidget({}, {})
    widget._axis_time_window = (1000.0, 2000.0)
    widget._hover_pos = PointStub(250, 100)
    painter = Painter()
    widget._draw_hover_overlay(
        painter, RectStub(0, 0, 800, 200), [(200, 100, "BUY fill $100.00\n12:00 UTC")], 90, 110,
    )
    assert len(painter.lines) == 2
    assert any("12:00 UTC" in text and "Cursor" in text for text in painter.labels)
    assert all("Linked time:" not in text and "Nearest record:" not in text for text in painter.labels)


@pytest.mark.parametrize("cursor_x,offscreen_x", [(0, -100), (800, 900)])
def test_independent_hover_filters_distant_offscreen_records(gui, cursor_x, offscreen_x):
    widget = gui.CycleTimelineWidget({}, {})
    widget._axis_time_window = (1000.0, 2000.0)
    widget._hover_pos = PointStub(cursor_x, 100)
    painter = Painter()
    widget._draw_hover_overlay(
        painter, RectStub(0, 0, 800, 200),
        [(offscreen_x, 100, "OFFSCREEN"), (400, 100, "VISIBLE RECORD")], 90, 110,
    )
    assert any("VISIBLE RECORD" in text for text in painter.labels)
    assert not any("OFFSCREEN" in text for text in painter.labels)
