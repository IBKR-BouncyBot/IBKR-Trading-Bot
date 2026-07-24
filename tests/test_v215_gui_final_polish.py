from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def test_v215_version_metadata_and_package_folder_name_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.2.0" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.2.0"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_v215_simple_mode_does_not_limit_full_strategy_flowchart_to_three_cards():
    assert 'if mode == "Current cycle only":' in GUI
    assert 'if mode == "Current cycle only" or self._compact_mode' not in GUI
    assert 'return cards[:3] if self._compact_mode else cards' not in GUI
    assert 'self.flowchart.set_compact_mode(False)' in GUI
    assert 'self.history_combo.setVisible(True)' in GUI
    assert 'self.history_combo.setVisible(not compact)' not in GUI
    assert 'Simple mode hides the explanatory paragraph but must not reduce' in GUI


def test_v215_lock_button_is_icon_only_and_still_checkable():
    assert 'self.input_lock_btn = QPushButton("\\U0001f513")' in GUI
    assert 'self.input_lock_btn.setText("\\U0001f512" if locked else "\\U0001f513")' in GUI
    assert 'self.input_lock_btn.setFixedWidth(48)' in GUI
    assert 'font-size: 20px;' in GUI
    assert 'Values unlocked' not in GUI
    assert 'Values locked' not in GUI


def test_v215_recovery_details_expand_and_advanced_actions_stay_at_bottom():
    recovery_block = GUI[GUI.index('def _build_recovery') : GUI.index('def _recovery_resume_clicked')]
    assert 'recovery_lower_panel = QWidget()' in recovery_block
    assert 'recovery_lower_layout = QVBoxLayout(recovery_lower_panel)' in recovery_block
    assert 'self.recovery_details.setMinimumHeight(180)' in recovery_block
    assert 'self.recovery_details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)' in recovery_block
    assert 'recovery_lower_layout.addWidget(self.recovery_details, 1)' in recovery_block
    assert 'advanced_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)' in recovery_block
    assert 'advanced_box.setMinimumHeight(104)' in recovery_block
    assert 'advanced_box.setMaximumHeight(132)' in recovery_block
    assert 'recovery_lower_layout.addWidget(advanced_box, 0)' in recovery_block
    assert 'root.addWidget(recovery_lower_panel, 1)' in recovery_block
    assert 'root.addWidget(advanced_box, 0, Qt.AlignBottom)' not in recovery_block
    assert 'self.recovery_details.setMinimumHeight(430)' not in recovery_block


def test_v215_recovery_uses_yellow_for_configured_guard_pauses_not_red():
    assert 'def _is_expected_guard_or_timing_blocker(message: Any) -> bool:' in GUI
    assert 'status_state = "PriceStatusWarning"' in GUI
    assert 'QLabel#PriceStatusWarning' in GUI
    assert 'Trading is paused by a configured guard/session condition.' in GUI
    assert 'Resolution actions are disabled; Refresh from IBKR/TWS and audit export remain available.' in GUI
    assert 'Red is reserved for real broker/local-state inconsistencies' in README
    assert 'trading_text, trading_state = "Guard paused", "waiting"' in GUI
