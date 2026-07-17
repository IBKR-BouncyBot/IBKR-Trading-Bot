# v2.15 flowchart, recovery, and lock-button polish

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## Operator input lock

The live status bar includes an icon-only lock/unlock toggle. The toggle defaults to unlocked. When enabled, it disables editable configuration widgets that could otherwise be changed accidentally while supervising the bot.

The lock is intentionally GUI-only. It does not stop market-data updates, does not stop an active strategy, does not cancel broker orders, and does not submit broker orders. Start/Stop commands, tab navigation, history review, recovery actions, and the view-mode selector remain available.

## Controlled window close

Clicking the main window close **X** now opens the same **5. Stop strategy** dialog used by the Live strategy tab. This gives the operator an explicit controlled-exit choice instead of immediately closing the app. When no strategy is running and no app-owned open TWS orders are visible, the dialog shows the safe **Exit app** path.

## Important live-tab headings

The Live strategy tab emphasizes the main configuration area with uppercase bold headings:

```text
STRATEGY INPUTS
ENTRY
EXIT
```

This is purely presentational and does not change the settings model.

## RTH wording

Raw regular-trading-hours values are converted to operator-facing text in the GUI. Instead of exposing only booleans or raw adapter messages, the status reads as one of:

```text
RTH open
RTH closed
RTH status unavailable
```

When the adapter provides a check timestamp, the GUI shows the check time in UTC.

## Timekeeping convention

All app-generated timestamps are treated as UTC. This includes SQLite cycle rows, app audit events, market-data capture rows, broker-recovery snapshots, and cycle audit timeline labels. GUI rendering also displays audit timestamps in UTC so imported debug captures and SQLite records can be aligned without relying on the Windows workstation timezone.

Legacy imported rows that contain timezone-less timestamps are interpreted as UTC for audit/timeline display. The raw original value is preserved in source data; the GUI only normalizes the display.

## Default history example

The built-in Trade history example row was refreshed to a v2.15-style successful cycle with UTC order, execution, market-capture, and decision-event timestamps. The example remains UI-only. It is not inserted into SQLite and does not affect summary metrics.


## v2.15 final GUI polish

- Simple mode no longer compresses the Strategy flowchart Full strategy view to three cards; Full strategy always renders the full five-stage flow.
- The top status-bar input lock now uses an icon-only lock/unlock button with tooltip text, reducing visual clutter next to the persistent status pills.
- The Trade recovery audit log now receives more vertical space, with Advanced stop strategy actions anchored at the bottom of the tab.
- Configured guard/session pauses such as outside RTH, max cycles, spread, stale data, or user-enabled guard blocks are shown as caution/yellow in Recovery instead of red recovery errors. Red remains reserved for broker/local-state inconsistencies and manual-intervention states.
