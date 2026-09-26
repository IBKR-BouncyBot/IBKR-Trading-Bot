"""Reconciliation navigation remains independent of trading and input locks."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.support.qt_stubs import SignalStub, imported_gui_with_stubs

ROOT = Path(__file__).resolve().parents[1]


class TabsDouble:
    def __init__(self, pages):
        self.pages = list(pages)
        self.index = 0
        self.bar = Mock()
        self.currentChanged = SignalStub()
        self.corner = None

    def indexOf(self, page):
        return self.pages.index(page)

    def tabBar(self):
        return self.bar

    def setCornerWidget(self, widget, corner):
        self.corner = widget, corner

    def setCurrentIndex(self, index):
        if self.index != index:
            self.index = index
            self.currentChanged.emit(index)

    def currentIndex(self):
        return self.index


class ReconciliationNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = imported_gui_with_stubs(ROOT)
        cls.gui = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.pages = [object() for _ in range(4)]
        self.window = SimpleNamespace(
            tabs=TabsDouble(self.pages), recovery_tab=self.pages[3], current_snapshot={}, controller=Mock(),
        )
        self.window._open_reconciliation_tab = lambda: self.gui.MainWindow._open_reconciliation_tab(self.window)
        self.gui.MainWindow._place_reconciliation_tab_on_right(self.window)
        self.window.tabs.currentChanged.connect(lambda index: self.gui.MainWindow._on_tab_changed(self.window, index))
        self.gui.MainWindow._on_tab_changed(self.window, 0)

    def test_only_reconciliation_header_is_hidden_and_pages_keep_their_indices(self):
        self.window.tabs.bar.setTabVisible.assert_called_once_with(3, False)
        self.assertEqual(self.window.tabs.pages, self.pages)
        self.assertEqual(self.window.recovery_tab_index, 3)
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        self.assertEqual(self.window.tabs.corner, (self.window.recovery_corner_btn, self.gui.Qt.TopRightCorner))
        self.assertEqual(self.window.recovery_corner_btn.text(), "Reconciliation")

    def test_button_opens_reconciliation_without_broker_commands(self):
        self.window.recovery_corner_btn.clicked.emit()
        self.assertEqual(self.window.tabs.currentIndex(), 3)
        self.assertTrue(self.window.recovery_corner_btn.isChecked())
        self.assertTrue(self.window.recovery_corner_btn.property("activeRecovery"))
        self.assertEqual(self.window.controller.mock_calls, [])

    def test_programmatic_selection_and_each_return_update_selected_state(self):
        for index in range(3):
            with self.subTest(index=index):
                self.window.tabs.setCurrentIndex(3)
                self.assertTrue(self.window.recovery_corner_btn.isChecked())
                self.assertTrue(self.window.recovery_corner_btn.property("activeRecovery"))
                self.window.tabs.setCurrentIndex(index)
                self.assertFalse(self.window.recovery_corner_btn.isChecked())
                self.assertFalse(self.window.recovery_corner_btn.property("activeRecovery"))

    def test_repeated_click_keeps_current_page_selected(self):
        self.window.recovery_corner_btn.clicked.emit()
        # A native checkable button toggles off before emitting clicked.
        self.window.recovery_corner_btn.setChecked(False)
        self.window.recovery_corner_btn.clicked.emit()
        self.assertEqual(self.window.tabs.currentIndex(), 3)
        self.assertTrue(self.window.recovery_corner_btn.isChecked())
        self.assertTrue(self.window.recovery_corner_btn.property("activeRecovery"))

    def test_navigation_still_works_with_input_lock_engaged(self):
        self.window._manual_input_lock_enabled = True
        self.window.recovery_corner_btn.clicked.emit()
        self.assertEqual(self.window.tabs.currentIndex(), 3)
        self.assertTrue(self.window.recovery_corner_btn.isEnabled())
        self.assertEqual(self.window.controller.mock_calls, [])


class NativeReconciliationNavigationTests(unittest.TestCase):
    def test_native_selection_keyboard_and_corner_geometry(self):
        # A clean process prevents other tests' Qt doubles from passing this
        # native geometry test. It skips only if PySide6 is not installed.
        script = textwrap.dedent("""
            import sys
            try:
                from PySide6.QtCore import Qt
                from PySide6.QtTest import QTest
                from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget, QWidget
            except ModuleNotFoundError as exc:
                if exc.name == 'PySide6':
                    sys.exit(77)
                raise
            from app import gui

            class NavigationWindow(gui.MainWindow):
                def __init__(self):
                    QMainWindow.__init__(self)
                    self.tabs = QTabWidget()
                    self.setCentralWidget(self.tabs)
                    self.pages = [QWidget() for _ in range(4)]
                    for page, title in zip(self.pages, ('Live strategy', 'Strategy flowchart', 'Trade history', 'Reconciliation')):
                        self.tabs.addTab(page, title)
                    self.recovery_tab = self.pages[3]
                    self.current_snapshot = {}
                    self._place_reconciliation_tab_on_right()
                    self.tabs.currentChanged.connect(self._on_tab_changed)
                    self._on_tab_changed(0)

            app = QApplication([])
            app.setStyle('Fusion')
            window = NavigationWindow()
            for dark in (False, True):
                app.setProperty(gui.DARK_MODE_APP_PROPERTY, dark)
                window._apply_styles()
                for width in (520, 800, 1440):
                    window.resize(width, 400)
                    window.show()
                    app.processEvents()
                    tabs = window.tabs
                    button = window.recovery_corner_btn
                    bar = tabs.tabBar()
                    assert tabs.count() == 4
                    assert [tabs.widget(i) for i in range(4)] == window.pages
                    assert [bar.isTabVisible(i) for i in range(4)] == [True, True, True, False]
                    assert button.isVisible()
                    assert button.geometry().right() <= tabs.width()
                    assert bar.geometry().right() < button.geometry().left()
                    for index in range(3):
                        tabs.setCurrentIndex(index)
                        app.processEvents()
                        assert not button.isChecked()
                        assert not button.property('activeRecovery')
                        QTest.mouseClick(button, Qt.LeftButton)
                        app.processEvents()
                        assert tabs.currentIndex() == 3 and tabs.currentWidget() is window.recovery_tab
                        assert window.recovery_tab.isVisible()
                        assert button.isChecked() and button.property('activeRecovery')
                        QTest.mouseClick(button, Qt.LeftButton)
                        assert button.isChecked()
                    tabs.setCurrentIndex(0)
                    tabs.setCurrentIndex(3)
                    app.processEvents()
                    assert window.recovery_tab.isVisible() and button.isChecked()
                    tabs.setCurrentIndex(2)
                    QTest.keyClick(tabs, Qt.Key_Tab, Qt.ControlModifier)
                    assert tabs.currentIndex() == 0
                    button.setFocus()
                    QTest.keyClick(button, Qt.Key_Space)
                    assert tabs.currentIndex() == 3 and button.isChecked()
                    QTest.keyClick(tabs, Qt.Key_Tab, Qt.ControlModifier)
                    assert tabs.currentIndex() == 0 and not button.isChecked()
            window.hide()
            window.deleteLater()
            app.processEvents()
        """)
        for scale in ("1", "1.5", "2"):
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
                capture_output=True, text=True, timeout=60, check=False,
            )
            if result.returncode == 77:
                self.skipTest("Native PySide6 is not installed; Qt geometry was not exercised.")
            with self.subTest(scale=scale):
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
