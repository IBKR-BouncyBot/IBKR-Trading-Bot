# Recovery and fail-safe behavior

Recovery reconciles local application state with app-owned broker facts after startup, reconnect, interrupted order transitions, or manual activity. The objective is not to continue at any cost; it is to avoid creating an order when ownership or status is uncertain.

## Core recovery principles

1. **Ordinary startup remains manual.** A stored active cycle remains visible, but a normal launch requires the operator to connect and explicitly Start/resume monitoring. The only automatic exception is an authenticated immediate watchdog replacement of the same already-running supervision session, and that exception must pass the exact-cycle and normal broker-reconciliation gates described below.
2. **App-owned orders only.** Broker order recovery uses the complete `OrderRef` already persisted by this installation. Recent-execution recovery also supports an omitted legacy reference when a recorded permanent ID proves ownership and available account/contract/side evidence does not conflict. The shared `IBKRBOT|` prefix or numeric order ID alone is not ownership proof.
3. **Executions outrank assumptions.** A recent app-owned execution can update local fill state even when an expected callback was missed.
4. **Unknown state fails closed.** Incomplete broker recovery reads pause and retry without changing the saved stage. A confirmed inconsistency or unresolved order ambiguity requires manual review; the app never invents an order or fill.
5. **One app SELL transition at a time.** A replacement/final/market SELL waits for a potentially working app SELL to be confirmed nonworking.
6. **Local position scope.** The unsold application quantity is reconstructed from persisted app fills, not the account-wide IBKR position.
7. **Probe freshness matters.** A recovery probe is a point-in-time snapshot. A newer terminal broker poll for the same app order supersedes an older working-order row; a later probe that still reports the order remains authoritative and visible.
8. **Guards are not recovery faults.** ATR warmup, spread/session/data guards, and ordinary strategy waits do not expose broker-changing recovery actions unless an independent order, position, or state mismatch also exists.
9. **Connectivity has two layers.** A live local API socket does not prove that Gateway/TWS is connected to IBKR servers. Upstream loss invalidates quote freshness and pauses broker/strategy activity.
10. **Cached quote fields are evidence, not fresh events.** Only a newly delivered ticker event whose relevant price field or quote basis actually updated can refresh that field’s age or drive waiting stages/ATR.
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

Recovery checks connectivity around broker reads and requires completed authoritative open-order, recent-execution and position requests. A timeout, request error, lost connection or temporarily empty managed-account snapshot keeps recovery pending and trading paused; it is not evidence that orders, fills or an account are absent. The existing retry path runs again when the connection is ready. Normal recovery does not clear an existing manual-review hold.

The controller does not cancel a native order solely because connectivity was interrupted. Any status/fill that occurred during the gap is imported when the broker can report it. A restored local socket does not resume strategy processing until upstream connectivity, exact-contract qualification, broker reconciliation, and a new actual market-data event are all confirmed.

## Contract and currency recovery checks

For the live adapter, an active cycle without a positive stored conId cannot be resumed automatically. Qualification must return the same conId, contract currency, ordinary `STK` type, and SMART route. A mismatch or a database currency-lock conflict moves the cycle to `MANUAL_REVIEW` or blocks recovery rather than searching by symbol or rewriting the stored identity.

Older cycles using automatic account selection may have a blank stored account. Recovery can read their original persisted `Fill(...)` evidence without executing that text, verifying its execution ID, exact owned OrderRef and contract against the local records. The proven account must still agree with the configured account when present and belong to the current broker's managed accounts. Conflicting evidence or unavailable proof keeps recovery blocked; a sole managed account or matching account-wide position is insufficient proof of historical ownership. This compatibility check does not reconstruct missing fills, change quantities or automatically clear a previously persisted `MANUAL_REVIEW` state. Preserve an audit bundle when an earlier failed attempt already moved the cycle into manual review; the prior stage must not be guessed.

Every contract requires usable IBKR ContractDetails `liquidHours` and `timeZoneId` for RTH-restricted submissions; missing or unusable metadata fails closed. This schedule comes from contract metadata, not price ticks. BouncyBot does not guess a weekday session or substitute a timezone, and the GUI shows no estimated hours or countdown when an authoritative window is unavailable. For `LSE` and `LSEETF`, the effective boundary is additionally capped at the verified 08:00-16:30 `Europe/London` continuous session so recovery and pre-close replacement logic do not treat a later broker auction/post-continuous endpoint as ordinary RTH.

The database contains only one contract currency. BouncyBot does not convert P/L, risk limits, reinvestment, or commissions through FX. A commission received in another currency is retained for audit, excluded from local net P/L, and disables Auto-repeat for that cycle.

## Broker facts used

Depending on stage and availability, recovery examines:

- open orders whose complete `IBKRBOT|` references match locally persisted ownership;
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
- preserve the first persisted positive-fill time as execution evidence and keep the original partially filled marketable BUY working;
- retain any explicit operator or separately configured pre-close cancellation and reconcile fills while awaiting its terminal result;
- advance to post-BUY management using the recorded fill;
- stop in `ERROR` when an unfilled order is `Inactive`/`Rejected` or carries a substantive broker validation error;
- require review when multiple/conflicting BUY orders or unidentified facts exist.

