# v3.0.3 quality gate cleanup

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Implemented changes:

- Bumped app/package metadata and visible labels to `3.0.3`.
- Applied reviewed safe ruff fixes: import ordering, unused imports, unused local variables, and simple return-condition simplifications.
- Fixed dataclass-instance detection before `asdict()` in JSON default handlers.
- Changed pyright dependency to `pyright[nodejs]` so the Python package uses the more reliable wheel-provided Node runtime where available instead of the default nodeenv path.
- Improved `scripts/run_quality_checks.py` and `run_all_tests.bat` so ruff/pyright failures are reported clearly and make the batch file exit non-zero.

No trading strategy formulas, thresholds, stage transitions, or broker order-decision behavior were intentionally changed.
