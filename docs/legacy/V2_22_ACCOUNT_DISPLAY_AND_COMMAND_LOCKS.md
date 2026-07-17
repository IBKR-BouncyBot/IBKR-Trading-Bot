# v2.24 account display and running-cycle command locks

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This revision fixes two GUI supervision issues found during live use.

## Account display

If the optional Account field is blank, the top status bar now shows the IBKR account ID reported by TWS/IB Gateway through the managed-accounts list. This value is used only for display. The trading/order code still uses `ConnectionSettings.account` exactly as before, so leaving the field blank preserves IBKR default-account behavior.

## Command bar locking

When a strategy cycle is active, command-bar steps 2 and 3 are now marked `DONE` but disabled:

- `2. Search / select ticker` is disabled because the contract identity is fixed for order ownership and recovery.
- `3. Confirm ticker + get price` is disabled because the first usable price/contract confirmation has already been completed for the current cycle.

This makes the state clearer without changing strategy math, broker order construction, adapter behavior, storage behavior, or database schema.
