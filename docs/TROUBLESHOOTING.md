# Troubleshooting

## The application cannot connect

Check, in order:

1. TWS or IB Gateway is running and fully logged in.
2. API/socket clients are enabled.
3. The selected profile matches the platform and live/paper session.
4. Host and port match the platform configuration.
5. The API client ID is not already in use.
6. A firewall or security product is not blocking the local socket.
7. The API is not configured read-only when live orders are expected.

Long connection errors wrap in the connection area. Preserve the full error text in an audit report when requesting help.

## Connected, but no usable price appears

Review the Price data monitor and event log:

- contract qualification may be incomplete or ambiguous;
- the account may lack the required market-data subscription;
- the selected data type may be delayed/frozen;
- the market may be closed;
- bid, ask, last, close, and delayed fields may all be unavailable;
- a stale subscription from a disconnected socket may not yet have refreshed.

After reconnect, confirm that the **actual update** timestamp/count/sequence advances. A non-null bid, ask, or last value marked cached-only does not prove that new data is arriving. Reconfirm the ticker when the selected contract changed.

## Gateway is running, but the app shows Gateway only, Reconciling, or Data pending

These states intentionally distinguish the local API socket from the Gateway/TWS connection to IBKR servers:

- **Gateway only:** the local socket is alive, but IBKR reported upstream connectivity unavailable (normally code 1100 or 2110). Trading, app-order polling, strategy advancement, and broker-dependent workflow commands are paused.
- **Reconciling:** code 1101/1102 restored the upstream link, but app-owned open orders and recent executions are still being checked.
- **Data pending:** connectivity/reconciliation is available, but no new post-connect/post-recovery ticker event has arrived. Cached fields remain non-tradeable.

Inspect Gateway/TWS messages and Internet connectivity. After restoration, wait for the state to clear and confirm an actual update arrives. If it does not, disconnect/reconnect the app, reconfirm the ticker, refresh Reconciliation, and inspect market-data permissions. Do not rely solely on a populated cached quote.

## Trading says BUY blocked

Hover the **Trading** box. It lists all currently evaluated blockers, not only the first one. Common reasons include:

- ATR warmup;
- closed or unknown RTH;
- first/last-minute session window;
- stale selected price, bid/ask, or RTH status;
- delayed data in live mode;
- spread, gap, price, volatility, loss, cycle, or streak limit;
- failed IBKR what-if/preflight;
- disconnected local API socket, lost upstream IBKR link, or post-reconnect reconciliation;
- waiting for an actual post-connect/post-recovery ticker event;
- unsold application-owned shares;
- retained order-submission/recovery error.

A blocker is usually intentional. Do not disable it solely to make the status green; verify the underlying data and operating assumption. A normal guard pause is not a recovery fault, so the Reconciliation tab intentionally disables Reconcile and resume and other broker/local-state-changing buttons while leaving Refresh from IBKR/TWS and audit export available.

## A BUY becomes Inactive or Rejected

Open the Live Strategy event list or the Cycle Audit broker/decision events and locate the retained IBKR error code and message. In v3.1.2 a definitive no-fill rejection moves the cycle to `ERROR` and does not automatically retry. This is intentional; restarting the same invalid request can produce repeated broker rejections.

For `Invalid Price`, minimum-variation, or invalid-stop errors:

1. confirm that the selected contract and route are correct;
2. inspect the contract's advertised market-rule ID and the increment selected at the proposed price;
3. confirm the submitted stop is aligned to that increment, not merely to `ContractDetails.minTick`;
4. preserve the audit bundle and Gateway/TWS log; and
5. do not restart the cycle until the structural cause is understood.

A normal operator or session cancellation should report `Cancelled` or `ApiCancelled`. Code 202 by itself is an ordinary cancellation notification and should return an unfilled BUY to Stage 1.

## The what-if check reports a failure

The check now requires a real IBKR `OrderState` with non-error status and at least one finite margin/equity result. `ValidationError`, missing state, rejection warnings, and IBKR unset sentinels fail closed. Review the retained message and Gateway/TWS API log. Do not treat an empty warning as approval when the status is a validation error.

A successful what-if result is only a preflight. A later live order can still be rejected if price, account, permissions, market rules, or broker controls change.

## Maximum spread appears to change by itself

The **Maximum spread %** setting is not derived from live bid/ask and is never rewritten by quote updates. The live spread changes; the configured threshold does not. It can change only when:

- the user edits the field; or
- persisted strategy settings are loaded when the GUI initializes or a saved configuration is restored.

If the displayed threshold still changes without one of those events, confirm the running executable/version and preserve the current settings plus audit bundle. A changing live spread can repeatedly cross a fixed threshold and alternate the blocker state without changing the setting itself.

## ATR never becomes ready

ATR requires application-observed prices during open RTH and at least `period + 1` distinct bar buckets. The newest bucket may still be forming. With the defaults, usable observations must span at least 15 distinct 60-second buckets.

ATR will not warm up from time while the application is closed, from pre/post-market data, or from a separate historical feed. Confirm:

- the regular-session status is open and current;
- usable prices are arriving even if adaptation is currently disabled;
- the selected price timestamp advances;
- the ATR period and bar duration are not set unnecessarily high.

When the warmup blocker is enabled, the initial-drop trigger remains unset until readiness. The readiness update creates a new anchor and cannot itself trigger a BUY. Observation/bar collection continues when adaptation is off, but the in-memory history resets whenever the application restarts and pauses outside RTH.

## An external long position exists, but the app still allows a BUY

This behavior is intentional. The application ignores account-wide external positions for entry blocking and uses only unsold quantities reconstructed from its own fill records.

The broker position and local application ledger are not separate broker lots. Avoid manual activity that makes the local ledger misleading. Use Reconciliation and **Mark manually handled** after resolving an app-recorded quantity externally.

