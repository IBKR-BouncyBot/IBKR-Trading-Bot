# v2.13 GUI space and scrollbar polish

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision focuses on GUI layout behavior only. It does not change strategy math, IBKR order construction, database schema, or broker adapter order behavior.

Changes:

- Added visible global Qt scroll-bar styling so scrollable tables and graph panels no longer hide their scroll handles.
- Standardized table sizing through shared helpers. Compact key/value tables fit their rows; larger audit/history tables expose vertical and horizontal scroll bars.
- Polished the cycle-audit Timeline, Market capture, Decision events, Raw log, Recovery comparison, raw API fields, and Trade history tables for better use of available space.
- Kept zoomable timeline graphs scrollable when zoomed in.
- Added a root-level build_windows.bat launcher that delegates to scripts\build_windows.ps1.
- The packaged ZIP name now matches the top-level package folder name.
