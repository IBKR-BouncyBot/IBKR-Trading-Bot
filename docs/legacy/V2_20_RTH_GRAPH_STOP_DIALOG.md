# v2.24 RTH readability, graph placement, and resume-later exit

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision is a GUI maturity pass.

- RTH display text now prefers human-readable regular-hours summaries over raw IBKR liquidHours strings. When the contract liquidHours window and checked UTC timestamp are available, the GUI shows open hours, market close/open time, and time remaining.
- The Live strategy graph is positioned immediately below the Price data monitor and above the Market and strategy state cards. This keeps price feed, chart, calculated state, orders, and P/L in a clearer supervision sequence.
- The Stop strategy dialog includes **Exit app and resume/recover later** while a strategy cycle is active. This closes the app without sending a broker command or changing the persisted cycle. On the next launch, the operator must reconnect and click **4. Start strategy** to resume monitoring/recovery.

No strategy math, IBKR order construction, broker adapter order behavior, controller behavior, storage behavior, or database schema changes are made in this revision.
