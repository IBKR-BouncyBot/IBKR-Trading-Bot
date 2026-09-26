"""Flowchart sizing contracts with deterministic font metrics, not render tests."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.flowchart_model import FlowchartStageCard
from app.models import Stage
from tests.support.qt_stubs import Dummy, RectStub, imported_gui_with_stubs


class FontProbe:
    def __init__(self, source):
        self.family = source.family
        self.point_size = source.point_size
        self.bold = source.bold

    def setPointSize(self, size):
        self.point_size = size

    def setBold(self, bold):
        self.bold = bold

    def toString(self):
        return f"{self.family},{self.point_size},{self.bold}"


class MetricsProbe:
    calls = []

    def __init__(self, font, device):
        self.font = font
        self.device = device

    def boundingRect(self, rect, flags, text):
        # A deterministic wrapping contract; this is deliberately not a claim
        # about the native font rasterizer or Windows scaling.
        scale = self.device.logicalDpiY() / 96.0
        character_width = self.font.point_size * 0.60 * scale
        line_height = self.font.point_size * 1.40 * scale
        columns = max(1, int(rect.width() / character_width))
        lines = sum(max(1, math.ceil(len(line) / columns)) for line in text.split("\n"))
        height = lines * line_height
        self.calls.append((text, rect.width(), self.font.point_size, self.font.bold, height))
        return RectStub(0, 0, rect.width(), height)


class FlowchartLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(Path(__file__).resolve().parents[1])
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.font = SimpleNamespace(family="Test font", point_size=10, bold=False)
        self.dpi = 96
        self.ratio = 1.0
        MetricsProbe.calls = []
        for name, value in (("QFont", FontProbe), ("QFontMetricsF", MetricsProbe)):
            patcher = patch.object(self.gui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.flow = object.__new__(self.gui.StrategyFlowchartWidget)
        Dummy.__init__(self.flow)
        self.flow.font = lambda: self.font
        self.flow.logicalDpiX = lambda: self.dpi
        self.flow.logicalDpiY = lambda: self.dpi
        self.flow.devicePixelRatioF = lambda: self.ratio
        self.flow._strategy = self.gui.StrategySettings()
        self.flow._cycle = None
        self.flow._price_snapshot = None
        self.flow._view_mode = "Full strategy"
        self.flow._compact_mode = False
        self.flow._text_height_cache = {}
        stages = (
            Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE,
            Stage.WAIT_RISE_TRIGGER, Stage.SELL_TRAIL_ACTIVE, Stage.CYCLE_COMPLETE,
        )
        self.flow._cards = [FlowchartStageCard(stage, f"Stage {index + 1}", "TRAIL", "Trigger", ("Details",)) for index, stage in enumerate(stages)]
        self.flow.resize(1120, 1580)

    def test_uniform_height_fits_largest_stage_and_removes_fixed_spare_height(self):
        self.flow._cards[1] = replace(self.flow._cards[1], details=tuple(f"Input {index}" for index in range(9)))
        card, header, body, _ = self.flow._card_layout()
        body_measurements = [call[4] for call in MetricsProbe.calls if call[2] == 9]
        self.assertEqual(body, math.ceil(max(body_measurements)) + 16)
        self.assertEqual(card, header + 12 + body + 12)
        self.assertLess(card, 272)

    def test_any_stage_can_determine_shared_height(self):
        before = self.flow._card_layout()[0]
        self.flow._cards[4] = replace(self.flow._cards[4], details=("Extra details\n" * 25,))
        full_height = self.flow._card_layout()[0]
        self.assertGreater(full_height, before)
        self.flow.set_view_mode("Entry path only")
        self.assertEqual(len(self.flow._filtered_cards()), 2)
        self.assertEqual(self.flow._card_layout()[0], full_height)

    def test_narrow_width_wraps_text_and_canvas_grows(self):
        self.flow._cards[1] = replace(self.flow._cards[1], details=("Long parameter description " * 20,))
        wide = self.flow._canvas_height(1600)
        narrow = self.flow._canvas_height(760)
        self.assertGreater(narrow, wide)
        self.assertEqual(self.flow._canvas_height(500), narrow)

    def test_repeated_layout_does_not_repeat_font_measurements(self):
        first = self.flow._card_layout()
        measured = len(MetricsProbe.calls)
        self.assertGreater(measured, 0)
        self.assertEqual(self.flow._card_layout(), first)
        self.assertEqual(len(MetricsProbe.calls), measured)

    def test_one_changed_paragraph_preserves_other_cached_measurements(self):
        self.flow._card_layout()
        measured = len(MetricsProbe.calls)
        self.flow._cards[1] = replace(self.flow._cards[1], trigger_summary="Updated trigger")
        self.flow._card_layout()
        self.assertEqual(len(MetricsProbe.calls), measured + 1)

    def test_width_font_dpi_and_pixel_ratio_invalidate_measurements(self):
        self.flow._card_layout()
        for change in (
            lambda: self.flow.resize(760, 1580),
            lambda: setattr(self.font, "family", "Another font"),
            lambda: setattr(self, "dpi", 144),
            lambda: setattr(self, "ratio", 2.0),
        ):
            before = len(MetricsProbe.calls)
            change()
            self.flow._card_layout()
            self.assertGreater(len(MetricsProbe.calls), before)

    def test_font_sizes_stay_unchanged_when_dpi_requires_taller_cards(self):
        original = self.flow._card_layout()[0]
        self.dpi = 192
        enlarged = self.flow._card_layout()[0]
        self.assertGreater(enlarged, original)
        self.assertEqual({call[2] for call in MetricsProbe.calls}, {9, 12})

    def test_cache_stays_bounded_as_live_values_change(self):
        for number in range(300):
            self.flow._wrapped_text_height(f"Current price {number}", 200, 9, False)
        self.assertLessEqual(len(self.flow._text_height_cache), 256)

    def test_paint_uses_identical_card_heights_and_fitting_text_rectangles(self):
        boxes = []
        texts = []
        self.flow._draw_round_box = lambda painter, rect, *args: boxes.append(rect)
        self.flow._draw_text = lambda painter, rect, text, color, size=9, bold=False, *args: texts.append((rect, text, size, bold))
        self.flow.paintEvent(Dummy())
        cards = [rect for rect in boxes if rect.top() >= 140 and rect.width() == self.flow.width() - 32]
        self.assertEqual(len(cards), 5)
        self.assertEqual({rect.height() for rect in cards}, {self.flow._card_layout()[0]})
        for rect, text, size, bold in texts:
            if rect.top() < 140 or text.endswith("/5"):
                continue
            required = self.flow._wrapped_text_height(text, rect.width(), size, bold)
            self.assertGreaterEqual(rect.height(), required)

    def test_panel_measures_target_width_before_resize(self):
        panel = object.__new__(self.gui.FlowchartPanel)
        Dummy.__init__(panel)
        panel.flowchart = self.flow
        viewport = Dummy()
        viewport.resize(900, 500)
        panel.scroll = SimpleNamespace(viewport=lambda: viewport, widget=lambda: self.flow, updateGeometry=lambda: None)
        actual = self.flow._canvas_height(896)
        self.flow._canvas_height = Mock(wraps=self.flow._canvas_height)
        panel._sync_flowchart_canvas()
        self.flow._canvas_height.assert_called_once_with(896)
        self.assertEqual((self.flow.width(), self.flow.height()), (896, actual))

    def test_resize_remeasures_width_changes_but_not_height_only_events(self):
        self.flow._refresh_canvas_size = Mock()
        event = SimpleNamespace(size=lambda: SimpleNamespace(width=lambda: 900), oldSize=lambda: SimpleNamespace(width=lambda: 1120))
        self.flow.resizeEvent(event)
        self.flow._refresh_canvas_size.assert_called_once()
        self.flow._refresh_canvas_size.reset_mock()
        event.oldSize = event.size
        self.flow.resizeEvent(event)
        self.flow._refresh_canvas_size.assert_not_called()

    def test_font_and_device_ratio_events_refresh_canvas_and_forward_event_result(self):
        events = SimpleNamespace(FontChange=1, ApplicationFontChange=2, DevicePixelRatioChange=3)
        self.flow._refresh_canvas_size = Mock()
        with patch.object(self.gui, "QEvent", events), patch.object(Dummy, "event", return_value=False, create=True) as parent_event:
            self.flow.changeEvent(SimpleNamespace(type=lambda: events.FontChange))
            self.flow.changeEvent(SimpleNamespace(type=lambda: events.ApplicationFontChange))
            self.assertEqual(self.flow._refresh_canvas_size.call_count, 2)
            event = SimpleNamespace(type=lambda: events.DevicePixelRatioChange)
            self.assertFalse(self.flow.event(event))
            parent_event.assert_called_once_with(event)
            self.assertEqual(self.flow._refresh_canvas_size.call_count, 3)


_NATIVE_PROBE = r'''
import sys
try:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QFont, QFontMetricsF
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        sys.exit(77)
    raise

from app.gui import FlowchartPanel
from app.models import StrategySettings

app = QApplication([])
panel = FlowchartPanel()
flow = panel.flowchart
boxes = []
texts = []
draw_box = flow._draw_round_box
draw_text = flow._draw_text

def record_box(painter, rect, *args):
    boxes.append(QRectF(rect))
    return draw_box(painter, rect, *args)

def record_text(painter, rect, text, color, point_size=9, bold=False, align=Qt.AlignLeft | Qt.AlignTop):
    texts.append((QRectF(rect), text, point_size, bold))
    return draw_text(painter, rect, text, color, point_size, bold, align)

flow._draw_round_box = record_box
flow._draw_text = record_text
panel.show()
settings = [
    StrategySettings(),
    StrategySettings(ticker="EXAMPLE", hard_risk_limits_enabled=True,
                     max_daily_loss_ticker=1000.0, max_daily_loss_total=5000.0,
                     max_cycles_per_ticker_day=100, max_consecutive_losses=10,
                     max_spread_pct=0.75, protective_sell_enabled=True,
                     slippage_buffer_enabled=True),
]
for strategy in settings:
    flow.update_data(None, {"price": 123.4567}, strategy)
    for width in (1640, 1120, 780, 1640):
        panel.resize(width, 800)
        app.processEvents()
        panel._sync_flowchart_canvas()
        app.processEvents()
        full_height = None
        for mode, count in (("Full strategy", 5), ("Entry path only", 2), ("Exit path only", 3), ("Full strategy", 5)):
            flow.set_view_mode(mode)
            panel._sync_flowchart_canvas()
            app.processEvents()
            expected_width = max(flow.MIN_CANVAS_WIDTH, panel.scroll.viewport().width() - 4)
            assert flow.width() == expected_width, (flow.width(), expected_width)
            assert flow.height() == flow._canvas_height(expected_width)
            geometry = (flow.width(), flow.height())
            for _ in range(3):
                panel._sync_flowchart_canvas()
                app.processEvents()
                assert (flow.width(), flow.height()) == geometry
            boxes.clear()
            texts.clear()
            image = flow.grab()
            assert not image.isNull()
            cards = [rect for rect in boxes if rect.top() >= 140 and abs(rect.width() - (flow.width() - 32)) < 0.1]
            assert len(cards) == count, (mode, len(cards))
            assert len({rect.height() for rect in cards}) == 1
            assert cards[-1].bottom() <= flow.height()
            if full_height is None:
                full_height = cards[0].height()
            assert cards[0].height() == full_height
            body_count = 0
            title_count = 0
            for rect, text, size, bold in texts:
                if rect.top() < 140 or text.endswith("/5"):
                    continue
                font = QFont(flow.font())
                font.setPointSize(size)
                font.setBold(bold)
                needed = QFontMetricsF(font, flow).boundingRect(
                    QRectF(0, 0, rect.width(), 1000000),
                    Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, text).height()
                assert needed <= rect.height() + 0.01, (width, mode, needed, rect.height(), text)
                assert any(rect.top() >= card.top() and rect.bottom() <= card.bottom()
                           and rect.left() >= card.left() and rect.right() <= card.right() for card in cards)
                if text.startswith("Stage ") and size == 12:
                    title_count += 1
                else:
                    assert size == 9
                    body_count += 1
            assert title_count == count
            assert body_count == count * 3
panel.close()
app.processEvents()
'''


class NativeFlowchartLayoutTests(unittest.TestCase):
    """Real Qt checks run on installed runtimes; missing PySide6 is an explicit skip."""

    def _run_native(self, scale):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale)
        result = subprocess.run(
            [sys.executable, "-c", _NATIVE_PROBE],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode == 77:
            self.skipTest("Native PySide6 is not installed; font rendering was not verified")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_flowchart_at_100_percent(self):
        self._run_native("1")

    def test_native_flowchart_at_150_percent(self):
        self._run_native("1.5")

    def test_native_flowchart_at_200_percent(self):
        self._run_native("2")


if __name__ == "__main__":
    unittest.main()
