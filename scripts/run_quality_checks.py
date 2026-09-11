"""Run the repository's required Ruff and Pyright quality gates.

The Windows full-test launcher installs both tools from ``requirements.txt`` and
executes this script with the virtual-environment interpreter. ``--require-tools``
turns a missing installation into a failed gate; without it, direct/manual runs
may report and skip unavailable tools.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_module(module: str, args: list[str], *, required: bool) -> int:
    if importlib.util.find_spec(module) is None:
        message = f"{module} is not installed in the active Python environment."
        if required:
            print(f"QUALITY CHECK FAILED: {message}")
            print("Run: python -m pip install -r requirements.txt")
            return 1
        print(f"{message} Skipping optional check.")
        return 0
    print("Running", module, " ".join(args))
    completed = subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, check=False)
    if completed.returncode != 0:
        print(f"QUALITY CHECK FAILED: {module} exited with code {completed.returncode}.")
        if module == "pyright":
            print("Pyright requires Node.js. This project depends on pyright[nodejs] so a wheel-provided Node runtime is used where available.")
            print("If this still fails on Windows, run: python -m pip install --upgrade --force-reinstall -r requirements.txt")
    else:
        print(f"QUALITY CHECK PASSED: {module}.")
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-tools", action="store_true", help="fail if ruff or pyright is not installed")
    ns = parser.parse_args()
    failures = 0
    failures += int(run_module("ruff", ["check", "app", "tests"], required=ns.require_tools) != 0)
    failures += int(run_module("pyright", [], required=ns.require_tools) != 0)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
