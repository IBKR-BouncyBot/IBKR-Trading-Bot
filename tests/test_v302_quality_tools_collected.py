from __future__ import annotations

from enum import Enum
from pathlib import Path

from app.models import Stage
from app.storage import BotStorage

ROOT_BAT = Path("run_all_tests.bat").read_text(encoding="utf-8")
QUALITY_SCRIPT = Path("scripts/run_quality_checks.py").read_text(encoding="utf-8")
REQUIREMENTS = Path("requirements.txt").read_text(encoding="utf-8")
PYPROJECT = Path("pyproject.toml").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
ARCHIVE = Path("docs/legacy/README.md").read_text(encoding="utf-8")
DOC = Path("docs/legacy/V3_0_4_QUALITY_GATE_RESULT_FIX.md").read_text(encoding="utf-8")


def test_v302_quality_tools_are_installed_with_local_requirements():
    assert "ruff>=0.8,<1" in REQUIREMENTS
    assert "pyright[nodejs]>=1.1,<2" in REQUIREMENTS
    assert '"ruff>=0.8,<1"' in PYPROJECT
    assert '"pyright[nodejs]>=1.1,<2"' in PYPROJECT
    assert "[project.optional-dependencies]" in PYPROJECT


def test_v302_quality_runner_uses_active_venv_python_instead_of_path_lookup():
    assert "importlib.util.find_spec" in QUALITY_SCRIPT
    assert '[sys.executable, "-m", module' in QUALITY_SCRIPT
    assert "shutil.which" not in QUALITY_SCRIPT
    assert "python -m pip install -r requirements.txt" in QUALITY_SCRIPT
    assert "QUALITY CHECK FAILED" in QUALITY_SCRIPT
    assert "pyright[nodejs]" in QUALITY_SCRIPT


def test_v302_run_all_tests_requires_quality_tools_after_requirements_install():
    assert "required quality checks" in ROOT_BAT
    assert r"scripts\run_tests.ps1" in ROOT_BAT
    assert r"scripts\run_quality_checks.py" in ROOT_BAT
    assert "--require-tools" in ROOT_BAT
    assert "QUALITY CHECKS FAILED" in ROOT_BAT
    assert "QUALITY CHECKS PASSED" in ROOT_BAT
    quality_command = r'"%PYTHON_EXE%" "%~dp0scripts\run_quality_checks.py" --require-tools'
    capture = "set TEST_EXIT_CODE=%ERRORLEVEL%"
    assert quality_command in ROOT_BAT
    assert ROOT_BAT.index(quality_command) < ROOT_BAT.index(capture, ROOT_BAT.index(quality_command))
    assert 'if not exist "%PYTHON_EXE%" goto quality_python_missing' in ROOT_BAT
    assert 'if exist "%PYTHON_EXE%" (' not in ROOT_BAT


def test_v302_documentation_and_version_metadata_are_current():
    assert "# BouncyBot - IBKR Portable Trading Bot v3.0.19" in README
    assert 'version = "3.0.19"' in PYPROJECT
    assert "v3.0.4 quality-gate result handling and cleanup" in ARCHIVE
    assert "v3.0.4 quality-gate result handling and cleanup" in DOC
    assert "ruff" in DOC.lower() and "pyright" in DOC.lower()


def test_v304_storage_json_default_preserves_enum_values_with_typed_getattr():
    class NullValue(Enum):
        ITEM = None

    assert BotStorage._json_default(Stage.WAIT_INITIAL_DROP) == Stage.WAIT_INITIAL_DROP.value
    assert BotStorage._json_default(NullValue.ITEM) is None
