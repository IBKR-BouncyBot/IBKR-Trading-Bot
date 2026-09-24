"""Same-version GUI correction contracts; no broker orders are transmitted.

The layout doubles record explicit Qt policies. They complement, rather than
replace, the Windows/DPI visual checks in docs/TEST_PLAN.md.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models import Stage
from tests.support.qt_stubs import Dummy, PointStub, RectStub, imported_gui_with_stubs
from tests.test_comprehensive_gui_contracts import ControllerStub, _snapshot
from tests.test_v410_gui_metrics_and_layout import Layout, Painter, Table, buy_cycle

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gui():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


class ObservedLayout(Layout):
    def setContentsMargins(self, *values):
        self.margins = values

    def setSpacing(self, value):
        self.spacing = value


class Label(Dummy):
    def setMinimumWidth(self, value):
        self.minimum_width = value

    def setSizePolicy(self, *values):
        self.policy = values

    def setWordWrap(self, value):
        self.word_wrap = value

    def setAlignment(self, value):
        self.alignment = value


@pytest.mark.parametrize("dark", [False, True])
def test_equal_status_pill_contract_survives_large_details_and_theme(gui, monkeypatch, dark):
    monkeypatch.setattr(gui, "QHBoxLayout", ObservedLayout)
    monkeypatch.setattr(gui, "QVBoxLayout", ObservedLayout)
    monkeypatch.setattr(gui, "QLabel", Label)
    monkeypatch.setattr(gui.QSizePolicy, "Ignored", 37, raising=False)
    monkeypatch.setattr(gui, "_dark_mode_enabled", lambda: dark)
    monkeypatch.setattr(gui.StatusPill, "setSizePolicy", lambda self, *args: setattr(self, "policy", args))
    monkeypatch.setattr(gui.StatusPill, "setMinimumWidth", lambda self, value: setattr(self, "minimum_width", value))
    bar = gui.LiveStatusBar()
    entries = bar.test_layout.entries
    assert len(entries) == 11
    assert [args for _pill, args in entries[:10]] == [(1,)] * 10
    assert entries[-1] == (bar.input_lock_btn, (0,))
    for pill in bar.pills.values():
        assert pill.policy[0] == 37 and pill.minimum_width == 0
        assert pill.value.word_wrap and pill.value.minimum_width == 0
        assert pill.value.policy[0] == 37
    bar.update_data({"active_cycle": buy_cycle(rise_trigger_price=123456.789, avg_buy_price=12345.678),
                     "price_snapshot": {"price": 123456.0}, "connected": True})
    for name in ("Trading", "Position"):
        pill = bar.pills[name]
        before = pill.detail.text()
        pill.refresh_theme()
        assert pill.detail.text() == before
        assert pill.detail.policy[0] == 37 and pill.detail.minimum_width == 0
        assert pill.detail.word_wrap
    bar.pills["Trading"].set_value("A lengthy status message that must not widen just this box", "risk")
    assert bar.pills["Trading"]._state == "risk"
    assert bar.pills["Trading"].value.text().startswith("A lengthy status message")
    assert [args for _pill, args in entries[:10]] == [(1,)] * 10


def test_single_ribbon_is_in_fixed_shell_after_status_not_scroll_area(gui, monkeypatch):
    monkeypatch.setattr(gui, "QVBoxLayout", ObservedLayout)
    window = gui.MainWindow(ControllerStub())
    entries = window.shell_layout.entries
    assert [obj for obj, _args in entries[:3]] == [window.live_status_bar, window.stage_ribbon, window.tabs]
    source = (ROOT / "app/gui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    dashboard = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_build_dashboard")
    assert not any(isinstance(n, ast.Attribute) and n.attr == "stage_ribbon" for n in ast.walk(dashboard))
    assert source.count("self.stage_ribbon = StageRibbon()") == 1


@pytest.mark.parametrize("tab", [0, 1, 2, 3])
def test_fixed_ribbon_updates_while_each_tab_is_selected(gui, tab):
    controller = ControllerStub()
    window = gui.MainWindow(controller)
    window.tabs.setCurrentIndex(tab)
    for stage in (Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE, Stage.ERROR):
        window._on_snapshot(_snapshot(stage))
        assert window.stage_ribbon._last_stage == stage.value
    # Layout and snapshot presentation must not invoke trading actions.
    forbidden = {"start_strategy", "stop_strategy", "close_app_owned_position", "submit_order", "cancel_order"}
    assert not any(name in forbidden for name, _args, _kw in controller.calls)


@pytest.mark.parametrize("dark", [False, True])
def test_order_reference_has_explicit_card_style_not_log_style(gui, monkeypatch, dark):
    monkeypatch.setattr(gui, "_dark_mode_enabled", lambda: dark)
    monkeypatch.setattr(gui.MainWindow, "setStyleSheet", lambda self, css: setattr(self, "recorded_css", css))
    window = gui.MainWindow(ControllerStub())
    css = window.recorded_css
    rule = css.rsplit("QTextEdit#MetricValue {", 1)[1].split("}", 1)[0]
    card = css.split("QFrame#MetricCard {", 1)[1].split("}", 1)[0]
    background = next(line.strip() for line in card.splitlines() if "background-color:" in line)
    assert background in rule
    assert "border: none;" in rule and "padding: 0px;" in rule
    assert 'font-family: "Segoe UI", sans-serif;' in rule
    assert "Consolas" not in rule and "monospace" not in rule
    assert "Total buy cost" in window.metrics and "Total BUY cost" not in window.metrics
    ref = "IBKRBOT|NBIS|CYCLE-000039|" + "abcdef" * 35 + "|BUY_TRAIL"
    window._update_metrics(buy_cycle(buy_order_ref=ref))
    assert window.metrics["OrderRef"].value.toolTip() == ref


@pytest.mark.parametrize("snapshots", [[{}], [{"fields": {"bid": 101, "ask": 102}}] * 3])
def test_raw_api_value_columns_fill_width_after_each_refresh(gui, monkeypatch, snapshots):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui.QHeaderView, "Stretch", 41, raising=False)
    monkeypatch.setattr(gui.QHeaderView, "Interactive", 42, raising=False)
    panel = gui.PricePanel()
    for snapshot in snapshots:
        panel.update_data(None, snapshot)
        panel._set_raw_table_visible(True)
        panel._update_field_table(snapshot)
        assert panel.fields_table.columnCount() == 10
        assert [panel.fields_table.header.modes[i] for i in range(10)] == [42, 41] * 5
        panel._set_raw_table_visible(False)
        panel._set_raw_table_visible(True)
        assert [panel.fields_table.header.modes[i] for i in (1, 3, 5, 7, 9)] == [41] * 5


def test_compact_strategy_map_retains_all_sixteen_blocks_without_overlap(gui, monkeypatch):
    heights = []
    monkeypatch.setattr(gui.ProfitGuardWidget, "setMinimumHeight", lambda self, h: heights.append(h))
    painter = Painter()
    monkeypatch.setattr(gui, "QPainter", lambda _widget: painter)
    # QPainter's enum is accessed on the callable factory too.
    monkeypatch.setattr(gui.QPainter, "Antialiasing", 1, raising=False)
    widget = gui.ProfitGuardWidget()
    assert heights == [420]
    widget.rect = lambda: RectStub(0, 0, 1100, 420)
    blocks = []
    widget._draw_block = lambda _p, rect, title, value, small, color: blocks.append((rect, title, value, small))
    widget.paintEvent(Dummy())
    assert len(blocks) == 16
    assert [b[1] for b in blocks[::4]] == ["Anchor", "Buy reference", "Hard risk", "What-if"]
    for index in range(12):
        assert blocks[index][0].bottom() < blocks[index + 4][0].top()
    assert max(rect.bottom() for rect, *_ in blocks) < 390  # Footer starts here.
    assert any("Trailing values" in label for label in painter.labels)


def test_compact_map_text_rectangles_stay_inside_their_block(gui):
    widget = gui.ProfitGuardWidget()
    rect = RectStub(100, 60, 240, 72)
    painter = Painter()
    bounds = []
    painter.drawText = lambda r, flags, text: bounds.append((r, text))
    widget._draw_block(painter, rect, "Title", "$123.45", "two-line description", gui.QColor("#111111"))
    assert len(bounds) == 3
    for inner, _text in bounds:
        assert inner.left() >= rect.left() and inner.right() <= rect.right()
        assert inner.top() >= rect.top() and inner.bottom() <= rect.bottom()
    assert bounds[0][0].bottom() <= bounds[1][0].top()
    assert bounds[1][0].bottom() <= bounds[2][0].top()


def test_summary_average_immediately_follows_best_and_worst(gui, monkeypatch):
    monkeypatch.setattr(gui, "QGridLayout", ObservedLayout)
    window = gui.MainWindow(ControllerStub())
    titles = list(window.history_summary_cards)
    assert len(titles) == 12
    at = titles.index("Best net P/L")
    assert titles[at:at + 3] == ["Best net P/L", "Worst net P/L", "Average net P/L"]


@pytest.mark.parametrize("class_name", ["CycleTimelineWidget", "ProfitGuardWidget", "StrategyGraphWidget", "StrategyFlowchartWidget"])
def test_local_hover_methods_keep_their_independent_cursor_behavior(gui, class_name):
    cls = getattr(gui, class_name)
    widget = cls({}, {}) if class_name == "CycleTimelineWidget" else cls()
    widget._hover_pos = PointStub(100, 100)
    # Some non-timeline legacy overlays have no time mapping; providing one
    # verifies the formerly broken hover branch without changing production.
    widget._axis_time_for_position = lambda _fraction: None
    painter = Painter()
    widget._draw_hover_overlay(painter, RectStub(0, 0, 800, 200), [(120, 105, "local record")], 90, 110)
    assert len(painter.lines) == 2
    assert any("local record" in text for text in painter.labels)
    tree = ast.parse((ROOT / "app/gui.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    overlay = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "_draw_hover_overlay")
    assert not any(isinstance(n, ast.Attribute) and "linked" in n.attr for n in ast.walk(overlay))
    if class_name != "CycleTimelineWidget":
        assert not any(isinstance(n, ast.FunctionDef) and "linked" in n.name for n in node.body)


@pytest.mark.parametrize("kind", ["orders", "executions"])
@pytest.mark.parametrize("count", [0, 2, 500])
def test_audit_order_and_execution_tables_stretch_at_top(gui, monkeypatch, kind, count):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui, "QVBoxLayout", ObservedLayout)
    monkeypatch.setattr(gui.QHeaderView, "Stretch", 53, raising=False)
    dialog = object.__new__(gui.CycleAuditDialog)
    dialog.details = {kind: [{"order_ref": "IBKRBOT|TEST|CYCLE-000001|BUY_TRAIL", "quantity": i} for i in range(count)]}
    tab = getattr(dialog, f"_build_{kind}_tab")()
    table, _args = tab.test_layout.entries[0]
    assert isinstance(table, Table)
    assert table.header.modes[table.columnCount() - 1] == 53
    assert tab.test_layout.entries[-1] == (None, (1,))


@pytest.mark.parametrize("row_count,file_count", [(0, 0), (0, 1), (1, 1), (40, 2)])
def test_market_capture_top_alignment_and_full_width_with_or_without_captures(gui, monkeypatch, row_count, file_count):
    monkeypatch.setattr(gui, "QTableWidget", Table)
    monkeypatch.setattr(gui, "QVBoxLayout", ObservedLayout)
    monkeypatch.setattr(gui.QHeaderView, "Stretch", 67, raising=False)
    rows = [{"price": 100 + i, "captured_at_utc": f"2026-09-22T12:{i:02d}:00+00:00"} for i in range(row_count)]
    tab = gui.CycleAuditDialog._market_capture_tab(buy_cycle(), {
        "market_capture_rows": rows, "market_capture_files": [f"capture-{i}.zip" for i in range(file_count)],
    })
    layout = tab.test_layout
    assert layout.margins == (0, 0, 0, 0) and layout.spacing == 6
    summary, args = layout.entries[0]
    assert isinstance(summary, Table) and args == (0, gui.Qt.AlignTop)
    assert summary.header.modes[1] == 67
    assert summary.max_height <= 300
    if row_count:
        tables = [(obj, stretch) for obj, stretch in layout.entries if isinstance(obj, Table)]
        assert len(tables) == 2
        preview, stretch = tables[1]
        assert stretch == (2,)
        assert preview.min_height == 120 and preview.max_height == 16777215
        assert preview.header.modes[3] == 67
    else:
        assert layout.entries[-1] == (None, (1,))
    tree = ast.parse((ROOT / "app/gui.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CycleAuditDialog")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_market_capture_tab")
    assert not any(isinstance(n, ast.Name) and n.id == "QScrollArea" for n in ast.walk(method))
