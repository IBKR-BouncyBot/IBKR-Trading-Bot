from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
CONTROLLER = Path("app/controller.py").read_text(encoding="utf-8")


def test_v121_version_labels_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'self._visual_refresh_timer.setInterval(75)' in GUI
    assert 'self._history_filter_timer.setInterval(200)' in GUI
    assert 'now - self._last_human_report_monotonic < 60.0' in CONTROLLER
    assert ("v2." + "0 ") not in GUI
    assert ("v" + "1." + "21") not in GUI


def test_v121_changed_while_running_is_per_field():
    assert "ApplicabilityBadge" in GUI
    assert "Current cycle" in GUI
    assert "Next order" in GUI
    assert "Next cycle only" in GUI
    assert "Not applicable now" in GUI
    assert "Changed:" in GUI
    assert "_running_change_baseline" in GUI
    assert "_changed_while_running_fields" in GUI


def test_v121_history_timeline_uses_market_capture_and_audit_events():
    assert '"Timeline",\n            self._build_timeline_tab' in GUI
    assert "self.tabs.currentChanged.connect(self._queue_materialize_tab)" in GUI
    assert "CycleTimelineWidget" in GUI
    assert "market_capture_rows" in GUI
    assert "_load_market_capture_rows" in GUI
    assert "debug_captures_dir" in GUI
    assert "Stage transitions" in GUI
    assert "Risk/guard blocks" in GUI
    assert "PROTECTIVE SELL" in GUI


def test_v121_recovery_buttons_are_unambiguous_and_refresh_broker_state():
    assert 'self.recovery_stop_cycle_btn = QPushButton("Stop after current cycle")' in GUI
    assert 'self.recovery_refresh_broker_btn.clicked.connect(self._recovery_refresh_broker_clicked)' in GUI
    assert 'self.recovery_refresh_btn' not in GUI
    assert 'self.controller.refresh_history(self.history_ticker_filter.text() if hasattr(self, "history_ticker_filter") else "")' not in GUI
    assert 'self.recovery_stop_cycle_btn = QPushButton("Stop cycle")' not in GUI

def test_v121_stage_order_handles_normalized_string_stage_labels():
    assert "STAGE_ORDER = [stage.value for stage, _label in STAGE_LABELS]" not in GUI
    assert "def _stage_value(stage: Any) -> str:" in GUI
    assert "STAGE_ORDER = [_stage_value(stage) for stage, _label in STAGE_LABELS]" in GUI
    assert "STAGE_TITLES = {_stage_value(stage): label for stage, label in STAGE_LABELS}" in GUI



def test_v121_view_mode_defaults_to_advanced_and_explains_debug():
    assert 'DEFAULT_VIEW_MODE = "Advanced"' in GUI
    assert 'self.view_mode_combo.setCurrentText(DEFAULT_VIEW_MODE)' in GUI
    assert 'VIEW_MODE_HELP = {' in GUI
    assert 'Advanced: default live-supervision view' in GUI
    assert 'Debug: Advanced plus raw API fields' in GUI
    assert 'Simple hides diagnostics. Advanced is the default operating view. Debug adds raw API/internal troubleshooting detail.' in GUI


def test_v121_ticker_contract_section_is_above_entry():
    assert 'grid.addWidget(ticker_box, 0, 0, 1, 4)' in GUI
    assert 'grid.addWidget(entry_box, 1, 0, 1, 4)' in GUI
    assert 'grid.addWidget(exit_box, 2, 0, 1, 4)' in GUI
    assert 'grid.addWidget(atr_box, 3, 0, 1, 4)' in GUI
    assert 'ticker_grid.addWidget(QLabel("Ticker"), 0, 0)' in GUI
    assert 'ticker_grid.addWidget(QLabel("Primary exchange"), 0, 2)' in GUI
    assert 'ticker_grid.addWidget(QLabel("IBKR conId"), 1, 0)' in GUI
    assert 'ticker_grid.addWidget(self.contract_label, 3, 0, 1, 4)' in GUI


def test_v121_command_bar_is_bottom_bar_below_tabs():
    live_idx = GUI.index('self.shell_layout.addWidget(self.live_status_bar)')
    tabs_idx = GUI.index('self.shell_layout.addWidget(self.tabs, 1)')
    command_idx = GUI.index('outer.addWidget(self.command_bar, 0)')
    scroll_idx = GUI.index('outer.addWidget(scroll, 1)')
    assert live_idx < tabs_idx
    assert scroll_idx < command_idx
    assert 'self.tabs.currentChanged.connect(self._on_tab_changed)' in GUI
    assert 'self.shell_layout.addWidget(self.command_bar)' not in GUI
    assert 'parented inside the Live strategy' in GUI


def test_v121_stage_ribbon_is_above_input_boxes_in_all_view_modes():
    stage_idx = GUI.index('self.stage_ribbon = StageRibbon()')
    connection_idx = GUI.index('self.connection_box = self._connection_group()')
    strategy_idx = GUI.index('self.strategy_box = self._strategy_group()')
    assert stage_idx < connection_idx < strategy_idx


