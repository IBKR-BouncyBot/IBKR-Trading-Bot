# v3.6.0 SELL reconciliation and Trade History robustness

**Release:** v3.6.0
**Scope:** fail-closed SELL settlement, Trade History sorting/error handling, and focused regression coverage

## Summary

v3.6.0 reviews and integrates the local v3.5.0 fixes for final/protective SELL polling and Trade History behavior. The release closes several ambiguous quantity states that could otherwise complete a cycle too early or leave an order poll repeating indefinitely, and it makes formatted numeric history columns sort by their underlying values.

The release also adds operator-facing error handling for synchronous audit-detail reads and history exports. A locked database, read-only folder, full disk, or filesystem error is now reported in a dialog instead of escaping the Qt slot.

## Final SELL settlement

A final SELL can complete a cycle only when persisted executions prove all of the following:

- cumulative cycle SELL quantity exactly equals the app-owned BUY-filled quantity;
- no broker remainder is still reported as working;
- the aggregate SELL fill price is available;
- the observed quantity is neither an underfill nor an overfill.

Nonterminal partial fills are persisted and remain under order supervision. Terminal partials enter `Stage.ERROR` with a `SELL_PARTIAL_TERMINAL` decision event. Contradictory quantities, missing aggregate executions, overfills, a full aggregate fill with a reported remainder, or a missing aggregate price enter `Stage.ERROR` with `SELL_QUANTITY_MISMATCH`.

A cancel-and-replace or manually requested market close may intentionally sell only the unsold remainder. For that reason, completion is validated against all app-owned SELL executions for the cycle rather than only the replacement order's cumulative quantity. The aggregate weighted fill price and commission are used for final P/L.

Partially filled orders are still considered working until their broker status is terminal. A manual market-close request therefore cancels and confirms the existing order before it can submit a replacement, preventing overlapping SELL remainders.

## Protective SELL consistency

The protective-SELL completion gate now also rejects a terminal status that reports both the full app-owned filled quantity and a positive remainder. That combination cannot safely be waited on or treated as exact closure, so it enters the existing protective quantity-mismatch path.

The v3.4.0 protections remain in force: intermediate protective partials are persisted and supervised, terminal partials fail closed, and protective completion requires an exact app-owned quantity match.

## Trade History and audit GUI

Formatted numeric cells now retain their operator-friendly currency, percentage, or quantity text while sorting by the raw numeric value. This prevents lexical ordering such as `$1,050.00` appearing before `$85.00`. Blank, invalid, `NaN`, and infinite values are grouped deterministically below finite values.

The GUI now catches and reports failures from:

- opening the selected cycle's audit details;
- exporting the filtered Trade History CSV.

No success dialog is shown after a failed export, and no partially constructed audit dialog is opened after a failed read.

## Compatibility

v3.6.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.5.0 databases, active cycles, execution rows, checkpoints, watchdog state, captures, backups, and portable directories remain compatible.

The release does not change:

- entry calculations, ATR behavior, or configured strategy percentages;
- BUY construction, sizing, slippage handling, or preflight checks;
- normal final/protective SELL order type or quantity construction;
- market-data source priority, quote freshness, or RTH calculations;
- account, contract, `conId`, Client ID, or complete-`OrderRef` ownership rules;
- automatic watchdog restart, database schema, or Windows packaging layout.

The behavioral change is deliberately fail-closed: broker/local SELL facts that do not prove exact closure now require manual reconciliation rather than being interpreted as a completed cycle.

## Verification scope

The focused v3.6.0 regression layer verifies:

- protective terminal full-fill/remainder contradiction handling;
- persistence and continued supervision of nonterminal final-SELL partials;
- terminal final-SELL partial handling with both zero and positive reported remainder;
- final full-fill/remainder contradictions and missing fill-price handling;
- forced-market partial terminal handling without an endless poll loop;
- cancellation of a working partially filled SELL before manual replacement;
- aggregate completion across an earlier partial and a later remainder order;
- fail-closed behavior when the earlier partial cannot be proved from persisted executions;
- numeric history sorting by raw value, including non-finite input handling;
- audit-read and history-export failures being shown to the operator without false success.

The repository implementation report records the complete test, coverage, mutation, simulation, static-analysis, and packaging checks performed for the released source archive.