A partial BUY remains in Stage 2 until the original broker order is terminal. There is no elapsed partial-fill timeout to expire during reconnect or restart. Fills and commissions continue to update the app-owned quantity, weighted average price and execution ledger. Explicit operator cancellation and configured pre-close cancellation remain supervised until broker confirmation; fills racing such a request still count. A previously accepted cancellation cannot be undone by upgrading, and a terminal partial does not cause an automatic top-up order.

A broker rejection is not converted into a fresh entry setup. The rejected order reference and broker identifiers remain attached to the stopped cycle so the operator can reconcile the exact request. A normal confirmed cancellation without a substantive rejection remains recoverable and can reset Stage 2 to Stage 1 when no shares filled, or advance to Stage 3 with the final app-owned quantity when a positive fill exists.

### Post-BUY/protective stage

Recovery accounts for protective SELL status/fills and computes the remaining local quantity. It does not submit a final SELL until a potentially working protective order is safely resolved.

### Final SELL stage

Recovery may reattach to the matching final SELL, import missing SELL executions, complete the cycle when local app quantity is fully sold, or require review when the broker/local quantities or order identities conflict. When a normal order poll reports the final SELL terminal, it updates/removes the matching row in the cached recovery probe so a safe completed cycle is not presented as having an active order.

### Executions hidden by stale or completed-order counters

An order object can exist while its summary fill counters are stale or zero. Starting in 5.6.2, that object no longer prevents recovery from applying the exact matching broker executions. A fully evidenced final SELL updates the local ledger and can complete Stage 5. A partial SELL with no confirmed working exit remains incomplete and requires review; explicit close workflows retain their existing remainder rules. Repeated observations use the execution ID to avoid recording the same shares twice. Valid broker timestamps populate missing fill timestamps, and validated pending commissions are applied only with the matching execution record.

The status label `Filled`, a missing open order or a zero account position alone does not prove a fill's quantity or price. Recovery requires exact recorded order identity through its complete `OrderRef` or, for an omitted legacy reference, a proven recorded permanent ID. Available account, contract and side evidence must not conflict. New completed-order counter normalization requires complete matching attached execution identities and quantity; the separate recent-execution fallback retains the legacy-compatible identity checks. Unresolved or contradictory evidence must remain visible rather than being reported as fully reconciled. If the expected execution is unavailable or a discrepancy persists, preserve the audit and broker execution history. Do not send another SELL simply because the old local cycle still shows Stage 4. See the [5.6.2 release note](legacy/V5_6_2_COMPLETED_ORDER_RECOVERY.md).

## Reconciliation tab

The Reconciliation screen is the operator interface for local-versus-broker comparison. It distinguishes an actionable recovery mismatch from a configured trading pause and presents three explicit steps:

1. **Refresh from IBKR/TWS** — a read-only probe; no order submission, modification, or cancellation.
2. Compare SQLite with current app-owned orders, broker position, and recent executions.
3. Resolve the situation with the applicable action.

The status beside the refresh button reports **Not refreshed**, **Current**, **Stale**, or **Refresh failed**. A successful probe is current for at most 60 seconds and only while it remains connected, error-free, associated with the same active cycle, and matched to the same local stage/order/fill/recovery signature. Price-only updates do not invalidate it; a disconnect, upstream outage, or reconciliation-relevant state change does. A failed attempt retains the preceding successful refresh time for display.

The guided actions are:

- **Reconcile and resume** — rerun the controlled recovery path, including the narrowly supported legacy outage-hold checks described below;
- **Stop after current cycle** — set the local stop-after-cycle intent without direct broker action;
- **Cancel visible app-owned orders** — cancel visible app-owned order(s), not arbitrary account orders;
- **Mark manually handled** — record that the operator resolved the situation outside the application.

The Advanced row contains only **Sell app-bought unsold position** and **Leave orders working**. Reconcile/resume, cancellation, market SELL, and leave-working require a current probe and recheck freshness when clicked. **Mark manually handled** remains a manual override; without a current probe, its confirmation requires independent TWS verification. **Export audit bundle** remains available.

During ATR warmup or another ordinary guard/strategy wait, resolution actions are disabled because there is no recovery mismatch. Use an audit export before manually changing an ambiguous state.

## Legacy outage holds

Version 5.6.1 supports explicit revalidation of two previously persisted recovery reasons: failure to qualify the stored position contract with **Not connected to TWS**, and a cycle account reported as **not confirmed in the broker managed accounts**. This is the **Reconcile and resume** button, also referred to as “Reconcile and continue”. A normal reconnect or Start does not automatically clear these holds. Once explicitly requested, reconciliation may continue across incomplete-read retries for that same cycle. This intention is kept only in memory and is cleared when the attempt finishes or the operator disconnects; a restart does not restore it or automatically release a persisted hold.

The audit must prove the same hold and a preceding waiting stage. Stage 1 must have no order or execution evidence; Stage 3 must have one fully filled BUY with a matching settled execution ledger and no exit/protective-order transition. Fresh broker evidence must confirm the pinned account and exact contract, complete order/execution reads, and a sufficient exact-account/contract position. Restoration is rejected if local evidence changes during those reads. Current holdings alone cannot establish the prior stage or ownership.

