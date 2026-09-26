from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")


def _recovery_block() -> str:
    return GUI[GUI.index("def _build_recovery") : GUI.index("def _recovery_resume_clicked")]


def test_v216_version_metadata_and_package_docs_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v5.3.0" in GUI
    assert "# BouncyBot - an IBKR Trading Bot" in README
    assert 'version = "5.3.0"' in PYPROJECT
    assert Path("docs/legacy/V2_20_RECOVERY_GRAPH_RECOVERY_UI.md").exists()


def test_v216_recovery_advanced_actions_do_not_overlay_audit_log():
    block = _recovery_block()
    assert "recovery_lower_panel = QWidget()" in block
    assert "recovery_lower_layout.addWidget(self.recovery_details, 1)" in block
    assert "recovery_lower_layout.addWidget(advanced_box, 0)" in block
    assert "root.addWidget(recovery_lower_panel, 1)" in block
    assert "root.addWidget(advanced_box, 0, Qt.AlignBottom)" not in block
    assert "root.addWidget(self.recovery_details, 2)" not in block


def test_v216_recovery_layout_fits_wrapped_rows_and_scrolls_the_page():
    block = _recovery_block()
    assert "self.recovery_compare_table = ContentFitTable(0, 4)" in block
    assert "self.recovery_compare_table.setMaximumHeight(260)" not in block
    assert "self.recovery_scroll.setWidgetResizable(True)" in block
    assert "self.recovery_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)" in block
    assert "self.recovery_details.setMinimumHeight(180)" in block
    assert "self.recovery_details.setMinimumHeight(430)" not in block
    assert "advanced_box.setMinimumHeight(104)" not in block
    assert "advanced_box.setMaximumHeight(132)" not in block
    assert "Avoid large fixed" in block
    assert "guided_buttons = QGridLayout()" in block
    assert "guided_buttons.addWidget(button, index // 2, index % 2)" in block
