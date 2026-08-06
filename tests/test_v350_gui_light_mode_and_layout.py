"""v3.9.0 GUI layout, startup-theme, and theme-switch state regressions."""

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


def test_advanced_dashboard_places_price_monitor_before_configuration_sections() -> None:
    dashboard = GUI_SOURCE[
        GUI_SOURCE.index("def _build_dashboard(") : GUI_SOURCE.index(
            "def _connection_group(", GUI_SOURCE.index("def _build_dashboard(")
        )
    ]

    price_position = dashboard.index("root.addWidget(self.price_panel)")
    connection_position = dashboard.index("self.connection_box = self._connection_group()")
    strategy_position = dashboard.index("self.strategy_box = self._strategy_group()")
    configuration_position = dashboard.index("root.addLayout(top)")

    assert price_position < connection_position < configuration_position
    assert price_position < strategy_position < configuration_position
    assert 'self.view_mode_combo.addItems(["Simple", "Advanced", "Debug"])' in GUI_SOURCE
    assert 'DEFAULT_VIEW_MODE = "Advanced"' in GUI_SOURCE


def test_application_startup_forces_light_mode_and_does_not_install_system_hook() -> None:
    main_body = MAIN_SOURCE[MAIN_SOURCE.index("def main() -> int:") :]

    assert "_apply_application_palette(app)" in main_body
    assert "dark: Optional[bool] = False" in MAIN_SOURCE
    assert "_install_system_theme_hook(app, window)" not in main_body
    assert 'QAction("Light mode", self)' in GUI_SOURCE
    assert 'QAction("Dark mode", self)' in GUI_SOURCE


def test_cached_command_state_reasserts_button_enablement(gui_module) -> None:
    button = gui_module.QPushButton("4. Start strategy")
    card = gui_module.CommandStepCard("4. Start strategy", button)
    card.set_state("Ready", True, "Ready to start")

    # Model the native control state observed after a Fusion style rebuild while
    # the semantic state cache still says the button is enabled.
    button.setEnabled(False)
    assert button.isEnabled() is False

    card.set_state("Ready", True, "Ready to start")

    assert button.isEnabled() is True


def test_theme_switch_reconciles_interaction_state_immediately_and_next_turn(
    gui_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []

    class App:
        def setProperty(self, name: str, value: Any) -> None:
            events.append(("property", name, value))

        def allWidgets(self) -> list[Any]:
            return []

    app = App()
    monkeypatch.setattr(gui_module, "QApplication", SimpleNamespace(instance=lambda: app))
    monkeypatch.setattr(
        gui_module.QTimer,
        "singleShot",
        staticmethod(lambda delay, callback: (events.append(("singleShot", delay)), callback())),
    )

    window = object.__new__(gui_module.MainWindow)
    window._apply_styles = lambda: events.append("stylesheet")
    window._sync_theme_menu_actions = lambda: events.append("menu")
    window._restore_interaction_state_after_theme_change = lambda: events.append("restore")
    window.update = lambda: events.append("update")

    gui_module.MainWindow.apply_system_theme(window, True)

    assert events.count("restore") == 2
    assert ("singleShot", 0) in events
    assert events.index("stylesheet") < events.index("restore")


def test_theme_restore_enables_command_parent_and_reapplies_current_gates(gui_module) -> None:
    window = object.__new__(gui_module.MainWindow)
    command_bar = gui_module.QFrame()
    command_bar.setEnabled(False)
    view_mode_combo = gui_module.QComboBox()
    view_mode_combo.setEnabled(False)
    snapshot = {"active_cycle": {"stage": "3_WAIT_RISE_TRIGGER"}}
    calls: list[Any] = []

    window.__dict__.update(
        {
            "command_bar": command_bar,
            "view_mode_combo": view_mode_combo,
            "current_snapshot": snapshot,
        }
    )
    window._update_input_locks = lambda stage: calls.append(("locks", stage))
    window._update_command_bar_states = lambda current: calls.append(("commands", current))

    gui_module.MainWindow._restore_interaction_state_after_theme_change(window)

    assert command_bar.isEnabled() is True
    assert view_mode_combo.isEnabled() is True
    assert calls == [
        ("locks", "3_WAIT_RISE_TRIGGER"),
        ("commands", snapshot),
    ]


def test_theme_restore_preserves_stale_worker_fail_closed_buttons(gui_module) -> None:
    window = object.__new__(gui_module.MainWindow)
    command_bar = gui_module.QFrame()
    buttons = {
        "start": gui_module.QPushButton("4. Start strategy"),
        "stop": gui_module.QPushButton("5. Stop strategy"),
    }
    for button in buttons.values():
        button.setEnabled(True)
    snapshot = {
        "active_cycle": {"stage": "3_WAIT_RISE_TRIGGER"},
        "watchdog_override": {"active": True},
    }
    window.__dict__.update(
        {
            "command_bar": command_bar,
            "current_snapshot": snapshot,
            "command_step_buttons": buttons,
        }
    )
    window._update_input_locks = lambda _stage: None
    window._update_command_bar_states = lambda _snapshot: None

    gui_module.MainWindow._restore_interaction_state_after_theme_change(window)

    assert command_bar.isEnabled() is True
    assert all(button.isEnabled() is False for button in buttons.values())
