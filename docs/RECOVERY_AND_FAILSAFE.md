# Recovery and fail-safe behavior

Recovery reconciles local application state with app-owned broker facts after startup, reconnect, interrupted order transitions, or manual activity. The objective is not to continue at any cost; it is to avoid creating an order when ownership or status is uncertain.

## Core recovery principles

1. **Ordinary startup remains manual.** A stored active cycle remains visible, but a normal launch requires the operator to connect and explicitly Start/resume monitoring. The only automatic exception is an authenticated immediate watchdog replacement of the same already-running supervision session, and that exception must pass the exact-cycle and normal broker-reconciliation gates described below.
2. **App-owned orders only.** Broker order recovery requires a complete `OrderRef` already persisted by this installation; the shared `IBKRBOT|` prefix alone is not ownership proof.
3. **Executions outrank assumptions.** A recent app-owned execution can update local fill state even when an expected callback was missed.
4. **Unknown state fails closed.** The application enters recovery-required/manual-review rather than inventing an order or fill.
5. **One app SELL transition at a time.** A replacement/final/market SELL waits for a potentially working app SELL to be confirmed nonworking.
6. **Local position scope.** The unsold application quantity is reconstructed from persisted app fills, not the account-wide IBKR position.
7. **Probe freshness matters.** A recovery probe is a point-in-time snapshot. A newer terminal broker poll for the same app order supersedes an older working-order row; a later probe that still reports the order remains authoritative and visible.
8. **Guards are not recovery faults.** ATR warmup, spread/session/data guards, and ordinary strategy waits do not expose broker-changing recovery actions unless an independent order, position, or state mismatch also exists.
9. **Connectivity has two layers.** A live local API socket does not prove that Gateway/TWS is connected to IBKR servers. Upstream loss invalidates quote freshness and pauses broker/strategy activity.
10. **Cached quote fields are evidence, not fresh events.** Only a newly delivered ticker event can refresh quote age or drive waiting stages/ATR.
11. **Exact contract and currency identity are durable.** Recovery requires the stored positive conId to resolve to the same USD/EUR ordinary STK contract, and the active cycle must agree with the database's one-currency lock.

## Startup behavior

On launch, storage loads draft settings and any active cycle. A cycle whose `updated_at` is sufficiently old (current threshold: 12 hours) is marked stale and requires explicit reconciliation before normal monitoring can resume.

The application does not place an order merely because SQLite says Stage 2 or Stage 4 was active. It waits for the operator to connect/start and then probes broker state.

## Worker watchdog and replacement-process recovery

The main Qt thread independently times delivery of controller snapshots. The default thresholds are:

| Condition | Behavior |
|---|---|
| No snapshot for 3 seconds | Amber **Worker delayed** state; cached connection/data/RTH facts are treated as aging diagnostics. |
| No snapshot for 15 seconds | Red **Worker unresponsive** state; broker-dependent controls are disabled, connection/data facts become unknown, RTH becomes unknown, and displayed quote age continues to increase. |
| No snapshot for 30 seconds | Request a complete process replacement. |
| Worker thread no longer alive | Request replacement immediately after a one-second startup grace period. |

Replacement is process-level, not thread-level. Qt exits first, controller shutdown is attempted, the portable-folder single-instance lock is released, and Windows starts a properly quoted replacement with `subprocess.Popen` while POSIX uses `os.execv`. BouncyBot never starts a second worker inside the wedged process and never intentionally overlaps two BouncyBot processes for the same portable folder.

The GUI writes a short-lived one-time handoff outside SQLite and passes its random token only to the replacement process. The handoff can authorize automatic continuation only when all of these conditions hold:

- the final delivered worker snapshot showed an active Stage 1, 2, 3, or 4 cycle already under monitoring;
- neither startup-resume nor manual-recovery was already required before the incident;
- the exact persisted cycle ID and stage still match;
- the ticker, positive conId, stored app order references, and broker-relevant local cycle signature still match;
- no different active SQLite cycle has replaced the expected target;
- the existing connection, exact-contract qualification, app-owned order/execution/position reconciliation, and fresh-market-data gates succeed.

The handoff never creates a new cycle and never recreates a missing order merely from local intent. A mismatch, changed persisted fact, failed broker probe, ambiguous position, unavailable recent execution, or missing fresh ticker event leaves the replacement in recovery-required/manual-review state. Ordinary user-launched startups have no watchdog token and therefore remain manual.

Restart-loop history is also outside SQLite. Three rapid replacements are allowed within 15 minutes; further attempts wait five minutes between retries while the window remains populated. If the history file cannot be read or updated, automatic replacement is blocked fail-closed. Set `IBKR_BOT_AUTO_RESTART=0` to disable replacement while keeping watchdog display warnings.

