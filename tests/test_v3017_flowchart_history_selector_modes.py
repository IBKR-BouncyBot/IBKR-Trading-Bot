from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
RUN_TESTS_PS1 = Path("scripts/run_tests.ps1").read_text(encoding="utf-8")
RUN_ALL_TESTS_BAT = Path("run_all_tests.bat").read_text(encoding="utf-8")


def test_v3017_version_metadata_and_release_note_are_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.1" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.1"' in PYPROJECT
    assert Path("docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md").exists()


def test_flowchart_history_selector_is_not_hidden_by_simple_mode():
    compact_block = GUI[
        GUI.index("class FlowchartPanel") : GUI.index("def set_history_rows", GUI.index("class FlowchartPanel"))
    ]
    assert 'self.history_combo.setVisible(True)' in compact_block
    assert 'self.history_combo.setVisible(not compact)' not in compact_block
    assert 'self.explanation_label.setVisible(not compact)' in compact_block
    assert 'self.flowchart.set_compact_mode(False)' in compact_block


def test_windows_full_test_runner_does_not_filter_out_any_pytest_marker():
    assert RUN_TESTS_PS1.count('"-m", "pytest"') == 1
    assert '"-m", "not soak"' not in RUN_TESTS_PS1
    assert '"-m", "soak"' not in RUN_TESTS_PS1
    assert "run_tests_soak.log" not in RUN_TESTS_PS1
    assert "No pytest marker filter is applied" in RUN_ALL_TESTS_BAT
