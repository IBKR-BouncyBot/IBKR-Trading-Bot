# v2.12 GUI review and timeline-scaling notes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision is focused on GUI clarity, audit readability, and release validation. It does not change strategy math or IBKR order construction.

## Timeline scaling

The visual buy/sell timeline now separates scaling helpers into `app/timeline_scaling.py`, which can be tested without a Qt runtime. The timeline keeps the price path readable by ignoring API placeholder values such as zero, filtering one-off extreme capture outliers, and excluding impossible stale imported marker prices from the Y-axis range. Legitimate marker levels near the captured path remain included. Markers outside the selected axis are clamped to the plot edge and labeled off-scale instead of stretching the axis.

The X-axis uses ranked chronological positions rather than raw elapsed time. This avoids compressing useful BUY/SELL detail when a cycle has separate capture windows or large gaps between persisted audit events.

## Stop strategy dialog

When TWS reports app-owned open orders, the dialog shows the order-specific actions:

- Cancel open bot orders
- Sell app-bought unsold position with market order
- Leave orders working and recover later
- Stop after current cycle / no next cycle

When no app-owned open TWS orders are visible, those order-specific actions are hidden and the dialog shows:

- Stop strategy now
- Stop strategy and exit app
- Do not stop

The no-order stop path marks the local strategy stopped and sends no broker cancel/order request.

## Windows build path

`scripts/build_windows.ps1` has been reverted to the v2.2-style packaging path. By default it:

1. creates or reuses `.venv`;
2. upgrades pip and installs `requirements.txt`;
3. skips pytest for a faster, more reliable packaging run;
4. invokes PyInstaller through `Start-Process`;
5. verifies that `dist\IBKRTradingBot\IBKRTradingBot.exe` was created.

Use `scripts\build_windows.ps1 -RunTests` or `scripts\build_windows_checked.ps1` only when a checked build is explicitly wanted. Use `scripts\run_tests.ps1` for the separate test/simulation suite.


## v2.12 timeline scaling correction

- Market-capture price rows with zero, negative, NaN, or infinite prices are ignored for the visual timeline.
- The timeline Y-axis uses robust bounds so one stale outlier does not flatten the useful trade path.
- BUY/SELL/trigger markers that are implausibly far from the captured path are drawn at the plot edge and labelled off-scale rather than stretching the axis.
- The X-axis spaces audit items by sorted event rank. This keeps separate capture windows readable when there are large time gaps between entry and exit data.


## Latest correction

The timeline now performs two levels of protection for imported v1.38 debug captures. First, capture ZIP files and rows are matched to the active history cycle before they are loaded. Second, any remaining off-scale path rows are hidden from the plotted line and disclosed as a hidden-row count in the visual timeline.
