"""Fusion light/dark appearance regressions for v3.6.0."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.qt_stubs import imported_gui_with_stubs

ROOT = Path(__file__).resolve().parents[1]
GUI_SOURCE = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


@pytest.fixture
def gui_module():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


def test_dark_stylesheet_converts_surfaces_text_borders_and_console_colors(gui_module) -> None:
    light = """
        QWidget {
            color: #111827;
            background-color: #f6f7f9;
            border: 1px solid #c7cbd1;
        }
        QTextEdit {
            color: #f9fafb;
            background-color: #202124;
            border: 1px solid #111827;
        }
    """

    dark = gui_module._dark_stylesheet(light)

    assert "color: #f0f0f0;" in dark
    assert "background-color: #353535;" in dark
    assert "border: 1px solid #5a5a5a;" in dark
    assert "color: #f7f7f7;" in dark
    assert "background-color: #1e1e1e;" in dark
    assert "border: 1px solid #555555;" in dark


def test_theme_color_helpers_follow_application_property(gui_module, monkeypatch: pytest.MonkeyPatch) -> None:
    class App:
        def __init__(self) -> None:
            self.dark = False

        def property(self, name: str) -> bool:
            assert name == gui_module.DARK_MODE_APP_PROPERTY
            return self.dark

    app = App()
    monkeypatch.setattr(gui_module, "QApplication", SimpleNamespace(instance=lambda: app))

    assert gui_module._theme_hex("#light", "#dark") == "#light"
    app.dark = True
    assert gui_module._theme_hex("#light", "#dark") == "#dark"


def test_main_window_theme_refresh_updates_cached_state_widgets(gui_module, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []

    class ThemeWidget:
        def refresh_theme(self) -> None:
            events.append("refresh")

    class App:
        def setProperty(self, name: str, value: Any) -> None:
            events.append((name, value))

        def allWidgets(self) -> list[Any]:
            return [ThemeWidget()]

    app = App()
    monkeypatch.setattr(gui_module, "QApplication", SimpleNamespace(instance=lambda: app))
    window = object.__new__(gui_module.MainWindow)
    window._apply_styles = lambda: events.append("stylesheet")
    window.update = lambda: events.append("window-update")

    gui_module.MainWindow.apply_system_theme(window, True)

    assert window._dark_mode is True
    assert events == [
        (gui_module.DARK_MODE_APP_PROPERTY, True),
        "stylesheet",
        "refresh",
        "window-update",
    ]


def test_cached_semantic_widgets_reapply_their_last_state_on_theme_change(gui_module) -> None:
    stage_calls: list[Any] = []
    ribbon = object.__new__(gui_module.StageRibbon)
    ribbon._last_stage = "3_WAIT_SELL_PROFIT_GUARD"
    ribbon.set_stage = lambda stage: stage_calls.append(stage)

    status_calls: list[Any] = []
    pill = object.__new__(gui_module.StatusPill)
    pill._state = "success"
    pill.set_state = lambda state: status_calls.append(state)

    command_calls: list[Any] = []
    card = object.__new__(gui_module.CommandStepCard)
    card._last_state_signature = ("READY", True, "Contract confirmed")
    card.set_state = lambda state, enabled, detail: command_calls.append((state, enabled, detail))

    gui_module.StageRibbon.refresh_theme(ribbon)
    gui_module.StatusPill.refresh_theme(pill)
    gui_module.CommandStepCard.refresh_theme(card)

    assert stage_calls == ["3_WAIT_SELL_PROFIT_GUARD"]
    assert status_calls == ["success"]
    assert command_calls == [("READY", True, "Contract confirmed")]


def test_custom_painted_views_use_theme_selected_colors() -> None:
    class_boundaries = [
        ("class CycleTimelineWidget", "class ProfitGuardWidget"),
        ("class ProfitGuardWidget", "class StrategyGraphWidget"),
        ("class StrategyGraphWidget", "class StrategyFlowchartWidget"),
        ("class StrategyFlowchartWidget", "class PricePanel"),
    ]
    for start, end in class_boundaries:
        segment = GUI_SOURCE[GUI_SOURCE.index(start) : GUI_SOURCE.index(end)]
        assert "_theme_color(" in segment, start


def test_theme_startup_is_light_and_runtime_menu_switching_remains_wired() -> None:
    main_body = MAIN_SOURCE[MAIN_SOURCE.index("def main() -> int:") :]
    assert "_apply_application_palette(app)" in main_body
    assert "dark: Optional[bool] = False" in MAIN_SOURCE
    assert "_install_system_theme_hook(app, window)" not in main_body
    assert "def _set_theme_from_view_menu" in GUI_SOURCE
    assert "_apply_fusion_application_palette(app, bool(dark))" in GUI_SOURCE
    assert "self.apply_system_theme(bool(dark))" in GUI_SOURCE


def test_view_menu_exposes_explicit_light_and_dark_choices_in_menu_order() -> None:
    file_pos = GUI_SOURCE.index('self.menuBar().addMenu("File")')
    view_pos = GUI_SOURCE.index('self.menuBar().addMenu("View")')
    about_pos = GUI_SOURCE.index('self.menuBar().addMenu("About")')

    assert file_pos < view_pos < about_pos
    assert 'QAction("Light mode", self)' in GUI_SOURCE
    assert 'QAction("Dark mode", self)' in GUI_SOURCE
    assert "self.light_mode_action.setCheckable(True)" in GUI_SOURCE
    assert "self.dark_mode_action.setCheckable(True)" in GUI_SOURCE


def test_view_menu_theme_choice_applies_palette_and_refreshes_window(
    gui_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    app = object()
    monkeypatch.setattr(gui_module, "QApplication", SimpleNamespace(instance=lambda: app))
    monkeypatch.setattr(
        gui_module,
        "_apply_fusion_application_palette",
        lambda target, dark: events.append(("palette", target, dark)),
    )
    window = object.__new__(gui_module.MainWindow)
    window.apply_system_theme = lambda dark: events.append(("window", dark))

    gui_module.MainWindow._set_theme_from_view_menu(window, True)

    assert events == [("palette", app, True), ("window", True)]


@pytest.mark.parametrize("dark", [False, True])
def test_fusion_palette_helper_applies_complete_selected_palette(gui_module, dark: bool) -> None:
    events: list[Any] = []

    class App:
        def setStyle(self, style: Any) -> None:
            events.append(("style", style))

        def setPalette(self, palette: Any) -> None:
            events.append(("palette", palette))

        def setProperty(self, name: str, value: Any) -> None:
            events.append(("property", name, value))

    app = App()
    result = gui_module._apply_fusion_application_palette(app, dark)

    assert result is dark
    assert events[0] == ("style", "Fusion")
    assert isinstance(events[1][1], gui_module.QPalette)
    assert events[2] == ("property", gui_module.DARK_MODE_APP_PROPERTY, dark)


def test_theme_menu_checks_are_mutually_exclusive(gui_module) -> None:
    class Action:
        def __init__(self) -> None:
            self.checked: bool | None = None

        def setChecked(self, checked: bool) -> None:
            self.checked = checked

    window = object.__new__(gui_module.MainWindow)
    window.light_mode_action = Action()
    window.dark_mode_action = Action()
    window._dark_mode = True

    gui_module.MainWindow._sync_theme_menu_actions(window)

    assert window.light_mode_action.checked is False
    assert window.dark_mode_action.checked is True


def test_theme_state_is_not_persisted_or_exposed_as_strategy_configuration() -> None:
    assert "DARK_MODE_APP_PROPERTY" in GUI_SOURCE
    assert "app_settings" not in GUI_SOURCE[GUI_SOURCE.index("def apply_system_theme") : GUI_SOURCE.index("def _apply_styles")]
    for relative in ("app/models.py", "app/strategy.py", "app/controller.py", "app/storage.py"):
        assert "bouncybotDarkMode" not in (ROOT / relative).read_text(encoding="utf-8")
