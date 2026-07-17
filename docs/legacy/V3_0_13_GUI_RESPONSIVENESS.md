# v3.0.13 GUI responsiveness and reduced report writes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

> Release note. Current operating behavior remains documented in `README.md`, `OPERATIONS.md`, and the other current guides.

## Scope

This maintenance release changes presentation and diagnostic-write behavior only. It does not change strategy calculations, broker connectivity, order submission or cancellation, fill handling, recovery decisions, SQLite cycle/order/audit persistence, or backup safety checks.

## Incremental GUI updates

Frequently refreshed labels, metric cards, status pills, command cards, strategy panels, and price indicators now compare their next displayed value or style-driving state with the current one before mutating the Qt widget. Stage/input-lock refreshes repolish a widget only when its enabled state or a style property actually changes.

Custom strategy panels also avoid redundant work:

- the ProfitGuard graph skips an identical repaint request;
- the current-stage table is rebuilt only when its displayed rows change;
- the strategy flowchart skips geometry and paint work when its generated cards are unchanged;
- raw API fields are rebuilt only while the raw-fields table is visible.

## Coalesced setting refreshes

Editable strategy controls can emit overlapping value, visual-preview, and autosave signals. Their expensive visual work is now coalesced by a 75 ms single-shot timer. SQLite draft autosave remains a separate 500 ms timer and retains its existing behavior.

## Visible-tab refresh policy

The Live strategy dashboard and Strategy flowchart are refreshed only while their respective tabs are visible. The live price graph still records incoming samples while another tab is selected, but it does not request a hidden repaint. Returning to a tab immediately applies the latest snapshot.

Trade-history and flowchart history views retain a pending-refresh flag so a hidden view is rebuilt once when it next becomes visible, rather than on every snapshot or every tab switch.

## Trade-history filtering

Ticker and date text filters use a 200 ms debounce. Table reconstruction temporarily disables painting and sorting, restores both in a `finally` block, and performs content-based column sizing only once for each newly received history data set. Combo-box filters remain immediate.

## OneDrive-friendly diagnostic report interval

The human-readable `debug_reports/latest_state_report.txt` refresh interval is increased from 10 seconds to 60 seconds. Forced reports still write immediately. This reduces repetitive OneDrive synchronization activity without changing SQLite writes, order/fill audit events, backups, or the human-readable event log.

## Validation

Regression coverage exercises the coalescing callbacks, hidden-tab paths, deferred history refreshes, hidden chart repaint suppression, flowchart change detection, current version metadata, and the 60-second report interval. The unchanged slow generated-state and soak suites remain separate from the focused GUI validation.
