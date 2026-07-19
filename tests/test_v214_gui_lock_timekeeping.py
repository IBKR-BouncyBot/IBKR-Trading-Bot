from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
MODELS = Path("app/models.py").read_text(encoding="utf-8")
ADAPTER = Path("app/ib_adapter.py").read_text(encoding="utf-8")
TIMELINE = Path("app/timeline_scaling.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v215_version_metadata_and_package_docs_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.0.19" in GUI
    assert "# BouncyBot - IBKR Portable Trading Bot v3.0.19" in README
    assert 'version = "3.0.19"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_v215_stop_strategy_button_and_live_input_headings_are_emphasized():
    assert 'self.stop_btn = self.command_step_buttons["stop"]' in GUI
    assert 'self._make_button_bold(button)' in GUI
    assert 'box = QGroupBox("STRATEGY INPUTS")' in GUI
    assert 'entry_box = QGroupBox("ENTRY")' in GUI
    assert 'exit_box = QGroupBox("EXIT")' in GUI
    assert 'QGroupBox#StrategyInputsBox::title' in GUI
    assert 'QGroupBox#EntryBox::title' in GUI
    assert 'QGroupBox#ExitBox::title' in GUI
    assert 'font-weight: 900;' in GUI


def test_v215_status_bar_input_lock_exists_and_is_gui_only():
    assert 'self.input_lock_btn = QPushButton("\\U0001f513")' in GUI
    assert 'Values unlocked' not in GUI
    assert 'Values locked' not in GUI
    assert 'self.input_lock_btn.setFixedWidth(48)' in GUI
    assert 'font-size: 20px;' in GUI
    assert 'self.input_lock_btn.setCheckable(True)' in GUI
    assert 'self.input_lock_btn.setChecked(False)' in GUI
    assert 'def set_input_lock_state(self, locked: bool)' in GUI
    assert 'def _refresh_manual_input_lock_widget_registry(self) -> None:' in GUI
    assert 'def _manual_input_lock_toggled(self, checked: bool) -> None:' in GUI
    assert 'self.live_status_bar.input_lock_btn.toggled.connect(self._manual_input_lock_toggled)' in GUI
    assert 'widget.setProperty("manualInputLocked", locked_by_operator)' in GUI
    assert 'QLineEdit[manualInputLocked="true"]' in GUI
    assert 'The lock is an accidental-edit guard, not a trading stop.' in GUI


def test_v215_rth_text_and_utc_timekeeping_are_visible_and_consistent():
    assert 'APP_TIMEZONE_LABEL = "UTC"' in MODELS
    assert 'def _format_utc_timestamp(value: Any' in GUI
    assert 'def _format_rth_status(price_snapshot' in GUI
    assert 'RTH open' in GUI
    assert 'RTH closed' in GUI
    assert 'Last update ({APP_TIMEZONE_LABEL})' in GUI
    assert 'return datetime.fromtimestamp(float(parsed), timezone.utc).strftime(fmt)' in GUI
    assert 'timestamp=utc_now_iso()' in ADAPTER
    assert 'parsed = parsed.replace(tzinfo=timezone.utc)' in TIMELINE
    assert 'datetime.strptime(str(value).strip(), fmt).replace(tzinfo=timezone.utc).timestamp()' in TIMELINE
    assert 'All app-generated timestamps are recorded and displayed in UTC' in Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")


def test_v215_default_history_example_uses_current_utc_audit_data():
    assert 'This is synthetic v3.0.19 paper-trading example data.' in GUI
    assert '"created_at": "2026-07-16T13:35:00+00:00"' in GUI
    assert '"buy_filled_at": "2026-07-16T14:08:27+00:00"' in GUI
    assert '"sell_filled_at": "2026-07-16T15:55:14+00:00"' in GUI
    assert '"market_data_mode": "Live market data (synthetic sample)"' in GUI
    assert '"atr_adaptive_enabled": True' in GUI
    assert '"event_type": "DROP_TRIGGER_HIT"' in GUI


def test_v215_window_close_uses_stop_strategy_dialog_even_when_safe():
    close_idx = GUI.index('def closeEvent(self, event) -> None')
    close_block = GUI[close_idx: GUI.index('def _apply_styles', close_idx)]
    assert 'The window close button uses the same controlled-exit path' in close_block
    assert 'dialog = StopDialog(' in close_block
    assert 'safe_to_exit=(not open_orders and not show_position_close and safe_no_running_strategy)' in close_block
    assert 'if dialog.exec() != QDialog.Accepted:' in close_block
    assert 'self.controller.shutdown()' in close_block
    assert 'event.accept()' in close_block
    assert 'event.ignore()' in close_block
    assert 'if cycle and cycle.get("stage") not in {Stage.CYCLE_COMPLETE.value' not in close_block