The path does not clear arbitrary manual-review reasons, uncertain submissions, working orders, pending exit/cancellation/protection transitions, missing or conflicting audit/ledger evidence, wrong identities or insufficient positions. It records a successful restoration as `OUTAGE_HOLD_RECONCILED` and then applies the ordinary recovery and trading guards. If it remains blocked after a fresh comparison, export a new audit bundle; do not edit SQLite or use **Mark manually handled** merely to remove the hold. See the [5.6.1 release note](legacy/V5_6_1_OUTAGE_RECOVERY.md) for the supplied evidence and upgrade steps.

## Mark manually handled

This action is for a cycle/order/position resolved outside the application. It records the operator decision and removes that cycle from the local unsold-quantity blocker.

It does not:

- cancel an IBKR order;
- sell broker shares;
- verify tax lots;
- rewrite the broker account;
- prove that the external resolution was correct.

Confirm broker state before using it. When the app probe is not current, the confirmation is an explicit manual override and requires independent TWS verification.

### Historical cycles blocking Start

A completed or stopped cycle can remain unresolved when its stored order status is nonterminal or its fills leave shares unsold. Completion alone does not prove the broker order and position are settled. Multiple unresolved cycles continue to block Start and new orders; the status message identifies their cycle numbers, tickers and stages.

**Review historical blockers** is a separate Reconciliation action for these historical cycles. Inspect their audit logs and independently verify the exact IBKR orders, executions and remaining shares first. Recorded totals may be incomplete. If the cycle was handled outside the app, select it and explicitly acknowledge that handling; **No** is the default. The worker checks the exact cycle ID and the confirmed snapshot again before recording the decision, rejecting changed or stale selections.

This records an operator responsibility transfer using the existing `MANUALLY_HANDLED` audit decision. It does not repair fills, cancel orders, sell shares, change the active cycle or automatically resume trading. The ordinary **Mark manually handled** action still targets the current recovery cycle; do not use it to clear a different historical blocker. After resolving all actual discrepancies, Start remains an explicit action subject to the usual broker, ownership and data checks.

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

Cancellation targets only exact app-owned order references, verifies ownership by the current API client, and checks the supplied order ID when present. A cancellation request is not treated as complete until status indicates the order is no longer working.

### Market-close app quantity

Before sending a market SELL, the controller cancels a working app-created protective/final SELL and waits for confirmation. It then sells only the remaining quantity reconstructed from persisted application BUY/SELL fills. The Stop dialog, main-window exit path, and Reconciliation tab use this same ledger, so unrelated account-wide holdings do not create a market-SELL choice.

### Leave orders working / stop without broker action

These choices intentionally transfer responsibility to the operator. Native orders may remain at IBKR while application monitoring is stopped.

### Clean shutdown

Normal worker shutdown writes an audit event and requests a database backup. Closing the main window routes through the stop-choice dialog rather than silently terminating an active strategy. A terminal cycle with no visible app order and no unsold app-ledger quantity is safe to exit without an unnecessary active-order/SELL warning.

A resume checkpoint can preserve unchanged accepted account/session/contract identity while multiple unresolved cycles block trading. A worker-rejected checkpoint is not retried through the direct fallback; an unavailable-worker fallback applies the same identity checks before writing.

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

## v5.0.0 uncertain orders and exact recovery

`SUBMISSION_UNKNOWN` means an order might have reached IBKR. Its exact reference and any returned IDs remain stored with `MANUAL_REVIEW`/`recovery_required`. Reconnect or an empty open-order response does not prove absence and cannot enable automatic retry. Use Reconciliation to inspect exact owned orders, completed orders/executions and position before any operator resolution. Mark manually handled only after the broker exposure has actually been dealt with.

Recovered terminal partial SELLs stay incomplete. Holdings must be sufficient for reconciled app-owned unsold quantity in the exact account/conId; unrelated extra holdings do not invalidate recovery. Legacy blank accounts with exposure require unambiguous owned broker evidence. A new ticker cannot replace an unresolved cycle. Late fills for replaced references update the ledger and may require cancellation/reconciliation of an oversized owned replacement.

Configured protection that is absent, uncertain, or cancelled without a feasible replacement is a recovery condition. This does not close or insure the shares. Protective handoffs check normalized order feasibility before cancellation and require subsequent confirmation/revalidation.

## v5.1.0 recovery corrections

Recovery refuses conflicting execution identifiers even when a numeric order ID matches. An exact-known working order beside a waiting-entry cycle is a reconciliation conflict, not permission to start another BUY.

Mark manually handled acknowledges the cycle and order/fill state actually reviewed. If that state changes while the dialog is open or before the worker processes the command, the acknowledgement is refused and must be reviewed again. Price-only updates do not invalidate it.

The timed-out exit-checkpoint fallback preserves the latest committed cycle under the SQLite transaction lock. It cannot replace a newly accepted order with an older GUI-thread copy. Normal worker checkpoints still apply allowed settings without evaluating the displayed quote.
