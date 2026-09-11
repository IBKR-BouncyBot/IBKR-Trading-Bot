# v2.24 Recovery button gating and controlled stop-exit persistence

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

v2.24 fixes two GUI/runtime edge cases found during final recovery testing:

- **Stop strategy and exit app** now waits for the existing worker stop command to persist the local `STOPPED` cycle state before closing the process. This prevents the next app launch from seeing the old active SQLite cycle and incorrectly locking ticker search/confirm controls.
- The Trade Recovery **Resume** button is no longer enabled solely because a stored active cycle is paused behind the startup-resume gate. The Live Strategy tab's **4. Start strategy** button remains the intended path for resuming a deliberately paused cycle. Recovery buttons are reserved for visible broker/local mismatches, app-owned open orders, app-bought unsold quantity, or explicit recovery/manual-review states.

No strategy math, IBKR order construction, broker adapter order behavior, database schema, or storage semantics changed.
