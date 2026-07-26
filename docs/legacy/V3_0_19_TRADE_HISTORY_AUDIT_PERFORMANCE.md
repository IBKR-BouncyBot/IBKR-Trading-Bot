# v3.0.19 Trade History audit performance and usability

## Scope

v3.0.19 removes the multi-second pause that could occur when a completed trade was selected in **Trade history**. The delay was caused by three synchronous operations occurring before the dialog became visible:

- the human-readable `events` query could scan a large part of the SQLite table because it had no ordered `cycle_id` index;
- market-capture discovery recursively traversed the exact cycle directory, the ticker directory, and the complete capture archive, and a `cycle_1` substring could also select `cycle_10` through `cycle_19` candidates;
- every audit tab, both timeline widgets, every record table, the complete raw-log text, and all matching capture ZIP contents were constructed before `QDialog.exec()` could display the window.

## Changes

- Added ordered SQLite indexes on `events(cycle_id, created_at, id)` and `decision_events(cycle_id, created_at, id)`. Existing databases receive the indexes through the normal additive schema initialization.
- Changed current-format capture discovery to inspect only direct `*.zip` files in `debug_captures/<TICKER>/cycle_<N>/` and return immediately when that authoritative directory contains captures.
- Retained a single legacy/import fallback scan only when the exact current-format directory has no captures. Cycle-folder matching now compares complete path components, so cycle 1 cannot select cycle 10.
- Made the audit dialog build only its lightweight SQLite summary before becoming visible. Timeline, market-capture, order, execution, decision-event, and raw-log tabs are materialized once, when first selected.
- Deferred market-capture ZIP parsing until the operator opens **Timeline** or **Market capture**. Both tabs share the same loaded result, so the ZIP files are not parsed twice.
- Removed the previous 6× application zoom ceiling from the detailed and compact audit timelines. The effective upper boundary is now only Qt's maximum representable widget width; extreme numeric requests are clamped before multiplication to avoid overflow.
- Replaced the sparse built-in history placeholder with a deterministic synthetic AAPL paper-account scenario: a realistic pullback, a two-execution 47-share BUY, a temporary protective SELL, a minimum-profit transition, a trailing SELL exit, commissions, structured decision events, and staged market-capture rows. The row remains explicitly labelled as a sample and is never persisted or included in risk/P&L totals.
- Added an explicit potential-loss confirmation to **5. Stop strategy**. Clicking **Sell app-bought unsold position with market order** now opens a second dialog with **OK** and **Cancel**, defaults to **Cancel**, states that the market fill may realize a loss, and clarifies that unrelated account positions are excluded.
- Changed the current product display name to **BouncyBot - IBKR Portable Trading Bot**. The package identifier follows the new name; the `IBKRTradingBot.exe` filename remains stable to avoid breaking existing shortcuts and portable release procedures.

## Measured regression fixture

A deterministic local benchmark with 300,000 `events` rows and three rows belonging to the selected cycle changed the ordered event query from approximately 2.50 seconds to approximately 0.002 seconds. In a capture fixture containing 500 unrelated ZIPs plus two current-cycle ZIPs, candidate discovery changed from 277 candidates in approximately 0.067 seconds to the two exact files in less than 0.001 seconds. The headless dialog-construction fixture changed from approximately 0.54 seconds to less than 0.001 seconds because capture parsing and non-visible tabs are no longer part of the click-to-dialog path.

These figures are regression-fixture measurements, not guarantees for every computer or database. Actual time still depends on storage speed and the number of audit rows belonging to the selected cycle.

## Safety boundaries

- Strategy calculations, broker connectivity, market-data selection, order construction, order submission after confirmation, fill handling, risk guards, reconciliation, persistence timing, and shutdown behavior are unchanged. The new Stop strategy modal only prevents the existing market-close action from being selected until the operator presses **OK**.
- The new SQLite indexes change query access paths only; they do not modify audit records.
- Audit data remains read-only in the dialog. Deferred tabs use the same persisted rows and capture ZIP validation rules as before. The synthetic example is created in memory only.
- No broker/API request is made when inspecting a completed trade.

## Verification

Regression coverage includes ordered SQLite query-plan assertions, exact-folder capture discovery without recursive archive traversal, cycle 1 versus cycle 10 path isolation, deferred ZIP loading, one-time shared capture loading, zoom beyond 6× and overflow-safe maximum handling, example-trade formula and lifecycle consistency, product-name consistency, explicit Cancel/OK market-SELL confirmation behavior, complete pytest execution, compilation, callable-entry coverage, mutation smoke tests, and deterministic CSV simulations.
