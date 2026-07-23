# Recovery and fail-safe behavior

Recovery reconciles local application state with app-owned broker facts after startup, reconnect, interrupted order transitions, or manual activity. The objective is not to continue at any cost; it is to avoid creating an order when ownership or status is uncertain.

## Core recovery principles

1. **No automatic startup resumption.** A stored active cycle remains visible, but the operator must connect and explicitly Start/resume monitoring.
2. **App-owned orders only.** Broker order recovery filters `OrderRef` by `IBKRBOT|`.
3. **Executions outrank assumptions.** A recent app-owned execution can update local fill state even when an expected callback was missed.
4. **Unknown state fails closed.** The application enters recovery-required/manual-review rather than inventing an order or fill.
5. **One app SELL transition at a time.** A replacement/final/market SELL waits for a potentially working app SELL to be confirmed nonworking.
6. **Local position scope.** The unsold application quantity is reconstructed from persisted app fills, not the account-wide IBKR position.
7. **Probe freshness matters.** A recovery probe is a point-in-time snapshot. A newer terminal broker poll for the same app order supersedes an older working-order row; a later probe that still reports the order remains authoritative and visible.
8. **Guards are not recovery faults.** ATR warmup, spread/session/data guards, and ordinary strategy waits do not expose broker-changing recovery actions unless an independent order, position, or state mismatch also exists.
9. **Connectivity has two layers.** A live local API socket does not prove that Gateway/TWS is connected to IBKR servers. Upstream loss invalidates quote freshness and pauses broker/strategy activity.
10. **Cached quote fields are evidence, not fresh events.** Only a newly delivered ticker event can refresh quote age or drive waiting stages/ATR.

## Startup behavior

On launch, storage loads draft settings and any active cycle. A cycle whose `updated_at` is sufficiently old (current threshold: 12 hours) is marked stale and requires explicit reconciliation before normal monitoring can resume.

The application does not place an order merely because SQLite says Stage 2 or Stage 4 was active. It waits for the operator to connect/start and then probes broker state.

## Reconnect behavior

When the local socket disconnects:

- trading is paused;
- cached market-data subscription handles are discarded;
- current quote diagnostics are invalidated for a fresh session;
- order state is not guessed from the disconnect alone;
- the reconnect backoff is used.

When Gateway/TWS remains locally reachable but reports code 1100 or 2110, the controller keeps the local connection fact separate, marks the upstream link unavailable, invalidates cached quote fields, and pauses strategy advancement, app-order polling, and new submissions.

When IBKR reports restoration:

- code 1101 discards old market-data handles because subscriptions were lost;
- code 1102 retains handles but resets their update metadata;
- app-owned open orders and recent executions are reconciled before normal processing resumes;
- a post-recovery ticker event is required before prices become strategy-usable.

The controller does not cancel a native order solely because connectivity was interrupted. Any status/fill that occurred during the gap is imported when the broker can report it.

## Broker facts used

Depending on stage and availability, recovery examines:

- open orders with `IBKRBOT|` references;
- order IDs, permanent IDs, action, quantity, and status;
- recent executions and execution IDs;
- locally recorded orders/executions;
- stored BUY/protective/final SELL quantities and timestamps;
- current account/contract identity;
- local API socket state and upstream IBKR system-message state;
- market-data subscription/update identity and post-recovery freshness;
- local recovery flags and requested stop/market-close state;
- retained app-owned IBKR order errors, including code, message, order identity, and advanced rejection details when supplied.

The account-wide position can be shown as a broker fact, but it is not the entry blocker or authoritative app-owned quantity.

## Stage-oriented outcomes

### Waiting stages

If no app order should exist and none is found, monitoring can continue after normal validation. An unexplained working app order requires review.

### BUY order stage

Recovery may:

- reattach to the matching open app BUY;
- import one or more missing BUY executions;
- cancel a remaining unfilled quantity after a positive fill;
- advance to post-BUY management using the recorded fill;
- stop in `ERROR` when an unfilled order is `Inactive`/`Rejected` or carries a substantive broker validation error;
- require review when multiple/conflicting BUY orders or unidentified facts exist.

A broker rejection is not converted into a fresh entry setup. The rejected order reference and broker identifiers remain attached to the stopped cycle so the operator can reconcile the exact request. A normal confirmed cancellation without a substantive rejection remains recoverable and can reset Stage 2 to Stage 1.

### Post-BUY/protective stage

Recovery accounts for protective SELL status/fills and computes the remaining local quantity. It does not submit a final SELL until a potentially working protective order is safely resolved.

### Final SELL stage

Recovery may reattach to the matching final SELL, import missing SELL executions, complete the cycle when local app quantity is fully sold, or require review when the broker/local quantities or order identities conflict. When a normal order poll reports the final SELL terminal, it updates/removes the matching row in the cached recovery probe so a safe completed cycle is not presented as having an active order.

## Reconciliation tab

The Reconciliation screen is the operator interface for local-versus-broker comparison. It distinguishes an actionable recovery mismatch from a configured trading pause and presents three explicit steps:

