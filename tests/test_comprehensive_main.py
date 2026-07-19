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
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.styles: list[Any] = []
        self.palette: Any = None

    def setStyle(self, style: Any) -> None:
        self.styles.append(style)

    def setPalette(self, palette: Any) -> None:
        self.palette = palette

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


def test_force_light_palette_sets_every_declared_role(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApplication()
    monkeypatch.setattr(main_module, "QPalette", RecordingPalette)
    monkeypatch.setattr(main_module, "QColor", lambda value: value)

    main_module._force_light_palette(app)

    assert app.styles == ["Fusion"]
    assert app.palette is not None
    assert app.palette.colors == {
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

    class Window:
        def __init__(self, controller: Any) -> None:
            events.append(("window", controller))

        def show(self) -> None:
            events.append("show")

    controller = object()
    monkeypatch.setattr(main_module, "QApplication", lambda argv: app)
    monkeypatch.setattr(main_module, "QStyleFactory", SimpleNamespace(create=lambda name: f"style:{name}"))
    monkeypatch.setattr(main_module, "_force_light_palette", lambda value: events.append(("palette", value)))
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: controller)
    monkeypatch.setattr(main_module, "MainWindow", Window)

    assert main_module.main() == 17
    assert app.styles == ["style:Fusion"]
    assert events == [
        ("palette", app),
        "acquire",
        ("window", controller),
        "show",
        "release",
    ]


def test_main_releases_lock_when_final_shutdown_raises(main_module, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    app = FakeApplication(exit_code=23)
    lock = SimpleNamespace(
        acquire=lambda: events.append("acquire"),
        release=lambda: events.append("release"),
    )
    controller = SimpleNamespace(
        shutdown=lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed"))
    )

    class Window:
        def __init__(self, value: Any) -> None:
            assert value is controller

        def show(self) -> None:
            events.append("show")

    monkeypatch.setattr(main_module, "QApplication", lambda argv: app)
    monkeypatch.setattr(main_module, "QStyleFactory", SimpleNamespace(create=lambda name: name))
    monkeypatch.setattr(main_module, "_force_light_palette", lambda value: None)
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
    monkeypatch.setattr(main_module, "QStyleFactory", SimpleNamespace(create=lambda name: name))
    monkeypatch.setattr(main_module, "_force_light_palette", lambda app: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", BusyLock)
    monkeypatch.setattr(
        main_module,
        "QMessageBox",
        SimpleNamespace(critical=lambda *args: messages.append(args)),
    )
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
    monkeypatch.setattr(main_module, "QStyleFactory", SimpleNamespace(create=lambda name: name))
    monkeypatch.setattr(main_module, "_force_light_palette", lambda app: None)
    monkeypatch.setattr(main_module, "SingleInstanceLock", lambda: lock)
    monkeypatch.setattr(main_module, "TradingController", lambda: (_ for _ in ()).throw(RuntimeError("startup failed")))

    with pytest.raises(RuntimeError, match="startup failed"):
        main_module.main()
    assert released == [True]
