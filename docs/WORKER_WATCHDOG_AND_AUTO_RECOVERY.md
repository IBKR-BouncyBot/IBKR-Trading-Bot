# Worker watchdog and automatic recovery

This document defines the current v3.9.0 worker/storage supervision behavior. It preserves BouncyBot's single-controller-worker design. The corrective implementation does not add a broker worker, database-writer worker, service, daemon, or second trading process.

## Failure class addressed

The Qt GUI and the controller worker run in different threads. A blocked or terminated controller worker can therefore leave the GUI responsive while the last price, quote age, connection mode, and RTH text remain frozen. Those cached values are not evidence that trading monitoring is still running.

The corrective watchdog treats delivery of controller snapshots as the independent health signal. The normal worker emits a snapshot every 0.5 seconds. The Qt main thread checks that delivery once per second.

## Watchdog thresholds

| Time without a new controller snapshot | GUI and recovery behavior |
|---:|---|
| Less than 3 seconds | Normal operation. |
| 3 to less than 15 seconds | Amber **Worker delayed** state. Cached facts remain visible only as stale diagnostics. |
| 15 to less than 30 seconds | Red **Worker unresponsive** state. Cached connection/data status is overridden, RTH becomes unknown, displayed quote age continues to increase, and broker-dependent controls are disabled. |
| 30 seconds or more | Request complete process replacement, subject to restart-loop protection. |
| Worker thread is not alive | Request replacement after a one-second startup grace period. |

The GUI-side age calculation is display-only. Trading decisions remain owned by the controller worker and are never made by the watchdog.

## Full-process replacement

A Python thread blocked in SQLite, filesystem I/O, or a third-party call cannot be safely killed. BouncyBot therefore never creates a second worker inside the existing process.

The replacement sequence is:

1. The GUI creates a short-lived one-time handoff file and random token outside SQLite.
2. Qt exits with an internal watchdog exit code.
3. `main.py` asks the controller to shut down and waits for its existing bounded shutdown interval.
4. The portable-folder single-instance lock is released.
5. Windows starts the same source or frozen BouncyBot entry point with a properly quoted `subprocess.Popen` argument list; POSIX uses `os.execv`. Both carry the private token argument.
6. The replacement process consumes and deletes the matching handoff once, before its worker starts.

If Qt cannot exit, restart history cannot be made durable, the request cannot be written, no token is available, or process replacement fails, BouncyBot records the failure in the emergency log and remains fail-closed. It does not launch an overlapping fallback process.

## Exact-cycle automatic continuation

Ordinary application startup remains manual. Loading an active cycle from SQLite does not authorize automatic monitoring or order recreation.

The immediate watchdog replacement is a narrow exception. The handoff marks a cycle eligible only when the final delivered worker snapshot proves all of the following:

- an exact cycle existed in Stage 1, Stage 2, Stage 3, or Stage 4;
- that cycle was already under active monitoring;
- startup resume was not already required;
- recovery/manual review was not already required locally or on the cycle.

Before any automatic continuation, the replacement process re-reads SQLite and requires an exact match for:

- cycle ID;
- stage;
- ticker;
- positive IBKR conId when stored;
- app BUY, protective SELL, and final SELL order references when stored;
- broker-relevant local cycle signature, including recovery, order-status, permanent-ID, fill-quantity, fill-price, and fill-time facts;
- identity as the newest active recovery target.

The controller then invokes the existing Start/reconciliation path. It must reconnect to the configured endpoint, confirm upstream IBKR connectivity, qualify the exact contract, reconcile locally owned open orders and recent executions, account for current position facts, and require fresh market data before ordinary strategy evaluation continues.

The handoff never creates a new cycle and never submits an order simply because the previous process intended one. A changed cycle, changed order reference, missing exact contract, ambiguous broker state, manual position/order activity, incomplete execution history, recovery mismatch, or unavailable fresh quote blocks automatic continuation and leaves the app in manual Reconciliation.

## SQLite storage-fault behavior

A SQLite error is treated as a storage fault rather than a normal strategy exception.

While the fault is active:

- event logging cannot throw back into the worker;
- the error and traceback fall back to `debug_reports/worker_emergency.log`;
- strategy evaluation stops;
- every order placement, cancellation, replacement, recovery mutation, and close-before-RTH broker action is blocked;
- broker callbacks that require durable application state are not applied;
- the IBKR transport remains serviced so the socket is not deliberately abandoned;
- health snapshots continue when the worker itself is still making progress.

The controller periodically opens a separate SQLite connection with a short timeout and attempts:

```text
BEGIN IMMEDIATE
CREATE TABLE IF NOT EXISTS __bouncybot_watchdog_write_probe (...)
INSERT ...
ROLLBACK
```

The transaction is always rolled back. It proves that an actual main-database write can be acquired without adding a lasting table, row, column, index, or migration.

When that probe succeeds, trading remains blocked and the GUI requests complete process replacement. The replacement must still pass exact-cycle and broker reconciliation before monitoring resumes. If the worker dies or stops emitting snapshots while storage is unavailable, hard worker failure takes precedence and process replacement is requested even without a successful probe; the old process can no longer provide supervision.

## Persist-before-publish boundary

For market-driven waiting-stage transitions capable of generating a broker action, BouncyBot now follows this order:

1. calculate the next cycle and proposed actions;
2. persist the next cycle;
3. publish it as the active in-memory cycle;
4. execute the broker action.

If persistence fails at step 2, the previous in-memory cycle remains authoritative and no corresponding broker action is submitted. This closes the failure mode where memory could move ahead of the last committed SQLite state.

## Restart-loop protection

Restart attempts are recorded outside SQLite in `debug_reports/watchdog_restart_history.json`.

- Up to three rapid replacements are permitted within a rolling 15-minute window.
- Once that limit is reached, another replacement must wait five minutes after the most recent attempt.
- Each further attempt during the same rolling window starts another five-minute cooldown.
- If the history file is malformed, unreadable, or unwritable, automatic replacement is blocked fail-closed.

Set the environment variable below before starting BouncyBot to disable automatic process replacement while retaining watchdog warnings and stale-status overrides:

```text
IBKR_BOT_AUTO_RESTART=0
```

Values `false`, `no`, and `off` are also recognized case-insensitively.

## Diagnostics and audit bundles

The corrective implementation uses these files under the portable `debug_reports/` directory:

| File | Purpose |
|---|---|
| `worker_emergency.log` | Best-effort traceback, storage-fault, watchdog, shutdown, and replacement diagnostics independent of SQLite. |
| `watchdog_restart_request.json` | Short-lived one-time handoff for the immediate replacement process. Contains a token and must remain private. |
| `watchdog_restart_history.json` | Rolling restart-attempt timestamps used to prevent uncontrolled loops. |

Audit bundle export includes the emergency log and restart history when present. If a pending handoff exists, the export writes only `watchdog_restart_request_redacted.json`; the token is replaced with `[redacted]` and the raw request is not copied.

## Operational limits

The built-in watchdog can act only while the Qt main event loop is still executing. It cannot recover:

- a frozen Qt GUI/main thread;
- a complete process hang that also prevents the GUI timer from running;
- a Windows crash, power loss, machine freeze, storage-device disappearance, or unavailable executable/source tree;
- a failure during controller construction or other startup work before the GUI watchdog starts;
- TWS/IB Gateway credentials, two-factor approval, platform login, a fully closed Gateway that is not otherwise restarted, or daily maintenance authentication;
- missing or conflicting broker history that the normal reconciliation model deliberately treats as manual.

For machine- or process-level supervision beyond this boundary, an external Windows service manager or scheduled-task policy would be required. Such an external supervisor must still preserve the one-instance rule and must not bypass BouncyBot's reconciliation gates.

## Verification scope

The current corrective tests cover one-time token consumption, exact-cycle signatures, restart rate limiting, emergency logging, real rolled-back SQLite write probes, persist-before-publish ordering, storage-fault broker-action blocking, transport-only callback pumping, dead/stalled worker detection, stale quote-age/RTH display invalidation, lock release before process replacement, exact-cycle reconciliation gating, and audit-token redaction.

A native Windows executable and its `subprocess.Popen` replacement behavior must still be smoke-tested on Windows after running `build_windows.bat`; source tests on Linux cannot produce or execute a Windows PyInstaller binary.