def test_v121_max_cycles_text_is_total_not_daily():
    assert 'QLabel("Max cycles")' in GUI
    assert 'Maximum completed cycles allowed for this ticker in total.' in GUI
    assert 'cycles <= {self.max_cycles_ticker_day_spin.value()} total' in GUI
    assert ('Max cycles' + '/ticker/day') not in GUI
    assert 'cycles <= {self.max_cycles_ticker_day_spin.value()}/day' not in GUI


def test_v121_requested_guard_defaults_and_zero_disabled_text_are_visible():
    assert 'self.max_gap_pct_spin = self._pct_spin(0.00, allow_zero=True)' in GUI
    assert 'self.volatility_filter_check.setChecked(False)' in GUI
    assert 'self.session_timing_guard_check.setChecked(True)' in GUI
    assert 'Default 0 disables this guard.' in GUI
    assert 'Default OFF. Check to block new BUY entries when app-observed recent price movement exceeds the configured maximum.' in GUI
    assert 'Default ON. Blocks new BUY entries near market open/close' in GUI
    assert 'ZeroDisabledLabel' in GUI
    assert 'Disabled by value 0:' in GUI
    assert '0 = disabled' in GUI


def test_v22_stop_strategy_button_and_dialog_hide_tws_order_actions_without_open_orders():
    assert '("stop", "5. Stop strategy", self._stop_clicked)' in GUI
    assert 'self.stop_btn = self.command_step_buttons["stop"]' in GUI
    assert 'show_tws_order_actions: bool = True' in GUI
    assert 'No app-owned open orders are currently visible in TWS.' in GUI
    assert 'layout.addWidget(self.cancel_btn)' in GUI
    assert 'if self.show_position_close_action:' in GUI
    assert 'layout.addWidget(self.leave_btn)' in GUI
    assert 'layout.addWidget(self.after_btn)' in GUI
    assert 'self.stop_now_btn = QPushButton("Stop strategy now")' in GUI
    assert 'self.stop_exit_btn = QPushButton("Stop strategy and exit app")' in GUI
    assert 'StopAction.STOP_NOW_NO_BROKER_ACTION' in GUI
    assert 'self._stop_dialog_exit_requested = True' in GUI


def test_v22_stopped_graph_hides_strategy_levels_but_keeps_price_graph():
    assert 'stage in {Stage.STOPPED.value, Stage.IDLE.value}' in GUI
    assert 'add("Current price", current_price, "#111827", "last usable app price")' in GUI
    assert 'Only the current price is shown until a strategy cycle is running.' in GUI
    assert 'self.strategy_graph.update_data(cycle, price_snapshot, strategy, repaint=dashboard_active)' in GUI


def test_v22_guard_blocker_text_says_not_waiting_for_initial_drop():
    assert 'Trading is paused by a guard/risk/timing condition, not by the initial-drop price condition.' in GUI
    assert 'Stage 1 is blocked by a guard/risk/timing condition, not by the initial-drop condition.' in GUI
    assert 'Guard paused' in GUI


def test_v22_startup_resume_requires_explicit_start_click():
    controller = Path("app/controller.py").read_text(encoding="utf-8")
    assert 'self._startup_resume_required = self._cycle_needs_operator_start(self.active_cycle)' in controller
    assert 'click 4. Start strategy to resume monitoring/recovery.' in controller
    assert (
        "if self._startup_resume_required:\n"
        "            self._refresh_confirmed_market_data_if_due(timeout=read_timeout)\n"
        "            return"
    ) in controller


def test_v28_windows_build_reverts_to_v22_fast_packaging_path():
    build = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    checked = Path("scripts/build_windows_checked.ps1").read_text(encoding="utf-8")
    bat = Path("scripts/build_windows.bat").read_text(encoding="utf-8")
    assert "[switch]$RunTests" in build
    assert "[switch]$CleanVenv" in build
    assert "[switch]$SkipTests" not in build
    assert "Skipping full tests for faster, more reliable packaging." in build
    assert 'Invoke-Checked "Run pytest"' in build
    assert 'Invoke-Checked "Run CSV simulation fixtures"' in build
    assert 'Invoke-Checked "Compile app, tests, and main.py"' not in build
    assert "build_pytest.log" not in build
    assert '$env:QT_QPA_PLATFORM = "offscreen"' not in build
    assert '& $script -RunTests' in checked
    assert 'powershell -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*' in bat


def test_v23_timeline_uses_pure_scaling_helpers():
    assert 'filter_path_points_for_display' in GUI
    assert 'from .timeline_scaling import' in GUI
    assert 'downsample_timeline_points' in GUI
    assert 'path_bounds = display_price_bounds(path_prices, ())' in GUI
    assert 'def _position_for_timed_item' in GUI
    assert 'Hidden from plotted market path' in GUI
    assert 'No positive price markers are available for this cycle.' in GUI