## SQLite storage-fault recovery

A SQLite exception no longer relies on SQLite to report itself. The controller records a best-effort traceback in `debug_reports/worker_emergency.log`, marks storage unhealthy in memory, and blocks every strategy evaluation, order placement, order cancellation, replacement, and broker-state application that would require durable local state. The IBKR socket transport remains serviced and GUI health snapshots continue while the fault is supervised.

A separate short-timeout SQLite connection periodically performs an actual `BEGIN IMMEDIATE`, table creation, and row insert inside a transaction that is always rolled back. This proves that the main database can accept a write without changing its schema or application rows. When the probe succeeds, trading remains blocked and the GUI requests a complete process replacement; the replacement then performs normal exact-cycle broker reconciliation before monitoring can resume. If the worker dies or stops producing snapshots during the storage fault, the hard worker watchdog takes precedence and replaces the process even without a successful probe, because the original process can no longer provide supervision.

## Reconnect behavior

When the local socket disconnects:

- trading is paused;
- cached market-data subscription handles are discarded;
- current quote diagnostics are invalidated for a fresh session;
- order state is not guessed from the disconnect alone;
- the same endpoint is retried every 10 seconds indefinitely; manual **Disconnect** or application shutdown stops the retries.

When Gateway/TWS remains locally reachable but reports code 1100 or 2110, the controller keeps the local connection fact separate, marks the upstream link unavailable, invalidates cached quote fields, and pauses strategy advancement, app-order polling, and new submissions.

When IBKR reports restoration:

- code 1101 discards old market-data handles because subscriptions were lost;
- code 1102 retains handles but resets their update metadata;
- app-owned open orders and recent executions are reconciled before normal processing resumes;
- a post-recovery ticker event is required before prices become strategy-usable.

The controller does not cancel a native order solely because connectivity was interrupted. Any status/fill that occurred during the gap is imported when the broker can report it. A restored local socket does not resume strategy processing until upstream connectivity, exact-contract qualification, broker reconciliation, and a new actual market-data event are all confirmed.

## Contract and currency recovery checks

For the live adapter, an active cycle without a positive stored conId cannot be resumed automatically. Qualification must return the same conId, contract currency, ordinary `STK` type, and SMART route. A mismatch or a database currency-lock conflict moves the cycle to `MANUAL_REVIEW` or blocks recovery rather than searching by symbol or rewriting the stored identity.

For a recognized U.S. primary exchange, the legacy New York RTH fallback remains available when IBKR session metadata cannot be read. A non-U.S. or unknown contract requires usable `liquidHours` and `timeZoneId`; missing metadata fails closed. BouncyBot does not assign U.S. hours to an EUR contract. For `LSE` and `LSEETF`, the effective boundary is additionally capped at the verified 08:00-16:30 `Europe/London` continuous session so recovery and pre-close replacement logic do not treat a later broker auction/post-continuous endpoint as ordinary RTH.

The database contains only one contract currency. BouncyBot does not convert P/L, risk limits, reinvestment, or commissions through FX. A commission received in another currency is retained for audit, excluded from local net P/L, and disables Auto-repeat for that cycle.

## Broker facts used

Depending on stage and availability, recovery examines:

- open orders with `IBKRBOT|` references;
- order IDs, permanent IDs, action, quantity, and status;
- recent executions and execution IDs;
- duplicate/replayed execution callbacks and commission-before-execution ordering;
- locally recorded orders/executions;
- stored BUY/protective/final SELL quantities and timestamps;
- current account, exact conId, contract currency, SMART route, ordinary STK type, required order capabilities, and database currency lock;
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
- audit bundles with manifest and snapshot;
- `worker_emergency.log`, restart-loop history, and a token-redacted watchdog handoff when present.

A backup is local application evidence, not proof that a broker order did or did not execute.

## Conditions that remain manual

Manual review is required when facts are incomplete or conflicting, for example:

- multiple app-owned orders for a state that expects one;
- an order reference or execution cannot be associated confidently;
- local unsold quantity conflicts with manual account activity;
- broker recent-execution history is insufficient;
- a cancellation status remains uncertain;
- the exact conId, contract currency, SMART capability, session metadata, database currency lock, or account identity changed;
- a stale cycle cannot be matched to current broker facts;
- connectivity returned but app-owned order/execution reconciliation still fails;
- no fresh post-recovery ticker event is arriving after the broker reports connectivity restored.

Do not resolve these by editing SQLite. Preserve the audit bundle and use broker records/TWS order history.
