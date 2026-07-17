# legacy release safety, recovery, and test additions

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release keeps the pre-rebrand legacy release app identity and adds safety/recovery work on top of legacy release.

## New broker and data guards

- IBKR what-if BUY pre-check before transmitting a BUY order.
- Stale-data blocker for selected API price, bid/ask fields, and RTH status.
- Recent-volatility blocker based on the app's rolling observed price buffer.
- Session timing blocker for first/last RTH minutes and active BUY cancellation near close.

## New persistence and recovery features

- Append-only `decision_events` table for strategy decisions, broker submissions, fills, risk blocks, and recovery-required transitions.
- SQLite backups before schema checks, before order submission, after fills, and on app shutdown.
- Single-instance lock file beside the portable app data to avoid two app instances controlling the same SQLite state.
- Explicit `RECOVERY_REQUIRED`/manual-review transition when local state and broker state cannot be reconciled safely.

## Tests added

- Startup default checks.
- Decision-event and backup tests.
- Single-instance lock tests.
- What-if guard failure tests.
- Stale-data, volatility, and session-timing guard tests.
- Failure-injection test for broker submission failure/rollback.
