"""GUI-only display and lock regressions; controller calls are recorded, not run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.models import Stage
from tests.support.qt_stubs import Dummy, imported_gui_with_stubs
from tests.test_v3012_minor_reliability_and_history_layout import _blocked_running_snapshot, _ControllerStub

ROOT = Path(__file__).resolve().parents[1]


class RecordingController(_ControllerStub):
    def __init__(self):
        super().__init__()
        self.calls = []

    def __getattr__(self, name):
        original = super().__getattr__(name)

        def call(*args, **kwargs):
            self.calls.append(name)
            return original(*args, **kwargs)

        return call


class RecordingLayout(Dummy):
    """Observe construction order without pretending to implement Qt geometry."""

    instances = []

    def __init__(self, parent=None):
        super().__init__()
        self.parent_widget = parent
        self.entries = []
        self.instances.append(self)

    def addWidget(self, widget, *args):
        self.entries.append(("widget", widget))

    def addLayout(self, layout, *args):
        self.entries.append(("layout", layout))


class RecordingPainter(Dummy):
    Antialiasing = 1

    def __init__(self, *args):
        super().__init__(*args)
        self.texts = []

    def drawText(self, *args):
        self.texts.append(str(args[-1]))


class LiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(ROOT)
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def make_window(self):
        controller = RecordingController()
        window = self.gui.MainWindow(controller)
        controller.calls.clear()
        return window, controller

    def test_missing_ticker_displays_na_without_exchange_currency_suffix(self):
        for snapshot in ({}, {"strategy": {"exchange": "SMART", "currency": "USD"}},
                         {"connected": True, "price_snapshot": {"contract": {"currency": "EUR"}}}):
            with self.subTest(snapshot=snapshot):
                before = deepcopy(snapshot)
                bar = self.gui.LiveStatusBar()
                bar.update_data(snapshot)
                self.assertEqual(bar.pills["Ticker"].value.text(), "N/A")
                self.assertEqual(bar.pills["Ticker"]._state, "waiting")
                self.assertEqual(snapshot, before)

    def test_confirmed_contract_precedes_retained_cycle_and_strategy_tickers(self):
        snapshot = {
            "strategy": {"ticker": "STRATEGY", "exchange": "SMART", "currency": "USD"},
            "price_snapshot": {"contract": {"ticker": "CONTRACT", "exchange": "IBIS2", "currency": "EUR"}},
            "active_cycle": {"ticker": "CYCLE", "exchange": "NASDAQ", "currency": "USD"},
        }
        bar = self.gui.LiveStatusBar()
        for expected in ("CONTRACT / IBIS2 / EUR", "CONTRACT / IBIS2 / EUR", "STRATEGY / SMART / USD"):
            with self.subTest(expected=expected):
                bar.update_data(snapshot)
                self.assertEqual(bar.pills["Ticker"].value.text(), expected)
                self.assertEqual(bar.pills["Ticker"]._state, "success")
                if "active_cycle" in snapshot:
                    del snapshot["active_cycle"]
                else:
                    snapshot["price_snapshot"] = {}

    def test_ticker_returns_to_na_after_context_is_cleared(self):
        bar = self.gui.LiveStatusBar()
        bar.update_data({"strategy": {"ticker": "AAPL"}})
        self.assertEqual(bar.pills["Ticker"].value.text(), "AAPL / SMART / USD")
        bar.update_data({})
        self.assertEqual(bar.pills["Ticker"].value.text(), "N/A")
        self.assertEqual(bar.pills["Ticker"]._state, "waiting")

    def test_embedded_graph_order_and_single_instance_in_all_modes(self):
        RecordingLayout.instances = []
        with (
            patch.object(self.gui, "QVBoxLayout", RecordingLayout),
            patch.object(self.gui, "QHBoxLayout", RecordingLayout),
            patch.object(self.gui, "QGridLayout", RecordingLayout),
            patch.object(self.gui, "StrategyGraphWidget", wraps=self.gui.StrategyGraphWidget) as graph_factory,
        ):
            window, controller = self.make_window()
        graph_factory.assert_called_once_with()
        root = next(layout for layout in RecordingLayout.instances if ("widget", window.price_panel) in layout.entries)
        panel = next(layout for layout in RecordingLayout.instances if layout.parent_widget is window.price_panel)
        outer = next(layout for layout in RecordingLayout.instances if layout.parent_widget is window.dashboard_tab)
        cards = list(window.price_panel.price_summary_cards.values())
        summary = next(layout for layout in RecordingLayout.instances if ("widget", cards[0]) in layout.entries)
        self.assertEqual(len(cards), 5)
        self.assertEqual(summary.entries, [("widget", card) for card in cards])
        for mode in ("Simple", "Advanced", "Debug", "Advanced"):
            with self.subTest(mode=mode):
                window.view_mode_combo.setCurrentText(mode)
                window._apply_view_mode()
                kind, configuration = root.entries[0]
                self.assertEqual(kind, "layout")
                self.assertEqual(configuration.entries, [("widget", window.connection_box), ("widget", window.strategy_box)])
                self.assertEqual(root.entries[1], ("widget", window.price_panel))
                kind, state = root.entries[2]
                self.assertEqual(kind, "layout")
                self.assertEqual(state.entries, [("widget", window.market_state_box),
                                                ("widget", window.order_state_box), ("widget", window.pnl_state_box)])
                self.assertIs(window.strategy_graph, window.price_panel.strategy_graph)
                self.assertNotIn("strategy_graph_box", vars(window))
                progress_index = panel.entries.index(("widget", window.price_panel.progress_bar))
                self.assertEqual(panel.entries[progress_index + 1], ("widget", window.strategy_graph))
                self.assertLess(progress_index + 1, panel.entries.index(("layout", summary)))
                graph_placements = [layout for layout in RecordingLayout.instances
                                    if ("widget", window.strategy_graph) in layout.entries]
                self.assertEqual(graph_placements, [panel])
                self.assertTrue(window.price_panel.isVisible())
                self.assertTrue(window.strategy_graph.isVisible())
                self.assertEqual(window.connection_box.isVisible(), mode != "Simple")
                self.assertEqual(window.strategy_box.isVisible(), mode != "Simple")
                self.assertEqual(outer.entries[-1], ("widget", window.command_bar))
        self.assertEqual(controller.calls, [])

    def test_graph_paints_prices_and_levels_without_explanatory_footer(self):
        for history_available in (False, True):
            with self.subTest(history_available=history_available):
                graph = self.gui.StrategyGraphWidget()
                cycle = {"id": "synthetic", "stage": Stage.WAIT_INITIAL_DROP.value,
                         "ticker": "AAPL", "anchor_price": 100.0, "drop_trigger_price": 98.0}
                snapshot = {"price": 99.0, "source": "Last"}
                graph.update_data(cycle, snapshot, None)
                if not history_available:
                    graph._history.clear()
                before = (deepcopy(cycle), deepcopy(snapshot), list(graph._history))
                painter = RecordingPainter()
                with patch.object(self.gui, "QPainter", return_value=painter) as factory:
                    factory.Antialiasing = RecordingPainter.Antialiasing
                    graph.paintEvent(None)
                self.assertIn("Market and strategy graph - AAPL", painter.texts)
                self.assertIn("Level list", painter.texts)
                self.assertIn("Anchor", painter.texts)
                self.assertIn("Initial drop trigger", painter.texts)
                self.assertEqual(any(text.startswith("Current ") for text in painter.texts), history_available)
                self.assertFalse(any("Rolling graph buffer" in text or "Native trailing-stop orders" in text
                                     for text in painter.texts))
                self.assertEqual((cycle, snapshot, list(graph._history)), before)

    @patch("app.gui.time.time", return_value=1000.0)
    def test_embedded_graph_receives_snapshots_once_and_retains_hidden_tab_history(self, _clock):
        window, controller = self.make_window()
        graph = window.price_panel.strategy_graph
        snapshot = _blocked_running_snapshot()
        window._on_snapshot(snapshot)
        graph._history.clear()
        controller.calls.clear()
        expected_prices = []
        for mode in ("Simple", "Advanced", "Debug"):
            window.view_mode_combo.setCurrentText(mode)
            window._apply_view_mode()
            for tab in (0, 1, 2, 3):
                with self.subTest(mode=mode, tab=tab):
                    window.tabs.setCurrentIndex(tab)
                    snapshot = _blocked_running_snapshot()
                    price = 100.0 + len(expected_prices)
                    snapshot["price_snapshot"]["price"] = price
                    before = deepcopy(snapshot)
                    expected_prices.append(price)
                    with (
                        patch.object(graph, "update_data", wraps=graph.update_data) as update_data,
                        patch.object(graph, "update") as repaint,
                    ):
                        window._on_snapshot(snapshot)
                    self.assertEqual(update_data.call_count, 1)
                    args, kwargs = update_data.call_args
                    self.assertEqual(args[0], snapshot["active_cycle"])
                    self.assertEqual(args[1], snapshot["price_snapshot"])
                    self.assertEqual(kwargs, {"repaint": tab == 0})
                    self.assertEqual(repaint.call_count, int(tab == 0))
                    self.assertEqual([value for _, value in graph._history], expected_prices)
                    self.assertEqual(snapshot, before)
                    self.assertIs(window.strategy_graph, graph)
        self.assertEqual(controller.calls, [])

    def test_lock_survives_tab_navigation_and_unlock_restores_current_button_gates(self):
        window, controller = self.make_window()
        for mode in ("Simple", "Advanced", "Debug"):
            with self.subTest(mode=mode):
                window.view_mode_combo.setCurrentText(mode)
                window._apply_view_mode()
                before = deepcopy(window.current_snapshot)
                unlocked_states = {name: button.isEnabled() for name, button in window.command_step_buttons.items()}
                window.live_status_bar.input_lock_btn.toggled.emit(True)
                self.assertFalse(window.command_bar.isVisible())
                self.assertTrue(window.live_status_bar.input_lock_btn.isEnabled())
                self.assertTrue(window.tabs.isEnabled())
                self.assertTrue(all(not button.isEnabled() for button in window.command_step_buttons.values()))
                for index in (1, 2, 3, 0):
                    window.tabs.setCurrentIndex(index)
                    window._on_tab_changed(index)
                    self.assertFalse(window.command_bar.isVisible())
                window._restore_interaction_state_after_theme_change()
                self.assertFalse(window.command_bar.isVisible())
                window.live_status_bar.input_lock_btn.toggled.emit(False)
                self.assertTrue(window.command_bar.isVisible())
                self.assertEqual({name: button.isEnabled() for name, button in window.command_step_buttons.items()}, unlocked_states)
                self.assertEqual(window.current_snapshot, before)
        self.assertEqual(controller.calls, [])

    def test_lock_and_unlock_do_not_mutate_active_cycle_or_submit_controller_commands(self):
        window, controller = self.make_window()
        for stage in (Stage.WAIT_INITIAL_DROP, Stage.BUY_TRAIL_ACTIVE, Stage.WAIT_RISE_TRIGGER,
                      Stage.SELL_TRAIL_ACTIVE, Stage.MANUAL_REVIEW):
            with self.subTest(stage=stage):
                snapshot = {
                    "connected": True, "broker_connectivity": {"local_connected": True, "upstream_connected": True},
                    "active_cycle": {"id": "synthetic", "ticker": "AAPL", "stage": stage.value, "last_price": 100.0},
                    "price_snapshot": {"price": 100.0},
                }
                window.current_snapshot = snapshot
                before = deepcopy(snapshot)
                window._update_command_bar_states(snapshot)
                unlocked_states = {name: button.isEnabled() for name, button in window.command_step_buttons.items()}
                window._manual_input_lock_toggled(True)
                window._manual_input_lock_toggled(False)
                self.assertEqual(snapshot, before)
                self.assertEqual({name: button.isEnabled() for name, button in window.command_step_buttons.items()}, unlocked_states)
        self.assertEqual(controller.calls, [])


NATIVE_PROBE = r'''
import json

from PySide6.QtWidgets import QApplication

from app.gui import MainWindow, StrategyGraphWidget, _apply_fusion_application_palette
from tests.test_v530_live_layout import RecordingController

app = QApplication([])
controller = RecordingController()
window = MainWindow(controller)
window._worker_watchdog_timer.stop()
window.resize(1800, 1100)
window.show()
controller.calls.clear()

def settle():
    for _ in range(3):
        app.processEvents()
        window.layout().activate()

results = []
for dark in (False, True):
    _apply_fusion_application_palette(app, dark)
    window.apply_system_theme(dark)
    for mode in ("Simple", "Advanced", "Debug"):
        window.tabs.setCurrentIndex(0)
        window.view_mode_combo.setCurrentText(mode)
        settle()
        scroll = window.dashboard_tab.layout().itemAt(0).widget()
        root = scroll.widget().layout()
        assert root.itemAt(0).layout().itemAt(0).widget() is window.connection_box
        assert root.itemAt(0).layout().itemAt(1).widget() is window.strategy_box
        assert root.itemAt(1).widget() is window.price_panel
        assert root.itemAt(2).layout().itemAt(0).widget() is window.market_state_box
        panel = window.price_panel
        assert panel.geometry().bottom() < window.market_state_box.geometry().top()
        assert window.connection_box.isVisible() == (mode != "Simple")
        assert window.strategy_box.isVisible() == (mode != "Simple")
        if mode != "Simple":
            assert window.connection_box.geometry().bottom() < panel.geometry().top()
            assert window.strategy_box.geometry().bottom() < panel.geometry().top()
        graph = window.strategy_graph
        assert graph is panel.strategy_graph
        assert graph.parentWidget() is panel
        assert window.findChildren(StrategyGraphWidget) == [graph]
        assert not hasattr(window, "strategy_graph_box")
        assert graph.isVisible()
        assert panel.layout().itemAt(2).widget() is panel.progress_bar
        assert panel.layout().itemAt(3).widget() is graph
        assert panel.progress_bar.geometry().bottom() < graph.geometry().top()
        assert len(panel.price_summary_cards) == 5
        for card in panel.price_summary_cards.values():
            assert card.parentWidget() is panel
            assert graph.geometry().bottom() < card.geometry().top()
        assert graph.height() >= 260
        assert window.dashboard_tab.isAncestorOf(window.command_bar)
        assert window.command_bar.isAncestorOf(window.view_mode_combo)
        unlocked_scroll_height = scroll.height()
        command_height = window.command_bar.height()
        assert window.command_bar.isVisible()
        assert window.view_mode_combo.isVisible()
        window.live_status_bar.input_lock_btn.setChecked(True)
        settle()
        assert not window.command_bar.isVisible()
        assert not window.view_mode_combo.isVisible()
        assert window.live_status_bar.input_lock_btn.isVisible()
        assert window.live_status_bar.input_lock_btn.isEnabled()
        assert scroll.height() >= unlocked_scroll_height + command_height
        for index in (1, 2, 3, 0):
            window.tabs.setCurrentIndex(index)
            settle()
            assert not window.command_bar.isVisible()
        window.apply_system_theme(not dark)
        settle()
        assert not window.command_bar.isVisible()
        window.live_status_bar.input_lock_btn.setChecked(False)
        settle()
        assert window.command_bar.isVisible()
        assert window.view_mode_combo.isVisible()
        assert window.command_bar.height() == command_height
        assert abs(scroll.height() - unlocked_scroll_height) <= 1
        assert window.command_bar.geometry().bottom() <= window.dashboard_tab.height()
        for index in (1, 2, 3):
            window.tabs.setCurrentIndex(index)
            settle()
            assert not window.command_bar.isVisible()
        window.tabs.setCurrentIndex(0)
        settle()
        assert window.command_bar.isVisible()
        results.append({"dark": dark, "mode": mode, "command_height": command_height})
assert controller.calls == [], controller.calls
window.hide()
window.deleteLater()
app.processEvents()
print(json.dumps(results))
'''


class NativeLiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [sys.executable, "-c", (
                "try:\n import PySide6.QtWidgets\n"
                "except ModuleNotFoundError as exc:\n"
                " if str(exc.name).startswith('PySide6'):\n  raise SystemExit(77)\n"
                " raise\n"
            )], capture_output=True, text=True, cwd=ROOT, timeout=30, check=False,
        )
        if result.returncode == 77:
            raise unittest.SkipTest("Native PySide6 is unavailable; layout geometry requires the real Qt engine.")
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def test_native_layout_and_lock_space_after_modes_tabs_themes_and_scaling(self):
        for scale in ("1", "1.5", "2"):
            with self.subTest(scale=scale):
                result = subprocess.run(
                    [sys.executable, "-c", NATIVE_PROBE],
                    capture_output=True, text=True, cwd=ROOT, timeout=60, check=False,
                    env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale, IBKR_BOT_AUTO_RESTART="0"),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = json.loads(result.stdout)
                self.assertEqual(len(rows), 6)
                self.assertTrue(all(row["command_height"] > 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
