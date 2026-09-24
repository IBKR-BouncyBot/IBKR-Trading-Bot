"""Status-row ordering and independent Timeline-hover regression tests.

Qt doubles record drawing calls here. Real rendering is a separate explicit
smoke check, never claimed on the basis of these tests alone.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.support.qt_stubs import Dummy, PointStub, RectStub, imported_gui_with_stubs
from tests.test_comprehensive_gui_contracts import ControllerStub
from tests.test_v410_gui_metrics_and_layout import Layout, Painter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gui():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


class RecordedPainter(Painter):
    def __init__(self):
        super().__init__()
        self.ellipses = []
        self.boxes = []

    def drawEllipse(self, point, rx, ry):
        self.ellipses.append((point.x(), point.y(), rx, ry))

    def drawRoundedRect(self, rect, *_radii):
        self.boxes.append(rect)


@pytest.mark.parametrize("view", ["Simple", "Advanced", "Debug"])
def test_status_above_stages_above_tabs_in_all_views(gui, monkeypatch, view):
    monkeypatch.setattr(gui, "QVBoxLayout", Layout)
    controller = ControllerStub()
    window = gui.MainWindow(controller)
    window.view_mode_combo.setCurrentText(view)
    window._apply_view_mode()
    for tab in range(4):
        window.tabs.setCurrentIndex(tab)
        assert [item for item, _ in window.shell_layout.entries[:3]] == [
            window.live_status_bar, window.stage_ribbon, window.tabs,
        ]
    assert len(window.live_status_bar.pills) == 10
    assert not any(name in {"start_strategy", "stop_strategy", "submit_order", "cancel_order"}
                   for name, _args, _kwargs in controller.calls)


def plots_and_targets():
    return [
        (RectStub(10, 10, 800, 200), [(210.0, 60.0, "Market data\n1970-01-01 00:18:00 UTC\n$100.00")], (90.0, 110.0)),
        (RectStub(10, 300, 800, 200), [(260.0, 460.0, "BUY fill\n1970-01-01 00:23:00 UTC\n$200.00")], (180.0, 250.0)),
    ]


def paint_pair(widget, painter, plots):
    for plot, targets, bounds in plots:
        widget._draw_hover_overlay(painter, plot, targets, *bounds)


@pytest.mark.parametrize("source_index", [0, 1])
@pytest.mark.parametrize("dark", [False, True])
def test_only_hovered_graph_draws_its_cursor_and_record(gui, monkeypatch, source_index, dark):
    monkeypatch.setattr(gui, "_dark_mode_enabled", lambda: dark)
    widget = gui.CycleTimelineWidget({}, {})
    widget._axis_time_window = (1000.0, 2000.0)
    plots = plots_and_targets()
    position = PointStub(250, 75 if source_index == 0 else 375)
    widget._hover_pos = position
    painter = RecordedPainter()
    paint_pair(widget, painter, plots)
    source, targets, _bounds = plots[source_index]
    other_targets = plots[1 - source_index][1]
    assert painter.lines == [
        (250, source.top(), 250, source.bottom()),
        (source.left(), position.y(), source.right(), position.y()),
    ]
    assert painter.ellipses == [(targets[0][0], targets[0][1], 4, 4)]
    assert len(painter.labels) == 1
    assert targets[0][2] in painter.labels[0]
    assert other_targets[0][2] not in painter.labels[0]
    assert "Linked time" not in painter.labels[0]
    assert widget._hover_pos is position


@pytest.mark.parametrize("position", [None, (5, 5), (250, 250), (900, 100), (250, 550)])
def test_leaving_both_plots_or_entering_gap_removes_both_guides(gui, position):
    widget = gui.CycleTimelineWidget({}, {})
    plots = plots_and_targets()
    widget._hover_pos = None if position is None else PointStub(*position)
    painter = RecordedPainter()
    paint_pair(widget, painter, plots)
    assert not painter.lines and not painter.labels
    widget._hover_pos = PointStub(250, 75)
    widget.leaveEvent(Dummy())
    paint_pair(widget, painter, plots)
    assert widget._hover_pos is None
    assert not painter.lines and not painter.labels


@pytest.mark.parametrize("source_index", [0, 1])
@pytest.mark.parametrize("width", [940, 1200, 4800])
def test_full_timeline_paint_keeps_other_graph_and_data_unchanged(gui, monkeypatch, source_index, width):
    row = gui.MainWindow._example_history_row()
    details = gui.MainWindow._example_audit_details(row)
    originals = deepcopy((row, details))
    widget = gui.CycleTimelineWidget(row, details)
    widget.rect = lambda: RectStub(0, 0, width, 650)
    painter = RecordedPainter()
    factory = lambda _widget: painter
    factory.Antialiasing = 1
    monkeypatch.setattr(gui, "QPainter", factory)
    plots = []
    original = gui.CycleTimelineWidget._draw_hover_overlay

    def record(self, p, plot, targets, minimum, maximum):
        plots.append(plot)
        original(self, p, plot, targets, minimum, maximum)

    monkeypatch.setattr(gui.CycleTimelineWidget, "_draw_hover_overlay", record)
    widget.paintEvent(Dummy())
    assert len(plots) == 2
    selected, counterpart = plots[source_index], plots[1 - source_index]
    baseline_lines = list(painter.lines)
    baseline_labels = list(painter.labels)
    baseline_ellipses = list(painter.ellipses)
    widget._hover_pos = PointStub(selected.left() + selected.width() * 0.52, selected.center().y())
    painter.lines.clear()
    painter.labels.clear()
    painter.ellipses.clear()
    widget.paintEvent(Dummy())
    added_lines = list(painter.lines)
    for line in baseline_lines:
        added_lines.remove(line)
    assert len(painter.lines) == len(baseline_lines) + 2
    assert len(added_lines) == 2
    assert all(selected.contains(PointStub(x, y)) for line in added_lines for x, y in (line[:2], line[2:]))
    assert not any(counterpart.contains(PointStub(x, y)) for line in added_lines for x, y in (line[:2], line[2:]))
    assert len(painter.labels) == len(baseline_labels) + 1
    assert len(painter.ellipses) == len(baseline_ellipses) + 1
    assert (row, details) == originals
    widget.leaveEvent(Dummy())
    painter.lines.clear()
    painter.labels.clear()
    painter.ellipses.clear()
    widget.paintEvent(Dummy())
    assert painter.lines == baseline_lines
    assert painter.labels == baseline_labels
    assert painter.ellipses == baseline_ellipses


@pytest.mark.parametrize("show_market_graph", [True, False])
def test_hover_help_matches_paired_or_summary_chart(gui, show_market_graph):
    widget = gui.CycleTimelineWidget({}, {}, show_market_graph=show_market_graph)
    help_text = widget.toolTip()
    assert "linked" not in help_text
    assert ("independent crosshairs" in help_text) is show_market_graph
    assert ("Hover for crosshairs." in help_text) is not show_market_graph
