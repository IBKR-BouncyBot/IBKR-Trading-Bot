# legacy release final-readiness pass

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release keeps the legacy release trading behavior except for requested default-value
changes and visual compaction of the Strategy flowchart.

## Requested default changes

The following optional hard-risk fields now default to zero, which disables each
specific cap until the user explicitly configures it:

- Max ticker loss/day
- Max total loss/day
- Max cycles
- Max consecutive losses

The hard-risk master switch remains available. Zero-valued individual limits are
intentionally ignored by the controller.

## Strategy flowchart compaction

The five stage cards now use smaller margins, smaller gaps, and wider detail
boxes without reducing the configured font sizes. This allows more of the
flowchart to remain visible in the tab while keeping the right-side detail text
inside its card area.

## Deep-scan fixes

- Removed a duplicated `Flowchart data` label in the Strategy flowchart tab.
- Aligned default arguments in the Strategy input map renderer with the actual
  strategy defaults so optional protective SELL, slippage buffer, and hard-risk
  limits do not default to ON in standalone/default rendering paths.
- Added a regression test for disabled hard-risk cap defaults.
