# v3.0 reconciliation, backup validation, and test hardening

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Scope: implement operational diagnostics and engineering-quality improvements without changing the pure trading strategy rules.

## Implemented

- Updated project/version metadata to `3.0.0` and GUI/README title to `IBKR Portable Trading Bot v3.0`.
- Renamed the operator recovery tab to `Reconciliation` and made the screen explicitly compare SQLite/local state against IBKR/TWS state.
- Added an `Export audit bundle` action from the Reconciliation screen. The bundle includes:
  - a SQLite online-backup copy,
  - a manifest with restore-validation result,
  - current reconciliation snapshot,
  - recent readable debug logs when present,
  - JSON exports of cycles, orders, executions, events, decision events, and broker events.
- Added restore validation for generated backups using SQLite `PRAGMA integrity_check`, required-table checks, and copy-to-temp restore-candidate validation.
- Kept rotated backups in the existing `backups` folder and write `latest_restore_validation.json` after successful validation.
- Added startup stale-active-cycle detection. A cycle older than 12 hours is flagged in snapshots and the Reconciliation screen; first resume is blocked until the operator explicitly reconciles/resumes.
- Added clean shutdown audit logging before worker shutdown completes.
- Added conservative optional `ruff` and `pyright`/basic typing configuration plus `scripts/run_quality_checks.py`.
- Added deterministic property-style tests for pure strategy invariants.
- Added large-database performance regression tests for history summary cache and bounded history queries.

## Behavior boundaries

- No trading thresholds, ATR calculations, order decision rules, order references, stage-transition formulas, or broker order payload formulas were intentionally changed.
- The only runtime gating change is stale startup-cycle handling, which is an operator safety/reconciliation control.
- Ruff and pyright are optional development tools; the portable runtime dependencies are unchanged.

## Validation

- `pytest -q`: 339 passed.
- `python scripts/run_all_simulations.py`: 18 CSV simulation files/scenarios passed.
- `python -m compileall -q app scripts tests`: passed.
- Static AST audit: no mutable literal defaults or duplicate literal dict keys in `app/*.py`.
- `python scripts/run_quality_checks.py`: runner executed; ruff/pyright were not installed in the execution environment, so optional checks were skipped as designed.
