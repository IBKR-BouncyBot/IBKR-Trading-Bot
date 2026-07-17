# v2.24 RTH, Live strategy layout, and resume-later exit polish

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

- RTH display now parses IBKR/TWS `liquidHours` when available and shows the regular-session window in both the contract timezone and UTC. The top status bar summarizes the countdown to market close/open when it can be calculated.
- The Live strategy tab now presents the sequence as price monitor, live graph, then detailed market/order/P&L state.
- The Stop strategy dialog has an explicit **Exit app and resume/recover later** path for active cycles. It sends no stop, cancel, or sell command and leaves the SQLite cycle state intact for recovery after restart.

No strategy math, IBKR order construction, broker adapter order behavior, controller behavior, storage behavior, or database schema changed in this revision.
