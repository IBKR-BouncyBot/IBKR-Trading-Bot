# v2.13 GUI polish and root Windows build launcher

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision focuses on GUI layout robustness rather than trading logic.

Changes:

- Tables use a shared sizing/scrollbar policy so small tables fit their content and larger tables expose scroll bars instead of clipping data.
- Audit, recovery, history, raw-log, and raw-market-data tables/text views expose vertical or horizontal scroll bars where content can extend beyond the visible area.
- The Strategy Flowchart canvas is resynchronized after resize/tab changes so Full strategy remains scrollable and stages do not disappear on smaller or high-DPI windows.
- Market-capture audit layout gives the capture preview more vertical space and keeps the summary section bounded.
- A root-level `build_windows.bat` launcher calls `scripts\build_windows.ps1`, so users can start a Windows build from the package root.

No strategy math, order construction, broker-adapter behavior, or database schema was changed.
