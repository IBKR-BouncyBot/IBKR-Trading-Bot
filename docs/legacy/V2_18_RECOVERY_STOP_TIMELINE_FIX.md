# v2.18 Recovery safe-stop and audit layout fixes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision focuses on GUI maturity in the Recovery tab and Cycle audit log.

- A controlled Stage 1 / no-order local stop is no longer shown as a red Recovery/manual-review problem. It is displayed as a consistent stopped state when no app-owned TWS order and no app-bought unsold position are visible.
- Stopped cycles with a remaining app-owned order or app-bought unsold quantity are shown as yellow caution states. They require a deliberate operator choice, but they are not automatically classified as application errors.
- Red Recovery state remains reserved for unresolved broker/local inconsistencies, explicit recovery-required flags, rejected/missing app-owned order states, and other conditions that need manual reconciliation.
- The Cycle audit Timeline graph uses a smaller default canvas and on-demand scroll bars so the graph should fit the default dialog before zooming.
- The Market capture metadata table now displays all Field/Value rows without its own vertical scrollbar; the row preview and file list absorb the remaining vertical space.

No strategy math, IBKR order construction, broker adapter order behavior, controller behavior, or database schema changed in this revision.
