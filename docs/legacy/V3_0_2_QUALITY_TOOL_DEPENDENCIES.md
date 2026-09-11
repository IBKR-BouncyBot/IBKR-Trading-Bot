# v3.0.2 quality-tool dependency collection

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## Change

- Added `ruff>=0.8,<1` and `pyright>=1.1,<2` to `requirements.txt` so the Windows test launcher installed them into `.venv` with the rest of the project dependencies.
- Added matching development extras in `pyproject.toml`.
- Updated `scripts/run_quality_checks.py` to run tools through the active Python interpreter with `python -m ruff` and `python -m pyright` instead of relying on PATH lookup.
- Updated `run_all_tests.bat` to run quality checks with `--require-tools`, making missing tools a test-runner failure after requirements installation.
- Bumped app/package metadata and visible labels to `3.0.2`.

## Reason

`ruff` and `pyright` were previously configured but optional. Because `run_all_tests.bat` invokes `.venv\Scripts\python.exe` directly without activating the virtual environment, PATH-based executable lookup can miss `.venv\Scripts\ruff.exe` and `.venv\Scripts\pyright.exe`. Running the tools as Python modules from the same interpreter is deterministic.

## Superseded by v3.0.3

v3.0.3 keeps the same quality-gate approach but changes the Pyright dependency to `pyright[nodejs]` and cleans up the Ruff/Pyright findings reported by the v3.0.2 test output.
