"""Windows GUI entry point and process-level safety setup.

The entry point creates the Qt application, applies the stable palette, acquires
the portable-folder single-instance lock, constructs the controller/window, and
releases process resources on shutdown. Trading decisions remain in the strategy
and controller layers.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory

from app.controller import TradingController
from app.gui import MainWindow
from app.lockfile import SingleInstanceError, SingleInstanceLock


def _force_light_palette(app: QApplication) -> None:
    """Apply a stable light palette independent of the Windows color theme."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f6f7f9"))
    palette.setColor(QPalette.WindowText, QColor("#111827"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f3f4f6"))
    palette.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipText, QColor("#111827"))
    palette.setColor(QPalette.Text, QColor("#111827"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#111827"))
    palette.setColor(QPalette.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.PlaceholderText, QColor("#6b7280"))
    app.setPalette(palette)


def _install_session_shutdown_hook(app: QApplication, window: MainWindow) -> None:
    """Connect Qt session management to the window's durable shutdown save."""
    signal = getattr(app, "commitDataRequest", None)
    connect = getattr(signal, "connect", None)
    if not callable(connect):
        return
    direct_connection = getattr(
        getattr(Qt, "ConnectionType", Qt),
        "DirectConnection",
        getattr(Qt, "DirectConnection", None),
    )
    try:
        if direct_connection is None:
            connect(window.handle_system_shutdown)
        else:
            connect(window.handle_system_shutdown, direct_connection)
    except TypeError:
        # Minimal Qt doubles and older bindings may expose only the one-argument
        # connect form. The production PySide6 path uses DirectConnection.
        connect(window.handle_system_shutdown)


def main() -> int:
    app = QApplication(sys.argv)
    # Apply Fusion before the explicit palette so native Windows theme
    # changes cannot produce unreadable foreground/background combinations.
    app.setStyle(QStyleFactory.create("Fusion"))
    _force_light_palette(app)
    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except SingleInstanceError as exc:
        QMessageBox.critical(None, "BouncyBot - IBKR Portable Trading Bot already running", str(exc))
        return 2
    controller = None
    try:
        controller = TradingController()
        window = MainWindow(controller)
        _install_session_shutdown_hook(app, window)
        window.show()
        return app.exec()
    finally:
        try:
            if controller is not None:
                shutdown = getattr(controller, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception:
                        pass
        finally:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
