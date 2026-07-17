# v3.0.16 Reconciliation refresh-first workflow

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release makes the operator sequence explicit and removes the last near-duplicate Reconciliation action. It does not change strategy or broker-order calculations.

## Three-step workflow

The Reconciliation tab is organized as:

1. **Refresh current broker facts** — **Refresh from IBKR/TWS** performs a read-only query for app-owned open orders, the account position for the active contract/account, and recent executions.
2. **Compare SQLite with IBKR/TWS** — the table shows the local cycle, order/fill ledger, broker facts, and the safest interpretation.
3. **Resolve the situation** — broker-dependent actions become available only when the refresh is current.

The upper guided controls are now:

- **Reconcile and resume**;
- **Stop after current cycle**;
- **Cancel visible app-owned orders**;
- **Mark manually handled**.

The Advanced row contains only the distinct **Sell app-bought unsold position** and **Leave orders working** actions. The duplicate lower cancellation button was removed.

## Refresh validity

The status beside **Refresh from IBKR/TWS** reports one of:

- **Not refreshed**;
- **Current**;
- **Stale**;
- **Refresh failed**.

A refresh is current only when all of the following remain true:

- it completed no more than 60 seconds ago;
- the API connection is still active and the probe did not report a component error;
- the probe belongs to the current local cycle, including the no-active-cycle case;
- reconciliation-relevant local stage, recovery, order, and fill facts still match the signature captured by the probe;
- no disconnect or upstream-connectivity loss has invalidated the probe;
- no later broker order-status update has superseded the full probe.

Ordinary market-price and editable-percentage updates are excluded from the signature, so a live quote does not immediately invalidate an otherwise current refresh. A fill, order-status change, stage transition, changed cycle, or recovery-flag change does invalidate it.

A failed refresh displays its failure time and preserves the preceding successful refresh timestamp for context. The failed attempt is never treated as current.

## Action gating

The following actions require a current refresh and perform the same check again when clicked:

- **Reconcile and resume**;
- **Cancel visible app-owned orders**;
- **Sell app-bought unsold position**;
- **Leave orders working**.

**Stop after current cycle** remains available when factually applicable because it sets a local stop-after-cycle intent and does not directly cancel or submit a broker order.

**Mark manually handled** remains an explicit manual-override path. When the refresh is not current, its confirmation states that the app has not verified current broker facts and requires the operator to confirm independent TWS verification. It still sends no broker instruction.

**Export audit bundle** remains available regardless of refresh state.

## Safety boundaries

- Refresh is one-way and read-only; it does not copy broker facts into SQLite as an automatic resolution.
- Cancellation remains restricted to app-owned order references and retains the guided confirmation and orphan-order path.
- No strategy, price, RTH, sizing, order, fill, database, backup, or recovery-matching algorithm was changed.
