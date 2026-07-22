from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
DOC = Path("docs/legacy/V3_0_1_CONNECTION_STATUS_WRAP.md").read_text(encoding="utf-8")


def test_v301_version_metadata_is_current():
    assert "BouncyBot - IBKR Portable Trading Bot v3.1.0" in GUI
    assert "# BouncyBot - an IBKR Portable Trading Bot " in README
    assert 'version = "3.1.0"' in PYPROJECT
    assert "v3.0.1 connection-status wrapping" in DOC


def test_v301_connection_status_label_wraps_long_errors_without_expanding_form():
    status_block = GUI[GUI.index('self.connection_status = QLabel("Disconnected")'):GUI.index('self.db_path_label = QLabel("")')]
    assert "self.connection_status.setWordWrap(True)" in status_block
    assert "self.connection_status.setTextInteractionFlags(Qt.TextSelectableByMouse)" in status_block
    assert "self.connection_status.setMinimumWidth(120)" in status_block
    assert "self.connection_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)" in status_block
