# v2.12 RTH-only ATR, split audit timeline, and safe exit dialog

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## ATR adaptive mode

ATR-adaptive strategy percentages are order-driving variables. In v2.12 the controller only appends prices to the ATR calculation buffer while RTH is open according to the latest contract RTH status. When RTH is closed, ATR status remains visible in the GUI, but adaptive percentage rewrites are paused and the existing strategy values remain in use.

## Cycle audit timeline

The audit Timeline tab now separates market data from app actions:

- Market data graph: captured selected-price path from market-capture ZIP rows.
- App actions graph: anchor, drop, BUY, protective SELL, final SELL, stage transitions, and risk/guard blocks.

Both graphs share the same horizontal timestamp scale when usable timestamps exist. Untimed imported legacy rows use labelled fallback positions. The separate Y-axes avoid the previous problem where app action levels and captured prices could distort each other or appear falsely aligned.

## Stop Strategy dialog

When no active strategy cycle is running and no app-owned open TWS orders are visible, the Stop Strategy dialog now shows **Exit app**. This closes the GUI without sending stop, cancel, or sell commands.