## The app blocks a BUY because it thinks application shares remain

Open Reconciliation and compare:

- the local cycle’s BUY and SELL filled quantities;
- app-owned order references;
- recent IBKR executions;
- whether a SELL was completed manually or in another client.

Refresh from IBKR/TWS. If the app-owned quantity was resolved outside the application and no app order remains, use **Mark manually handled** with an explanatory note. Do not edit SQLite directly.

## A completed max-cycle run still reports an active order or offers SELL

A cycle that reached its completed-cycle cap should remain in `CYCLE_COMPLETE` with auto-repeat stopped. The Stop dialog, window-close path, and Reconciliation tab calculate unsold quantity from the persisted application fill ledger, not the account-wide broker position. A newer terminal order poll also removes the matching older row from the cached recovery probe.

If a warning remains:

1. Refresh from IBKR/TWS.
2. Verify whether the refreshed probe is newer than the local terminal fill and still reports the order.
3. Compare BUY, final SELL, and protective SELL filled quantities in the audit bundle.
4. Treat a genuinely later broker report of a working app order as an inconsistency; do not suppress it manually.

External/manual shares of the same ticker should not create the app SELL option.

## A protective SELL and final SELL transition appears stuck

The controller intentionally waits for IBKR to confirm that the protective SELL is no longer working before it submits the final SELL. This avoids two app-created SELL orders for the same local quantity.

Check TWS/Gateway for the protective order’s actual state, reconnect if necessary, and refresh Reconciliation. Do not manually create another SELL until the working-order state is known.

## The application says recovery is required

Recovery is required when local state cannot be safely matched to broker facts or when a stored active cycle is stale. The application deliberately does not guess.

1. Inspect app-owned orders in TWS/Gateway.
2. Refresh from IBKR/TWS.
3. Compare order references, IDs, statuses, and executions.
4. Export an audit bundle before destructive action.
5. Resume monitoring only when the state is coherent, or mark it manually handled after external resolution.

## A lock-file error appears after a crash

First verify in Task Manager that no application/Python process from the same portable folder is still running. A valid running instance must not be bypassed.

If no process exists, the next launch normally detects and removes a stale PID lock. If it cannot, close all copies and remove `ibkr_trading_bot.lock` from the application folder.

## Tests pass, but Ruff or Pyright fails

`run_all_tests.bat` treats the quality tools as required gates. Read the exact Ruff/Pyright output; pytest success does not override a quality failure.

Reinstall the pinned tool set when the environment is inconsistent:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall -r requirements.txt
```

Then run:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_checks.py --require-tools
```

## PyInstaller output is red, but an executable is produced

The current build wrapper redirects stdout/stderr, displays the combined log, and determines success from the integer process exit code plus existence of `dist\IBKRTradingBot\IBKRTradingBot.exe`. Informational PyInstaller stderr should not create a false failure.

The current build wrapper contains this fix. Inspect `build_pyinstaller.log`: a normal success ends with the portable-build path, while a real nonzero exit or missing executable still fails.

## The packaged executable cannot find its data

PyInstaller creates an onedir application. Keep the complete `dist\IBKRTradingBot` folder together; do not copy only the `.exe`. The database and generated folders are created beside the executable, so the directory must be writable.

## The app appears frozen after pressing Stop or closing

Some stop actions wait for broker cancellation status before submitting a replacement market SELL or finalizing the local state. Check the event log and TWS/Gateway. Avoid force-closing while a cancellation is unresolved unless continued operation is less safe.

## Collecting diagnostics

For a reproducible report, include:

- application version;
- connection profile, mode, host/port, and client ID (redact account IDs as needed);
- exact UTC time of the issue;
- current stage and Trading blockers;
- relevant app order references;
- `debug_reports/latest_state_report.txt`;
- an exported audit bundle;
- the relevant completed market-capture ZIP when available;
- test or PyInstaller log for build problems.

Audit bundles and databases may contain sensitive account/trading information. Share them only through an appropriate secure channel.

## Close-before-RTH liquidation requires manual review

This message means the optional Stage-4 workflow could not prove a safe cancel-confirm-replace sequence before the regular-session boundary. Common causes are an unconfirmed trailing-order cancellation, unavailable contract-hours metadata, a replacement rejection, or a replacement that remained incomplete at the close.

Check TWS/Gateway first. Confirm whether the original trailing SELL or replacement market SELL is still open, cancelled, partially filled, or filled. Do not submit another SELL until the app-owned unsold quantity and every app-created SELL order are reconciled. BouncyBot deliberately does not send an outside-RTH fallback or silently recreate the cancelled trail. Use the Reconciliation tab and export an audit bundle before marking the situation manually handled.


## A partial BUY was followed by more fills during cancellation

This is a normal broker race. v3.1.2 keeps the cycle in Stage 2 until the original BUY order is terminal. Compare IBKR cumulative filled quantity with the cycle BUY quantity and execution table. Duplicate execution IDs should appear only once; late commission reports should enrich the existing row. If a late BUY arrives after an exit order already exists, BouncyBot stops in `ERROR` for manual quantity reconciliation.

## Another BouncyBot instance appears in the Master feed

The common `IBKRBOT|` prefix is not sufficient ownership proof. This installation applies, cancels, and attributes an order only when the complete `OrderRef` already exists in its local SQLite data. Unmatched events can remain in raw broker diagnostics with no cycle link but must not alter the active cycle.

## Stage 3 did not liquidate at the pre-close cutoff

The option acts in Stage 3 only when a fresh selected current price is strictly above the weighted average BUY fill price. Commissions are ignored for that eligibility comparison. If the price is equal or lower, no SELL is submitted at that observation. Even when eligible, the resulting market fill is not guaranteed to remain profitable.
