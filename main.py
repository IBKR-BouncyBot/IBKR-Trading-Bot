"""Windows GUI entry point and process-level safety setup.

The entry point creates the Qt application in light mode, acquires the
portable-folder single-instance lock, constructs the
controller/window, and releases process resources on shutdown. Trading decisions
remain in the strategy and controller layers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory

from app.controller import TradingController
from app.gui import (
    DARK_FUSION_PALETTE_COLORS,
    DARK_MODE_APP_PROPERTY,
    LIGHT_FUSION_PALETTE_COLORS,
    MainWindow,
)
from app.lockfile import SingleInstanceError, SingleInstanceLock
from app.paths import app_dir, resource_path
from app.watchdog import (
    WATCHDOG_RESTART_EXIT_CODE,
    append_emergency_log,
    consume_watchdog_restart_request,
    discard_expired_watchdog_restart_request,
    discard_watchdog_restart_request,
)


def _color_scheme_dark_state(scheme: Any) -> Optional[bool]:
    """Translate a Qt color-scheme enum to ``True``/``False`` when known."""
    color_scheme_enum = getattr(Qt, "ColorScheme", None)
    if color_scheme_enum is None:
        return None
    dark = getattr(color_scheme_enum, "Dark", None)
    light = getattr(color_scheme_enum, "Light", None)
    try:
        if dark is not None and scheme == dark:
            return True
        if light is not None and scheme == light:
            return False
    except Exception:
        return None
    return None


def _system_prefers_dark(app: QApplication) -> bool:
    """Detect the current OS color scheme with a palette fallback."""
    try:
        style_hints = app.styleHints()
        color_scheme = getattr(style_hints, "colorScheme", None)
        if callable(color_scheme):
            resolved = _color_scheme_dark_state(color_scheme())
            if resolved is not None:
                return resolved
    except Exception:
        pass

    # The fallback covers platforms or Qt builds where colorScheme() is not
    # exposed. It is evaluated before this module installs its own palette.
    try:
        palette_getter = getattr(app, "palette", None)
        palette = palette_getter() if callable(palette_getter) else None
        if palette is not None:
            window_color = palette.color(QPalette.Window)
            lightness = getattr(window_color, "lightness", None)
            if callable(lightness):
                return int(lightness()) < 128
    except Exception:
        pass
    return False


def _apply_application_palette(app: QApplication, dark: Optional[bool] = False) -> bool:
    """Apply a Fusion palette; startup defaults to light unless explicitly overridden."""
    dark = _system_prefers_dark(app) if dark is None else bool(dark)
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    colors = DARK_FUSION_PALETTE_COLORS if dark else LIGHT_FUSION_PALETTE_COLORS
    for role_name, color in colors.items():
        palette.setColor(getattr(QPalette, role_name), QColor(color))
    app.setPalette(palette)
    app.setProperty(DARK_MODE_APP_PROPERTY, dark)
    return dark


def _apply_application_icon(app: QApplication) -> None:
    """Apply the packaged/source BouncyBot icon when the asset is available."""
    icon_path = resource_path("Images", "BouncyBot_app_icon.png")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))


def _install_system_theme_hook(app: QApplication, window: MainWindow) -> None:
    """Keep the running application synchronized with OS theme changes."""
    try:
        style_hints = app.styleHints()
        signal = getattr(style_hints, "colorSchemeChanged", None)
        connect = getattr(signal, "connect", None)
    except Exception:
        return
    if not callable(connect):
        return

    def apply_scheme(scheme: Any = None) -> None:
        dark = _color_scheme_dark_state(scheme)
        if dark is None:
            dark = _system_prefers_dark(app)
        _apply_application_palette(app, dark)
        apply_theme = getattr(window, "apply_system_theme", None)
        if callable(apply_theme):
            apply_theme(dark)

    connect(apply_scheme)
    # Retain the callback for bindings/test doubles that do not hold a strong
    # reference to Python slots connected to a C++ signal.
    setattr(window, "_system_theme_callback", apply_scheme)


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


WATCHDOG_RECOVERY_ARGUMENT = "--watchdog-recovery-token="


def _split_watchdog_recovery_argument(argv: list[str]) -> tuple[list[str], str]:
    """Remove the private restart token before Qt parses command-line options."""
    cleaned: list[str] = []
    token = ""
    for index, argument in enumerate(list(argv or [])):
        text = str(argument)
        if index > 0 and text.startswith(WATCHDOG_RECOVERY_ARGUMENT):
            candidate = text[len(WATCHDOG_RECOVERY_ARGUMENT) :].strip()
            if candidate:
                token = candidate
            continue
        cleaned.append(text)
    if not cleaned:
        cleaned = [str(Path(__file__).resolve())]
    return cleaned, token


def _watchdog_replacement_argv(token: str, qt_argv: list[str]) -> list[str]:
    """Build argv for the same source or frozen application process."""
    recovery_argument = f"{WATCHDOG_RECOVERY_ARGUMENT}{str(token or '').strip()}"
    passthrough = list(qt_argv[1:]) if qt_argv else []
    if getattr(sys, "frozen", False):
        return [sys.executable, *passthrough, recovery_argument]
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        *passthrough,
        recovery_argument,
    ]


def _replace_with_watchdog_process(token: str, qt_argv: list[str]) -> None:
    argv = _watchdog_replacement_argv(token, qt_argv)
    if os.name == "nt":
        # os.exec* on Windows joins argv with spaces and applies no quoting, so
        # a portable folder path containing spaces would break the relaunch.
        # Windows also cannot atomically replace a process image. Start the
        # properly quoted replacement (subprocess applies MS quoting rules) and
        # let this process finish its normal exit; the single-instance lock was
        # already released by the caller.
        subprocess.Popen(argv, close_fds=True)  # noqa: S603
        return
    os.execv(sys.executable, argv)


def main() -> int:
    qt_argv, incoming_watchdog_token = _split_watchdog_recovery_argument(list(sys.argv))
    app = QApplication(qt_argv)
    # Start every session in the validated light appearance. Dark mode remains
    # available as an explicit runtime choice under View.
    _apply_application_palette(app)
    _apply_application_icon(app)
    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except SingleInstanceError as exc:
        QMessageBox.critical(None, "BouncyBot - IBKR Portable Trading Bot already running", str(exc))
        return 2

    restart_request: Optional[dict[str, Any]] = None
    if incoming_watchdog_token:
        restart_request = consume_watchdog_restart_request(
            incoming_watchdog_token,
            base_dir=app_dir(),
        )
        if restart_request is None:
            append_emergency_log(
                "Replacement process received an invalid or expired watchdog recovery token; starting fail-closed without auto-resume.",
                context={"token_present": True},
                base_dir=app_dir(),
            )
    else:
        # An ordinary tokenless launch cannot consume a handoff. Remove only an
        # expired or malformed leftover request file; a fresh one is preserved
        # for its token-holding replacement process.
        discard_expired_watchdog_restart_request(base_dir=app_dir())

    controller: Optional[TradingController] = None
    window: Optional[MainWindow] = None
    exit_code = 0
    restart_token = ""
    try:
        controller = TradingController()
        # MainWindow starts the worker in its constructor. Queue the authenticated
        # exact-cycle recovery command first so the worker can never observe a
        # replacement-process startup without its one-time recovery gate.
        if restart_request is not None:
            controller.resume_after_watchdog_restart(restart_request)
        window = MainWindow(controller)
        _install_session_shutdown_hook(app, window)
        window.show()
        exit_code = int(app.exec())
        if exit_code == WATCHDOG_RESTART_EXIT_CODE:
            token_getter = getattr(window, "watchdog_restart_token", None)
            if callable(token_getter):
                restart_token = str(token_getter() or "").strip()
    finally:
        try:
            if controller is not None:
                shutdown = getattr(controller, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception as exc:
                        append_emergency_log(
                            "Controller shutdown raised while preparing process exit.",
                            exc=exc,
                            base_dir=app_dir(),
                        )
        finally:
            # The replacement process must acquire the same portable-folder
            # lock. Release it before execv; never run two worker processes.
            lock.release()

    if exit_code == WATCHDOG_RESTART_EXIT_CODE:
        if not restart_token:
            append_emergency_log(
                "Qt exited with the watchdog restart code but no one-time token was available; refusing an unauthenticated auto-resume.",
                base_dir=app_dir(),
            )
            return 3
        try:
            _replace_with_watchdog_process(restart_token, qt_argv)
        except BaseException as exc:
            discard_watchdog_restart_request(restart_token, base_dir=app_dir())
            append_emergency_log(
                "Could not replace the BouncyBot process after a watchdog exit.",
                exc=exc,
                context={
                    "replacement_mode": "frozen" if getattr(sys, "frozen", False) else "source",
                    "token_present": bool(restart_token),
                },
                base_dir=app_dir(),
            )
            return 4
        return 0
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
