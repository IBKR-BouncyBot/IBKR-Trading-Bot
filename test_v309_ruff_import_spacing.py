from pathlib import Path

TARGET = Path("tests/test_v308_gui_guards_atr_position_scope.py")


def test_v308_import_block_has_single_blank_line_before_constants():
    source = TARGET.read_text(encoding="utf-8")
    expected_boundary = (
        "from tests.test_controller_headless import _install_qt_stub\n\n"
        'GUI = Path("app/gui.py").read_text(encoding="utf-8")'
    )
    assert expected_boundary in source
    assert "from tests.test_controller_headless import _install_qt_stub\n\n\nGUI =" not in source
