from pathlib import Path


def test_v3019_release_metadata_is_consistent() -> None:
    gui = Path("app/gui.py").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build_script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    docs_index = Path("docs/README.md").read_text(encoding="utf-8")

    assert "BouncyBot - IBKR Portable Trading Bot v3.1.2" in gui
    assert "This is synthetic v3.1.2 paper-trading example data." in gui
    assert "# BouncyBot - an IBKR Portable Trading Bot " in readme
    assert 'name = "bouncybot-ibkr-portable-trading-bot"' in pyproject
    assert 'version = "3.1.2"' in pyproject
    assert '$version = "3.1.2"' in build_script
    assert "BouncyBot - IBKR Portable Trading Bot $version" in build_script
    assert "## v3.0.19" in changelog
    assert "current v3.1.2 behavior" in docs_index
    assert Path("docs/V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md").is_file()
    assert Path("docs/legacy/V3_0_18_EVENT_DRIVEN_CADENCES.md").is_file()
    assert Path("docs/V3_0_18_EVENT_DRIVEN_CADENCES.md").is_file()
