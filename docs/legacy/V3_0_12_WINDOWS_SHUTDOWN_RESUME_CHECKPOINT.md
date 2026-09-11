# v3.0.12 Windows shutdown resume checkpoint

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

> Historical release note. Current operating behavior is documented in `README.md`, `OPERATIONS.md`, and `RECOVERY_AND_FAILSAFE.md`.

## Purpose

Windows can close an application as part of an update restart, sign-out, battery-triggered orderly shutdown, or another controlled session termination without the operator first using the main-window close button. Earlier builds relied on the ordinary close path and the worker's final backup, so that operating-system path was not explicitly connected to the existing **Exit app and resume/recover later** behavior.

## Behavior

v3.0.12 connects Qt's `commitDataRequest` session-management signal to a direct, non-interactive shutdown handler. The handler:

1. captures the latest connection and strategy values from the GUI;
2. asks the worker to apply safe active-cycle edits and write a resume checkpoint;
3. atomically stores connection settings, strategy settings, the current active cycle, checkpoint metadata, and an audit event in SQLite;
4. requests an online restore-validated backup;
5. returns control to Qt without stopping the worker, disabling periodic draft autosave, or exiting from the session callback. This preserves normal operation if Windows shutdown is cancelled; if shutdown proceeds, normal event-loop cleanup stops the worker and disconnects the API.

The checkpoint uses the same semantics as **Exit app and resume/recover later**. It preserves the active stage and does not mark an active strategy stopped, flatten a position, re-evaluate the last market quote, or submit/cancel a broker order. On the next launch, the stored cycle remains paused until the operator reconnects, reviews reconciliation facts where required, and explicitly starts/resumes monitoring.

A bounded direct-SQLite fallback is used if the worker does not acknowledge the checkpoint in time. The worker and fallback share one checkpoint identifier, making the write idempotent if both paths eventually execute.

## Boundaries

This mechanism applies only when Windows gives applications a graceful session-termination notification. A sudden loss of all power, forced process termination, kernel failure, or storage-device failure cannot execute shutdown code. In those cases the application can recover only from state that SQLite had already committed before the interruption. In-memory ATR observations and incomplete market-data captures remain intentionally non-persistent.

## Maintenance

The three Ruff `I001` failures reported by `run_all_tests.bat` were corrected in:

- `tests/support/deterministic_broker.py`;
- `tests/test_accelerated_soak_bounds.py`;
- `tests/test_crash_restart_and_migration_matrix.py`.
