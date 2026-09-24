"""Explicit real-PySide6 smoke check for the v4.1.0 GUI follow-up.

Run separately from the substitute-based suite:
    python scripts/check_v410_gui_real_qt.py --output-dir gui_smoke

Uses real widgets, mouse events and painters with an inert controller: no
Gateway connection, order, database write or worker thread is started. Missing
real GUI dependencies are a FAILED gate, not a skipped or passing test.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "gui_smoke")
    args = parser.parse_args()
    missing = [name for name in ("PySide6", "ib_async") if importlib.util.find_spec(name) is None]
    if missing:
        print("REAL-QT CHECK NOT RUN: missing " + ", ".join(missing))
        return 1
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))

    import PySide6
    import ib_async
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
    from PySide6.QtGui import QFont, QMouseEvent
    from PySide6.QtWidgets import QApplication, QLabel

    from app import gui
    from app.models import ConnectionSettings, StrategySettings

    if (not getattr(PySide6, "__file__", None) or not getattr(ib_async, "__file__", None)
            or QApplication.__module__ != "PySide6.QtWidgets"):
        raise RuntimeError("Real installed PySide6 is required; Qt stubs are not accepted.")
    print(f"Real PySide6 {PySide6.__version__}; ib_async {importlib.metadata.version('ib_async')}")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda *info: errors.append(info)

    class Signals(QObject):
        snapshot_updated = Signal(object)
        history_updated = Signal(object)
        connection_changed = Signal(bool, str)
        ticker_search_updated = Signal(object)

    class InertController:
        def __init__(self):
            self.connection = ConnectionSettings()
            self.strategy = StrategySettings(ticker="AAPL")
            self.signals = Signals()
            self.db_path = output / "unused.sqlite3"

        def start_thread(self):
            pass

        def refresh_history(self):
            pass

        def disconnect_tws(self):
            pass

        def save_draft_settings(self, _connection, _strategy):
            pass

    class TimelineProbe(gui.CycleTimelineWidget):
        def __init__(self, *values):
            self.hover_plots = []
            super().__init__(*values)

        def _draw_hover_overlay(self, painter, plot, targets, minimum, maximum):
            self.hover_plots.append(plot)
            super()._draw_hover_overlay(painter, plot, targets, minimum, maximum)

    def plot_image(image, plot):
        ratio = image.devicePixelRatio()
        return image.copy(round(plot.left() * ratio), round(plot.top() * ratio),
                          round(plot.width() * ratio), round(plot.height() * ratio))

    window = None
    timeline = None
    audit = None
    try:
        window = gui.MainWindow(InertController())
        # Do not run watchdog/autosave work against the inert test controller.
        for name in ("_worker_watchdog_timer", "_autosave_timer", "_visual_refresh_timer", "_history_filter_timer"):
            getattr(window, name).stop()
        row = gui.MainWindow._example_history_row()
        timeline = TimelineProbe(row, gui.MainWindow._example_audit_details(row))
        timeline.resize(1200, 650)
        for dark in (False, True):
            window.apply_system_theme(dark)
            window.show()
            timeline.show()
            app.processEvents()
            assert window.shell_layout.itemAt(0).widget() is window.live_status_bar
            assert window.shell_layout.itemAt(1).widget() is window.stage_ribbon
            assert window.live_status_bar.geometry().bottom() < window.stage_ribbon.geometry().top()
            assert len(window.live_status_bar.pills) == 10
            for name in ("Trading", "Position"):
                pill = window.live_status_bar.pills[name]
                assert pill.detail is not None
                assert pill.detail.geometry().top() == pill.title.geometry().top()
                assert pill.detail.geometry().left() > pill.title.geometry().right()
                assert pill.detail.alignment() == Qt.AlignRight | Qt.AlignTop
                assert pill.title.font() == pill.detail.font()
                assert pill.value.geometry().top() > max(pill.title.geometry().bottom(), pill.detail.geometry().bottom())
                assert pill.value.geometry().left() == pill.title.geometry().left()
                assert pill.value.geometry().right() == pill.detail.geometry().right()
            theme = "dark" if dark else "light"
            assert window.grab().save(str(output / f"header_{theme}.png"))
            timeline._hover_pos = None
            timeline.hover_plots.clear()
            baseline = timeline.grab().toImage()
            assert len(timeline.hover_plots) >= 2
            plots = timeline.hover_plots[-2:]
            for index, source in enumerate(plots):
                target = plots[1 - index]
                point = source.center()
                event = QMouseEvent(QEvent.MouseMove, point,
                                    QPointF(timeline.mapToGlobal(point.toPoint())),
                                    Qt.NoButton, Qt.NoButton, Qt.NoModifier)
                QApplication.sendEvent(timeline, event)
                app.processEvents()
                assert timeline._hover_pos is not None and source.contains(timeline._hover_pos)
                image = timeline.grab().toImage()
                assert plot_image(image, source) != plot_image(baseline, source), "Hovered graph did not draw its cursor"
                assert plot_image(image, target) == plot_image(baseline, target), "Other graph changed during independent hover"
                assert image.save(str(output / f"timeline_{theme}_{index}.png"))
            QApplication.sendEvent(timeline, QEvent(QEvent.Leave))
            assert timeline._hover_pos is None
            assert timeline.grab().toImage() == baseline

        # Exercise real wrapped row geometry and reachability. Font changes
        # complement a separate run with QT_SCALE_FACTOR=1.5 or 2 for DPI QA.
        window.tabs.setCurrentWidget(window.recovery_tab)
        window._update_recovery_panel({"active_cycle": row, "recovery_confidence": "local_state_only"})
        audit = gui.CycleAuditDialog(row, gui.MainWindow._example_audit_details(row))
        audit.show()
        summary = audit.tabs.widget(0).findChild(gui.ContentFitTable)
        assert summary is not None
        compare = window.recovery_compare_table
        assert compare.rowCount() == 8 and summary.rowCount() == 4
        guided = (window.recovery_resume_btn, window.recovery_stop_cycle_btn,
                  window.recovery_cancel_app_order_btn, window.recovery_mark_manual_btn)
        for point_size in (9, 12, 16):
            font = QFont(app.font())
            font.setPointSize(point_size)
            window.setFont(font)
            audit.setFont(font)
            compare.setFont(font)
            summary.setFont(font)
            for width in (1100, 1600):
                window.resize(width, 850)
                audit.resize(width, 850)
                for _ in range(8):
                    app.processEvents()
                for table in (compare, summary):
                    assert all(table.rowHeight(index) >= table.sizeHintForRow(index) for index in range(table.rowCount()))
                    assert table.horizontalScrollBar().maximum() == 0
                assert sum(compare.rowHeight(index) for index in range(8)) <= compare.viewport().height()
                assert compare.verticalScrollBar().maximum() == 0
                assert guided[0].geometry().top() == guided[1].geometry().top()
                assert guided[2].geometry().top() == guided[3].geometry().top()
                assert guided[0].geometry().bottom() < guided[2].geometry().top()
                for button in (*guided, window.recovery_sell_market_btn, window.recovery_leave_orders_btn):
                    window.recovery_scroll.ensureWidgetVisible(button)
                    app.processEvents()
                    top_left = button.mapTo(window.recovery_scroll.viewport(), QPoint(0, 0))
                    assert top_left.y() >= 0
                    assert top_left.y() + button.height() <= window.recovery_scroll.viewport().height()
                advanced = window.recovery_sell_market_btn.parentWidget()
                for label in advanced.findChildren(QLabel):
                    if label.hasHeightForWidth():
                        assert label.height() >= label.heightForWidth(label.width())
                    assert label.geometry().bottom() <= advanced.contentsRect().bottom()
                summary.verticalScrollBar().setValue(summary.verticalScrollBar().maximum())
                app.processEvents()
                last_item = summary.item(summary.rowCount() - 1, 0)
                last_rect = summary.visualItemRect(last_item)
                assert last_rect.bottom() <= summary.viewport().rect().bottom()
                if sum(summary.rowHeight(index) for index in range(summary.rowCount())) > summary.viewport().height():
                    assert summary.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
                    assert summary.verticalScrollBar().maximum() > 0
                window.recovery_scroll.verticalScrollBar().setValue(0)
                assert window.grab().save(str(output / f"reconciliation_{width}_{point_size}pt.png"))
                assert audit.grab().save(str(output / f"summary_{width}_{point_size}pt.png"))
        if errors:
            raise RuntimeError(f"Qt emitted {len(errors)} uncaught exception(s): {errors[0][1]}")
        print("REAL-QT CHECK PASSED: header details, independent hover, light/dark rendering, wrapped Reconciliation/Summary tables across widths/fonts.")
        print(f"Screenshots: {output}")
        return 0
    finally:
        sys.excepthook = original_hook
        for widget in (audit, timeline, window):
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
