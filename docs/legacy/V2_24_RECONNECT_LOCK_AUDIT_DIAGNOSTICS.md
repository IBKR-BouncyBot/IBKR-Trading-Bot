# v2.24 reconnect, ATR lock, and readable diagnostics

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

v2.24 focuses on final supervision hardening without changing strategy math or IBKR order construction.

## ATR settings and the top-bar input lock

The top-status-bar lock is an operator accident-prevention control. In v2.24 it also disables ATR adaptive settings:

- ATR adaptive mode toggle
- ATR minimum-profit adaptive toggle
- ATR period
- ATR bar duration
- ATR multipliers
- ATR min/max percentage clamps

The lock still does not stop trading, cancel orders, submit orders, or change order routing.

## Reconnect and market-data subscriptions

IBKR/TWS market-data `Ticker` objects belong to the API session that created them. After a socket disconnect/reconnect, cached ticker objects may remain in memory but no longer receive fresh values.

v2.24 clears cached market-data subscription handles whenever the adapter crosses a disconnected/reconnected session boundary. The next confirmed-ticker price read creates a fresh `reqMktData` subscription. The controller also forces an immediate confirmed-ticker price refresh after reconnect and resets GUI API-data age/counter diagnostics so stale data is easier to spot.

## Human-readable diagnostics beside SQLite

SQLite remains the source of truth. v2.24 also writes best-effort readable diagnostics beside `bot_state.sqlite`:

```text
debug_reports/audit_events_readable.log
debug_reports/latest_state_report.txt
```

`audit_events_readable.log` is append-only and contains readable event lines with timestamp, level, ticker, cycle ID, message, and compact raw JSON when present.

`latest_state_report.txt` is overwritten periodically and on forced snapshots. It summarizes:

- connection status
- strategy settings
- active cycle state
- market-data snapshot and raw API freshness counters
- broker recovery probe
- history summary
- recent audit events
- raw snapshot JSON

These files are intended for support/debugging when screenshots, SQLite data, and imported debug captures do not make the runtime state clear enough.
