"""v3.6.0 branding, About layout, packaging, and documentation cleanup."""

from __future__ import annotations

import importlib
import struct
import sys
from pathlib import Path

import pytest

from app import paths
from tests.support.qt_stubs import imported_gui_with_stubs

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUI_SOURCE = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
BUILD_SOURCE = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
CURRENT_NOTE = ROOT / "docs" / "V3_6_0_SELL_RECONCILIATION_AND_HISTORY_ROBUSTNESS.md"
LEGACY_DIR = ROOT / "docs" / "legacy"
LOGO_PATH = ROOT / "Images" / "BouncyBot_logo.png"
ICON_PNG_PATH = ROOT / "Images" / "BouncyBot_app_icon.png"
ICON_ICO_PATH = ROOT / "Images" / "BouncyBot_app_icon.ico"


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(ROOT) as module:
        yield module


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_submitted_logo_and_multisize_windows_icon_are_source_assets() -> None:
    assert _png_dimensions(LOGO_PATH) == (1536, 1024)
    assert _png_dimensions(ICON_PNG_PATH) == (1024, 1024)

    ico_header = ICON_ICO_PATH.read_bytes()[:6]
    reserved, icon_type, image_count = struct.unpack("<HHH", ico_header)
    assert reserved == 0
    assert icon_type == 1
    assert image_count >= 7


def test_readme_places_logo_immediately_below_title() -> None:
    lines = README.splitlines()
    assert lines[0] == "# BouncyBot - an IBKR Portable Trading Bot "
    assert lines[2] == '<p align="center">'
    assert 'src="Images/BouncyBot_logo.png"' in lines[3]
    assert lines[4] == "</p>"


def test_about_dialog_contains_logo_links_version_and_readme_support_data(gui_module) -> None:
    dialog = gui_module.AboutInfoDialog()

    assert dialog.title_label.text() == "BouncyBot - IBKR Portable Trading Bot"
    assert dialog.version_label.text() == "Version 3.6.0"
    assert gui_module.BOUNCYBOT_GITHUB_URL in dialog.repository_link.text()
    assert gui_module.BOUNCYBOT_REFERRAL_URL in dialog.referral_link.text()
    assert set(dialog.support_address_fields) == {
        "Cardano / ADA",
        "Midnight / NIGHT",
        "Ethereum / ETH",
        "Solana / SOL",
    }
    for label, address in gui_module.BOUNCYBOT_SUPPORT_ADDRESSES:
        assert dialog.support_address_fields[label].text() == address
        assert address in README


def test_about_logo_uses_a_bounded_panel_before_the_title_rows() -> None:
    about_source = GUI_SOURCE[
        GUI_SOURCE.index("class AboutInfoDialog") : GUI_SOURCE.index("class StopDialog")
    ]
    assert 'self.logo_panel.setObjectName("AboutLogoPanel")' in about_source
    assert "self.logo_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)" in about_source
    assert "self.logo_label.setAlignment(Qt.AlignCenter)" in about_source
    assert "self.logo_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)" in about_source
    assert "scaled_logo = pixmap.scaled(520, 240" in about_source
    assert "self.logo_label.setFixedSize(scaled_logo.size())" in about_source
    assert "logo_height = max(180, int(scaled_logo.height()) + 14)" in about_source
    assert "self.logo_panel.setMinimumHeight(logo_height)" in about_source
    assert "self.logo_panel.setMaximumHeight(logo_height)" in about_source
    assert "layout.addWidget(self.logo_panel)" in about_source
    assert "layout.addWidget(self.logo_panel," not in about_source
    assert "self.title_label.setWordWrap(True)" in about_source


