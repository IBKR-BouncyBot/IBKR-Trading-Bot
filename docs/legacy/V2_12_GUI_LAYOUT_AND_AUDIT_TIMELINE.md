# v2.12 GUI layout and audit-timeline review

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## Changes

- Moved the command/view-mode bar into the Live strategy tab layout instead of keeping it as a global bottom frame. Other tabs no longer need to hide/show that widget.
- Kept the command/view-mode bar fixed below the Live strategy scroll area so it remains visible at the bottom of the Live tab.
- Made the Cycle audit log dialog maximizable and size-grip enabled.
- Added Timeline graph controls: Zoom out, Zoom in, Reset zoom, Ctrl+mouse-wheel zoom, drag panning, scroll bars, and hover crosshairs.
- Changed the audit timeline X-axis so BUY/SELL/stage/risk markers and market-capture rows share one real timestamp scale. Missing timestamps still use stable fallback positions and are disclosed in the chart text.

## GUI bug-hunt notes

Reviewed the GUI code paths most affected by tab switching and button clicks:

- Main window tab switching no longer changes the visibility of a global command bar, removing the layout path that could put the bar below the visible screen after navigating back to Live strategy.
- Live strategy keeps the status bar at the top, the scrollable Live content in the middle, and the command/view-mode bar fixed at the bottom of that tab.
- Trade history still opens a read-only audit dialog from table rows after sorting, using the stored row index in Qt user data.
- Stop strategy still opens the same StopDialog from both the command bar and the dashboard control button.
- Recovery refresh buttons still share the broker-state refresh handler.
- The timeline graph is now in a QScrollArea so zoomed content has normal scroll bars instead of drawing off-screen without navigation.

No trading strategy math, IBKR order construction, database schema, or broker adapter order behavior was changed.
