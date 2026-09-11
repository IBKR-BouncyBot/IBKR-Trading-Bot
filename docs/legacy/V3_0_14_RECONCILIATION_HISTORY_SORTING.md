# v3.0.14 reconciliation layout and Trade-history sorting

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This maintenance release changes two GUI behaviors only.

## Reconciliation tab

The Advanced stop strategy actions row now contains only its unique actions:

- Cancel app-owned open orders
- Sell app-bought unsold position
- Leave orders working

The duplicate **Stop after current cycle** and **Refresh broker state** buttons were removed from that lower row. Their guided versions remain available above the SQLite-versus-TWS comparison and keep their existing behavior, tooltips, and permission gating.

## Trade history

The **Cycle** column now stores the cycle number in Qt's numeric `DisplayRole`. Qt therefore compares cycle numbers as integers when the header is clicked. For example, ascending order is `1, 2, 10` rather than `1, 10, 2`; descending order is the reverse.

The row's original visible-history index remains in `UserRole`, so opening a cycle after sorting continues to select the correct audit record.

## Safety boundary

No strategy calculations, IBKR communication, order handling, reconciliation commands, SQLite writes, backups, or trading-state transitions changed.
