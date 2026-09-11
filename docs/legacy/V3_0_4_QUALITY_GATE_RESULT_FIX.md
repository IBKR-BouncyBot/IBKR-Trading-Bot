# v3.0.4 quality-gate result handling and cleanup

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Implemented changes:

- Bumped app/package metadata and visible labels to `3.0.4`.
- Fixed `run_all_tests.bat` so `%ERRORLEVEL%` is captured after Ruff/Pyright completes and is not expanded early inside a parenthesized batch block.
- Added regression assertions for the batch control flow that preserves a non-zero quality-check exit code.
- Applied the safe Ruff import ordering and blank-line fixes reported by v3.0.3.
- Removed two unused local assignments in `suggested_hard_risk_defaults`.
- Replaced the Pyright-problematic direct `.value` access with a typed `getattr` fallback in the JSON serializer.

No trading strategy formulas, thresholds, stage transitions, or broker order-decision behavior were intentionally changed.