1. **Refresh from IBKR/TWS** — a read-only probe; no order submission, modification, or cancellation.
2. Compare SQLite with current app-owned orders, broker position, and recent executions.
3. Resolve the situation with the applicable action.

The status beside the refresh button reports **Not refreshed**, **Current**, **Stale**, or **Refresh failed**. A successful probe is current for at most 60 seconds and only while it remains connected, error-free, associated with the same active cycle, and matched to the same local stage/order/fill/recovery signature. Price-only updates do not invalidate it; a disconnect, upstream outage, or reconciliation-relevant state change does. A failed attempt retains the preceding successful refresh time for display.

The guided actions are:

- **Reconcile and resume** — rerun the controlled recovery path;
- **Stop after current cycle** — set the local stop-after-cycle intent without direct broker action;
- **Cancel visible app-owned orders** — cancel visible app-owned order(s), not arbitrary account orders;
- **Mark manually handled** — record that the operator resolved the situation outside the application.

The Advanced row contains only **Sell app-bought unsold position** and **Leave orders working**. Reconcile/resume, cancellation, market SELL, and leave-working require a current probe and recheck freshness when clicked. **Mark manually handled** remains a manual override; without a current probe, its confirmation requires independent TWS verification. **Export audit bundle** remains available.

During ATR warmup or another ordinary guard/strategy wait, resolution actions are disabled because there is no recovery mismatch. Use an audit export before manually changing an ambiguous state.

## Mark manually handled

This action is for a cycle/order/position resolved outside the application. It records the operator decision and removes that cycle from the local unsold-quantity blocker.

It does not:

- cancel an IBKR order;
- sell broker shares;
- verify tax lots;
- rewrite the broker account;
- prove that the external resolution was correct.

Confirm broker state before using it. When the app probe is not current, the confirmation is an explicit manual override and requires independent TWS verification.

## Close-before-RTH recovery

The workflow state and both order identities are persisted. After an explicit startup/reconnect reconciliation:

- a still-open original trail remains the only exit order and is polled normally;
- a terminal original cancellation can lead to the one remaining-quantity market SELL only while an open RTH session with time remaining is confirmed;
- an open replacement order is recovered and monitored without creating another replacement;
- persisted executions from the original and replacement are aggregated before completion;
- an ambiguous missing order/status is not guessed and falls back to the normal recovery/manual-review controls.

A restart does not waive the RTH requirement. If cancellation is confirmed only after the close, the cycle moves to `ERROR` and no outside-RTH order is submitted.

## Stop and shutdown fail-safes

### Cancel visible app-owned orders

Cancellation targets only app-owned order references. A cancellation request is not treated as complete until status indicates the order is no longer working.

### Market-close app quantity

Before sending a market SELL, the controller cancels a working app-created protective/final SELL and waits for confirmation. It then sells only the remaining quantity reconstructed from persisted application BUY/SELL fills. The Stop dialog, main-window exit path, and Reconciliation tab use this same ledger, so unrelated account-wide holdings do not create a market-SELL choice.

### Leave orders working / stop without broker action

These choices intentionally transfer responsibility to the operator. Native orders may remain at IBKR while application monitoring is stopped.

### Clean shutdown

Normal worker shutdown writes an audit event and requests a database backup. Closing the main window routes through the stop-choice dialog rather than silently terminating an active strategy. A terminal cycle with no visible app order and no unsold app-ledger quantity is safe to exit without an unnecessary active-order/SELL warning.

Before an accepted exit, the app atomically checkpoints the latest connection/strategy drafts and current cycle. A controlled Windows update restart, sign-out, or orderly shutdown invokes that same checkpoint through Qt session management without asking the operator to choose a stop action. This is equivalent to **Exit app and resume/recover later**: it sends no cancel, SELL, or local-stop command, does not re-evaluate the stored quote, and leaves the stored cycle available for explicit recovery on the next start. The worker is not stopped inside the session callback, so a cancelled Windows shutdown leaves the app operational.

The final checkpoint cannot run after an abrupt power cut or forced termination. In that case use the latest committed SQLite state and perform broker reconciliation before resuming.

## Backups and diagnostics

Recovery support includes:

- online SQLite backups that include WAL state;
- integrity and restore-copy validation;
- readable state/event reports;
- broker/decision event tables;
- completed pre/post-fill market-data capture ZIPs;
- audit bundles with manifest and snapshot.

A backup is local application evidence, not proof that a broker order did or did not execute.

## Conditions that remain manual

Manual review is required when facts are incomplete or conflicting, for example:

- multiple app-owned orders for a state that expects one;
- an order reference or execution cannot be associated confidently;
- local unsold quantity conflicts with manual account activity;
- broker recent-execution history is insufficient;
- a cancellation status remains uncertain;
- the contract/account identity changed;
- a stale cycle cannot be matched to current broker facts;
- connectivity returned but app-owned order/execution reconciliation still fails;
- no fresh post-recovery ticker event is arriving after the broker reports connectivity restored.

Do not resolve these by editing SQLite. Preserve the audit bundle and use broker records/TWS order history.
