from pathlib import Path


def test_v3018_release_metadata_is_consistent() -> None:
    gui = Path("app/gui.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build_script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    docs_index = Path("docs/README.md").read_text(encoding="utf-8")

    assert "IBKR Portable Trading Bot v3.0.18" in gui
    assert "This is built-in v3.0.18 example data only." in gui
    assert "# IBKR Portable Trading Bot v3.0.18" in readme
    assert 'version = "3.0.18"' in pyproject
    assert '$version = "3.0.18"' in build_script
    assert "## v3.0.18" in changelog
    assert "current v3.0.18 behavior" in docs_index
    assert Path("docs/V3_0_18_EVENT_DRIVEN_CADENCES.md").is_file()
    assert Path("docs/legacy/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md").is_file()
    assert not Path("docs/V3_0_17_FLOWCHART_HISTORY_SELECTOR.md").exists()
