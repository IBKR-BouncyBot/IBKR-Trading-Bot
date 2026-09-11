# v2.12 GUI bug hunt

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Scope: GUI layout, tab switching, cycle-audit timeline rendering, button visibility, and visible wording. Strategy math, IBKR order construction, database schema, and broker adapter order behavior were not changed in this revision.

## Issues fixed

- The command/view-mode bar was globally shown/hidden when switching tabs. On Windows this could return it below the visible area until the window was maximized. The bar is now parented directly inside the Live strategy tab and is never collapsed to zero height during tab changes.
- The Live strategy tab now refreshes its geometry after tab switches and resize events so the bottom command bar remains inside the visible tab layout.
- The cycle audit timeline dialog is explicitly resizable, minimizable, maximizable, and size-grip enabled.
- The audit timeline now supports mouse hover crosshairs, tooltip coordinates, drag panning, Ctrl+mouse-wheel zoom, and explicit Zoom in / Zoom out / Reset zoom buttons.
- Timeline marker placement now uses actual persisted/captured timestamps on one horizontal time axis. Imported legacy rows with missing timestamps fall back to labelled positions so the chart does not imply false precision.
- The audit timeline is placed in a scroll area, so zooming expands the canvas instead of forcing the graph to compress.

## Static GUI review checklist

Reviewed areas:

- Live strategy tab layout and bottom command bar ownership.
- Strategy input tab sections and command-bar interaction.
- Trade history table filters and audit dialog opening path.
- Cycle audit Summary and Timeline tabs.
- Recovery tab button naming and broker-state refresh labels.
- View mode behavior and raw diagnostics visibility.
- Version labels and package naming.
- Windows development launcher retention.

## Remaining runtime limitation

PySide6 is not installed in the Linux review container, so this revision is validated by static/code-path review, source compilation, automated tests, and simulations. A final visual check on Windows with the user's imported SQLite/debug-capture data is still required.
