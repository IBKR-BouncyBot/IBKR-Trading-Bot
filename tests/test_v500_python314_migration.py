"""Interpreter migration checks; no broker, Qt or third-party test tools required."""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_python_runtime

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class Python314RuntimeTests(unittest.TestCase):
    def test_accepts_standard_cpython314(self):
        for gil_disabled in (None, 0, False):
            with self.subTest(gil_disabled=gil_disabled):
                self.assertIsNone(check_python_runtime.runtime_requirement_error((3, 14), "CPython", gil_disabled))

    def test_rejects_other_python_branches(self):
        for version in ((3, 11), (3, 12), (3, 13), (3, 15), (4, 0)):
            with self.subTest(version=version):
                self.assertIn("requires standard CPython 3.14.x", check_python_runtime.runtime_requirement_error(version, "CPython", 0))

    def test_rejects_non_cpython_and_free_threaded_builds(self):
        self.assertIsNotNone(check_python_runtime.runtime_requirement_error((3, 14), "PyPy", 0))
        self.assertIn("free-threaded", check_python_runtime.runtime_requirement_error((3, 14), "CPython", 1))

    def test_command_exit_status_reports_each_runtime_configuration(self):
        for minor, implementation, gil_disabled, expected in (
            (14, "CPython", 0, 0), (12, "CPython", 0, 1),
            (14, "PyPy", 0, 1), (14, "CPython", 1, 1),
        ):
            with self.subTest(minor=minor, implementation=implementation, gil_disabled=gil_disabled):
                with (
                    patch.object(check_python_runtime.sys, "version_info", SimpleNamespace(major=3, minor=minor)),
                    patch.object(check_python_runtime.platform, "python_implementation", return_value=implementation),
                    patch.object(check_python_runtime.platform, "python_version", return_value=f"3.{minor}.0"),
                    patch.object(check_python_runtime.sysconfig, "get_config_var", return_value=gil_disabled),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(check_python_runtime.main(), expected)
                self.assertTrue(output.getvalue().strip())

    def test_real_subprocess_agrees_with_runtime_requirement(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_python_runtime.py")],
            capture_output=True, text=True, check=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            expected = check_python_runtime.main()
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_metadata_and_tooling_target_only_python314(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        self.assertEqual(project["project"]["requires-python"], ">=3.14,<3.15")
        self.assertEqual(project["tool"]["ruff"]["target-version"], "py314")
        self.assertEqual(project["tool"]["pyright"]["pythonVersion"], "3.14")
        self.assertIn("PySide6>=6.10.1,<7", project["project"]["dependencies"])
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        for requirement in project["project"]["dependencies"] + project["project"]["optional-dependencies"]["dev"]:
            self.assertIn(requirement, requirements)

    def test_windows_entrypoints_check_existing_venv_before_package_mutation(self):
        for name in ("run_dev.ps1", "run_tests.ps1", "build_windows.ps1"):
            with self.subTest(script=name):
                source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn('. (Join-Path $PSScriptRoot "python_runtime.ps1")', source)
                self.assertIn('$launcher = @(Resolve-PythonLauncher)', source)
                self.assertLess(source.index("Assert-IbkrPythonRuntime -Python $python"), source.index("-m pip"))
                self.assertNotIn("-3.11", source)

    def test_posix_test_runner_checks_runtime_before_running_tests(self):
        source = (ROOT / "scripts/run_tests.sh").read_text(encoding="utf-8")
        self.assertLess(source.index("python3 scripts/check_python_runtime.py"), source.index("python3 -m compileall"))

    @unittest.skipUnless(POWERSHELL, "PowerShell is not installed on this test host")
    def test_powershell_launcher_resolution_and_existing_venv_failure(self):
        helper_path = str(ROOT / "scripts/python_runtime.ps1").replace("'", "''")
        command = f"""
$ErrorActionPreference = 'Stop'
. '{helper_path}'
$script:pyResult = 0
$script:pythonResult = 0
function py {{
    if ($args[0] -ne '-3.14') {{ throw 'Incorrect Python launcher branch' }}
    $global:LASTEXITCODE = $script:pyResult
}}
function python {{ $global:LASTEXITCODE = $script:pythonResult }}
$launcher = @(Resolve-PythonLauncher)
if (($launcher -join ' ') -ne 'py -3.14') {{ throw 'Expected explicit Python 3.14 launcher' }}
$script:pyResult = 1
$launcher = @(Resolve-PythonLauncher)
if ($launcher.Length -ne 1 -or $launcher[0] -ne 'python') {{ throw 'Verified PATH fallback has wrong shape' }}
$script:pythonResult = 1
$rejected = $false
try {{ Resolve-PythonLauncher }} catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'An incompatible interpreter was accepted' }}
$rejected = $false
try {{ Assert-IbkrPythonRuntime -Python 'python' }} catch {{
    $rejected = $_.ToString().Contains('rename .venv to .venv_previous')
}}
if (-not $rejected) {{ throw 'Old virtual environment needs actionable rejection' }}
"""
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
