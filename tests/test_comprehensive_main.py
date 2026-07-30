"""Deterministic tests for the process entry point without opening a GUI."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.qt_stubs import imported_gui_with_stubs


@pytest.fixture
def main_module():
    """Import ``main`` against Qt doubles and restore the prior module."""
    previous = sys.modules.pop("main", None)
    try:
        with imported_gui_with_stubs(Path.cwd()):
            module = importlib.import_module("main")
            yield module
    finally:
        sys.modules.pop("main", None)
        if previous is not None:
            sys.modules["main"] = previous


class FakeApplication:
    def __init__(self, exit_code: int = 0, style_hints: Any = None) -> None:
        self.exit_code = exit_code
        self.styles: list[Any] = []
        self.applied_palette: Any = None
        self.window_icons: list[Any] = []
        self.properties: dict[str, Any] = {}
        self._style_hints = style_hints

    def setStyle(self, style: Any) -> None:
        self.styles.append(style)

    def setPalette(self, palette: Any) -> None:
        self.applied_palette = palette

    def setProperty(self, name: str, value: Any) -> None:
        self.properties[name] = value

    def property(self, name: str) -> Any:
        return self.properties.get(name)

    def setWindowIcon(self, icon: Any) -> None:
        self.window_icons.append(icon)

    def styleHints(self) -> Any:
        if self._style_hints is None:
            raise AttributeError("style hints unavailable")
        return self._style_hints

    def exec(self) -> int:
        return self.exit_code


class RecordingPalette:
    Window = "Window"
    WindowText = "WindowText"
    Base = "Base"
    AlternateBase = "AlternateBase"
    ToolTipBase = "ToolTipBase"
    ToolTipText = "ToolTipText"
    Text = "Text"
    Button = "Button"
    ButtonText = "ButtonText"
    BrightText = "BrightText"
    Highlight = "Highlight"
    HighlightedText = "HighlightedText"
    PlaceholderText = "PlaceholderText"

    def __init__(self) -> None:
        self.colors: dict[str, str] = {}

    def setColor(self, role: str, color: str) -> None:
        self.colors[role] = color


def _prepare_palette_test(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "QPalette", RecordingPalette)
    monkeypatch.setattr(main_module, "QColor", lambda value: value)
    monkeypatch.setattr(main_module, "QStyleFactory", SimpleNamespace(create=lambda name: f"style:{name}"))


def test_system_theme_detection_prefers_qt_color_scheme(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    dark_scheme = object()
    light_scheme = object()
    monkeypatch.setattr(
        main_module,
        "Qt",
        SimpleNamespace(ColorScheme=SimpleNamespace(Dark=dark_scheme, Light=light_scheme)),
    )

    dark_app = FakeApplication(style_hints=SimpleNamespace(colorScheme=lambda: dark_scheme))
    light_app = FakeApplication(style_hints=SimpleNamespace(colorScheme=lambda: light_scheme))

    assert main_module._system_prefers_dark(dark_app) is True
    assert main_module._system_prefers_dark(light_app) is False


def test_application_palette_sets_every_light_role(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApplication()
    _prepare_palette_test(main_module, monkeypatch)

    assert main_module._apply_application_palette(app, dark=False) is False

    assert app.styles == ["style:Fusion"]
    assert app.properties[main_module.DARK_MODE_APP_PROPERTY] is False
    assert app.applied_palette.colors == {
        "Window": "#f6f7f9",
        "WindowText": "#111827",
        "Base": "#ffffff",
        "AlternateBase": "#f3f4f6",
        "ToolTipBase": "#ffffff",
        "ToolTipText": "#111827",
        "Text": "#111827",
        "Button": "#ffffff",
        "ButtonText": "#111827",
        "BrightText": "#ffffff",
        "Highlight": "#2563eb",
        "HighlightedText": "#ffffff",
        "PlaceholderText": "#6b7280",
    }


def test_application_palette_sets_every_dark_role(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApplication()
    _prepare_palette_test(main_module, monkeypatch)

    assert main_module._apply_application_palette(app, dark=True) is True

    assert app.properties[main_module.DARK_MODE_APP_PROPERTY] is True
    assert app.applied_palette.colors == {
        "Window": "#353535",
        "WindowText": "#f0f0f0",
        "Base": "#232323",
        "AlternateBase": "#303030",
        "ToolTipBase": "#2b2b2b",
        "ToolTipText": "#f0f0f0",
        "Text": "#f0f0f0",
        "Button": "#3d3d3d",
        "ButtonText": "#f0f0f0",
        "BrightText": "#ffffff",
        "Highlight": "#2a82da",
        "HighlightedText": "#ffffff",
        "PlaceholderText": "#a0a0a0",
    }


def test_system_theme_hook_reapplies_palette_and_window_styles(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    dark_scheme = object()
    light_scheme = object()
    monkeypatch.setattr(
        main_module,
        "Qt",
        SimpleNamespace(ColorScheme=SimpleNamespace(Dark=dark_scheme, Light=light_scheme)),
    )

    callbacks: list[Any] = []
    signal = SimpleNamespace(connect=lambda callback: callbacks.append(callback))
    app = FakeApplication(style_hints=SimpleNamespace(colorSchemeChanged=signal))
    palette_calls: list[tuple[Any, bool]] = []
    theme_calls: list[bool] = []
    window = SimpleNamespace(apply_system_theme=lambda dark: theme_calls.append(dark))
    monkeypatch.setattr(
        main_module,
        "_apply_application_palette",
        lambda target, dark=None: palette_calls.append((target, bool(dark))) or bool(dark),
    )

    main_module._install_system_theme_hook(app, window)
    assert len(callbacks) == 1

    callbacks[0](dark_scheme)
    callbacks[0](light_scheme)

    assert palette_calls == [(app, True), (app, False)]
    assert theme_calls == [True, False]
    assert window._system_theme_callback is callbacks[0]


def test_session_shutdown_hook_uses_window_handler(main_module) -> None:
    connections: list[tuple[Any, ...]] = []

    class SessionSignal:
        def connect(self, *args: Any) -> None:
            connections.append(args)

    handler_calls: list[Any] = []
    app = SimpleNamespace(commitDataRequest=SessionSignal())
    window = SimpleNamespace(handle_system_shutdown=lambda manager=None: handler_calls.append(manager))

    main_module._install_session_shutdown_hook(app, window)

    assert len(connections) == 1
    assert connections[0][0] is window.handle_system_shutdown
    connections[0][0]("session")
    assert handler_calls == ["session"]


def test_main_runs_window_and_always_releases_lock(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    app = FakeApplication(exit_code=17)
    lock = SimpleNamespace(
        acquire=lambda: events.append("acquire"),
        release=lambda: events.append("release"),
    )
    controller = SimpleNamespace(shutdown=lambda: events.append("shutdown"))

    class Window:
        def __init__(self, value: Any) -> None:
            events.append(("window", value))

        def show(self) -> None:
            events.append("show")

    monkeypatch.setattr(main_module, "QApplication", lambda argv: app)
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda value: events.append(("palette", value)))
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda value: events.append(("icon", value)))
    monkeypatch.setattr(
        main_module,
        "_install_system_theme_hook",
        lambda value, window: (_ for _ in ()).throw(
            AssertionError("system theme hook must not be installed at startup")
        ),
    )
    monkeypatch.setattr(main_module, "_install_session_shutdown_hook", lambda value, window: events.append("session_hook"))
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)

    assert main_module.main() == 17
    assert events == [
        ("palette", app),
        ("icon", app),
        "acquire",
        ("window", controller),
        "session_hook",
        "show",
        "shutdown",
        "release",
    ]


def test_main_releases_lock_when_final_shutdown_raises(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    app = FakeApplication(exit_code=23)
    lock = SimpleNamespace(
        acquire=lambda: events.append("acquire"),
        release=lambda: events.append("release"),
    )
    controller = SimpleNamespace(shutdown=lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")))

    class Window:
        def __init__(self, value: Any) -> None:
            assert value is controller

        def show(self) -> None:
            events.append("show")

    monkeypatch.setattr(main_module, "QApplication", lambda argv: app)
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda value: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda value: None)
    monkeypatch.setattr(main_module, "_install_system_theme_hook", lambda value, window: None)
    monkeypatch.setattr(main_module, "_install_session_shutdown_hook", lambda value, window: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)

    assert main_module.main() == 23
    assert events == ["acquire", "show", "release"]


def test_main_reports_lock_collision_without_constructing_controller(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[tuple[Any, ...]] = []

    class BusyLock:
        def acquire(self) -> None:
            raise main_module.SingleInstanceError("already running")

        def release(self) -> None:
            raise AssertionError("a lock that was not acquired must not be released")

    monkeypatch.setattr(main_module, "QApplication", lambda argv: FakeApplication())
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda app: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda app: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", BusyLock)
    monkeypatch.setattr(main_module, "QMessageBox", SimpleNamespace(critical=lambda *args: messages.append(args)))
    monkeypatch.setattr(
        main_module,
        "TradingController",
        lambda: (_ for _ in ()).throw(AssertionError("controller must not be constructed")),
    )

    assert main_module.main() == 2
    assert messages == [(None, "BouncyBot - IBKR Portable Trading Bot already running", "already running")]


def test_main_releases_lock_when_controller_construction_fails(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[bool] = []
    lock = SimpleNamespace(acquire=lambda: None, release=lambda: released.append(True))

    monkeypatch.setattr(main_module, "QApplication", lambda argv: FakeApplication())
    monkeypatch.setattr(main_module, "_apply_application_palette", lambda app: None)
    monkeypatch.setattr(main_module, "_apply_application_icon", lambda app: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: (_ for _ in ()).throw(RuntimeError("startup failed")))

    with pytest.raises(RuntimeError, match="startup failed"):
        main_module.main()
    assert released == [True]