def test_about_menu_opens_info_dialog(gui_module, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class Dialog:
        def __init__(self, parent: object) -> None:
            calls.append(parent)

        def exec(self) -> int:
            calls.append("exec")
            return 0

    monkeypatch.setattr(gui_module, "AboutInfoDialog", Dialog)
    window = object.__new__(gui_module.MainWindow)
    gui_module.MainWindow._show_about_info(window)

    assert calls == [window, "exec"]
    assert 'self.menuBar().addMenu("About")' in GUI_SOURCE
    assert 'QAction("Info", self)' in GUI_SOURCE


def test_resource_path_resolves_source_and_pyinstaller_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(paths.sys, "_MEIPASS", raising=False)
    assert paths.resource_path("Images", "BouncyBot_logo.png") == LOGO_PATH

    monkeypatch.setattr(paths.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_path("Images", "BouncyBot_logo.png") == (
        tmp_path / "Images" / "BouncyBot_logo.png"
    )


def test_main_applies_branding_icon_when_asset_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = sys.modules.pop("main", None)
    try:
        with imported_gui_with_stubs(ROOT):
            module = importlib.import_module("main")
            icons: list[object] = []

            class App:
                def setWindowIcon(self, icon: object) -> None:
                    icons.append(icon)

            monkeypatch.setattr(module, "resource_path", lambda *parts: ICON_PNG_PATH)
            module._apply_application_icon(App())
            assert len(icons) == 1
    finally:
        sys.modules.pop("main", None)
        if previous is not None:
            sys.modules["main"] = previous

    assert "_apply_application_icon(app)" in MAIN_SOURCE


def test_windows_build_bundles_only_runtime_images_and_omits_source_images_root() -> None:
    assert '"--icon=Images\\BouncyBot_app_icon.ico"' in BUILD_SOURCE
    assert '"--add-data=Images\\BouncyBot_app_icon.png;Images"' in BUILD_SOURCE
    assert '"--add-data=Images\\BouncyBot_logo.png;Images"' in BUILD_SOURCE
    assert 'Copy-Item -Path (Join-Path $root "Images")' not in BUILD_SOURCE
    assert 'Get-ChildItem -Path $guiTarget -Recurse -File -Filter "BouncyBot_app_icon.png"' in BUILD_SOURCE
    assert 'Get-ChildItem -Path $guiTarget -Recurse -File -Filter "BouncyBot_logo.png"' in BUILD_SOURCE
    assert 'if (Test-Path (Join-Path $releaseRoot "Images"))' in BUILD_SOURCE


def test_windows_build_creates_release_root_shortcut_and_checksums_it() -> None:
    assert '$shortcutPath = Join-Path $releaseRoot "BouncyBot.lnk"' in BUILD_SOURCE
    assert '$relativeExePath = "GUI\\$appName.exe"' in BUILD_SOURCE
    assert "interface IShellLinkW" in BUILD_SOURCE
    assert "shellLink.SetPath(absoluteTarget)" in BUILD_SOURCE
    assert "shellLink.SetRelativePath(relativeTarget, 0)" in BUILD_SOURCE
    assert "[BouncyBot.Build.PortableShortcut]::Create(" in BUILD_SOURCE
    assert "WScript.Shell" not in BUILD_SOURCE
    assert "foreach ($file in @($releaseExePath, $shortcutPath, $releaseZip))" in BUILD_SOURCE
    assert "Double-click BouncyBot.lnk" in BUILD_SOURCE
    assert "GUI\\IBKRTradingBot.exe" in BUILD_SOURCE


def test_only_current_release_note_remains_in_docs_root() -> None:
    root_version_notes = sorted(path.name for path in (ROOT / "docs").glob("V3_*") if path.is_file())
    assert root_version_notes == [CURRENT_NOTE.name]
    assert (LEGACY_DIR / "V3_2_2_GUI_INFORMATION_AND_AUDIT_LAYOUT.md").is_file()

    for name in (
        "V3_0_17_FLOWCHART_HISTORY_SELECTOR.md",
        "V3_0_18_EVENT_DRIVEN_CADENCES.md",
        "V3_0_19_TRADE_HISTORY_AUDIT_PERFORMANCE.md",
    ):
        assert (LEGACY_DIR / name).is_file()
        assert not (ROOT / "docs" / name).exists()
