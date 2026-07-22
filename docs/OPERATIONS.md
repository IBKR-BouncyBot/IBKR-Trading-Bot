# Operations guide

This guide describes the normal operator workflow for v3.1.0. It does not replace the broker’s API documentation or account controls.

## Before starting

1. Use one writable folder for the application, database, and generated data.
2. Start TWS or IB Gateway and complete login and two-factor authentication.
3. Confirm API/socket clients are enabled and read-only API mode is disabled when orders will be sent.
4. Confirm the selected port, API client ID, market-data subscription, and trading permissions.
5. Review any existing application-owned native orders in TWS/Gateway before reconnecting after an interruption.
6. Back up the portable folder before moving it to another machine or replacing the executable.

## Launch

From source, run `run_dev.bat` or `python main.py` inside the prepared virtual environment. From a packaged build, run `IBKRTradingBot.exe` inside its complete onedir folder.

A single-instance lock is created beside the application. If the application reports that another instance is running, verify that no valid process is active before deleting a stale lock manually.

## Connect and qualify a stock

1. Select the correct TWS/Gateway live or paper profile.
2. Enter a custom host/port only when the platform is not using the standard local endpoint.
3. Keep Account blank to let IBKR apply the connected session’s default account, or enter an explicit managed-account override.
4. Click **1. Connect**.
5. Enter the symbol and click **2. Search/select ticker**.
6. Select the intended API contract. Use the primary exchange or conId to resolve ambiguity.
7. Click **3. Confirm ticker + get price**.
8. Review the local/upstream connection state, price source, data type, bid/ask, actual update age/sequence, minimum tick, and RTH status. A populated cached quote is not a fresh update.

Do not infer contract identity from the symbol alone when multiple API matches exist.

## Configure the strategy

Review at least:

- investment amount and whole-share quantity implications;
- manual versus ATR-adaptive percentages;
- ATR warmup state and whether new BUYs are blocked until ready; RTH observations/bars continue warming when adaptation is off, but no percentages are changed;
- zero-trail behavior, which changes the corresponding side to a market order;
- optional protective SELL;
- optional slippage planning assumption;
- auto-repeat and reinvestment;
- RTH, session-timing, stale-data, delayed-data, volatility, and hard-risk controls; set **Maximum spread %** deliberately because it is a fixed saved threshold and is never updated from live bid/ask;
- current application-owned unsold quantity and Reconciliation status.

Changing a draft setting saves it to SQLite. During an active cycle, only settings considered safe for the current stage are applied to the live cycle.

## Start monitoring

Click **4. Start strategy**. A saved active cycle is not silently resumed merely because the application connected; the Start action is the operator’s explicit request to enter/resume the controller path.

Monitor:

- **Trading** status and tooltip for current BUY/SELL blockers;
- current stage and trigger values;
- API actual-update age, update count/sequence, cached-only state, and source;
- local API socket and Gateway/TWS upstream IBKR state;
- RTH status;
- app-owned order status and fill quantities;
- warning/error events;
- Reconciliation state after any disconnect.

The top lock button is an accidental-edit guard. When engaged, editable settings and all five workflow buttons are disabled. It does not stop the worker or cancel an order.

Simple, Advanced, and Debug modes all show **Recovery / audit log** across the full dashboard width. The removed duplicate Controls panel is not needed because the fixed five-button command bar remains visible.

## Expected pauses versus recovery errors

Normal configured pauses, such as ATR warmup, closed RTH, session windows, stale data, or a hard-risk limit, are caution states. They do not mean the local database and broker disagree. Reconciliation therefore disables Reconcile-and-resume, Stop, Cancel, Sell, Leave-working, and Mark-handled actions during an ordinary guard/strategy wait; read-only **Refresh from IBKR/TWS** and audit export remain available.

Red is reserved for actual or suspected broker/local inconsistency, an uncertain recovery state, or a condition requiring operator review.

## External positions and manual activity

The application does not block a new BUY merely because IBKR reports an existing long position for the same stock. Its local BUY blocker uses unsold quantities reconstructed from application-recorded fills.

Avoid manually selling, modifying, or replacing application-owned shares/orders without recording the outcome through Reconciliation. Manual activity can make the broker account position diverge from the application ledger.

Never create a manual order with an `OrderRef` beginning with `IBKRBOT|`.

## Monitoring optional pre-close liquidation

When **Cancel SELL trail and liquidate before close** is enabled for the active cycle, configure it before Stage 4 begins. The Stage-4 controls are locked once the native final SELL trail is working. The default cutoff is five minutes before the contract-specific RTH close.

At the cutoff, monitor the audit/status messages for this sequence:

1. close-before-RTH workflow started;
2. final SELL-trail cancellation requested;
3. cancellation or another terminal status confirmed;
4. one RTH-only `DAY` market SELL submitted for the remaining app-owned quantity;
5. cumulative SELL fills complete the cycle.

Do not submit a second SELL manually while this sequence is active. The app refuses its own Stop-screen market-close request during the workflow, but independently submitted TWS orders remain outside app ownership controls. If the cycle enters `ERROR`, inspect current IBKR orders, executions, and the account position before taking manual action.

The cutoff must leave enough time for broker cancellation acknowledgement and market execution. A one-minute setting is valid but materially increases the chance that the workflow cannot finish before the close.

## Stop actions

Open **5. Stop strategy** and choose the action that matches the intended broker result.

### Cancel open application orders

Requests cancellation of app-owned open orders and stops the local cycle state as defined by the controller. It does not cancel unrelated orders.

### Sell application position at market

Clicking this action opens a second potential-loss confirmation. **Cancel** is the default. Pressing **OK** confirms that the entire app-bought unsold quantity for the active cycle may be sold immediately at an unfavorable price and may realize a loss. Unrelated account positions are not included.

After confirmation, the controller first requests cancellation of any working app-created protective/final SELL and waits until it is no longer working. It then submits one market SELL for the quantity reconstructed from the persisted app fill ledger. External account holdings are not included.

### Leave orders working

Stops local monitoring while leaving accepted native orders at IBKR. The operator assumes responsibility for those orders until the application is reconnected and reconciled.

### Stop after current cycle

Allows the active cycle to finish but prevents automatic repetition.

### Stop now without broker action

Stops the local strategy without cancelling or replacing broker orders. Use only when the broker-side consequences are understood.

Closing the main window invokes the same stop-choice path and uses the same persisted app-owned quantity. A completed cycle with no visible app order and no app-ledger remainder exits without an unnecessary active-order or SELL warning.

## Disconnects and restarts

The application distinguishes two failures:

- **Local socket loss:** the application can no longer reach TWS/Gateway. It pauses, discards subscription handles, and starts the normal reconnect backoff.
- **Upstream IBKR loss while Gateway/TWS remains local:** broker code 1100 or 2110 invalidates all quote freshness and pauses strategy advancement, app-order polling, and new order submission without claiming that the local process disconnected. The status bar shows **Gateway only**. Contract search, ticker confirmation, strategy start, broker refresh, cancellation, and market-close commands are rejected until the upstream link is ready; the workflow bar disables actions that require the broker session.

On restoration:

- **1101 (data lost):** obsolete ticker handles are removed and new subscriptions are created;
- **1102 (data maintained):** existing handles stay in place, but their old update identity is invalidated;
- both paths reconcile app-owned open orders and recent executions before ordinary processing resumes;
- both paths require a new post-recovery ticker event before a quote can advance Stage 1/3 or enter ATR/volatility history.

Native orders already accepted by IBKR are not cancelled merely because the connection is interrupted. They may continue at the broker. A BUY fill received only after recovery can delay application-side follow-up, including protective SELL placement, until broker reconciliation succeeds.

After any outage or restart:

1. Inspect app-owned orders, fills, and positions directly in TWS/Gateway.
2. Confirm the Connection indicator no longer shows **Gateway only** or **Reconciling**.
3. Confirm the Data indicator receives a new actual update rather than showing cached-only/data-pending state.
4. Open Reconciliation and press **Refresh from IBKR/TWS**.
5. Confirm the status says **Current**, then compare the local cycle, order references, fills, position, and executions.
6. Use **Reconcile and resume** only when the comparison is understood. Broker-dependent resolution actions disable again when the probe becomes stale.
7. Use **Mark manually handled** when the position/order was resolved outside the application and the local cycle should no longer block a new entry.

ATR observation history is in-memory only and starts empty after an application or Windows restart. A stale active cycle is intentionally held for explicit reconciliation. The recovery probe itself is point-in-time: normal terminal order polls can retire an older matching probe row; after any TWS-side change, use **Refresh from IBKR/TWS** to obtain a newer authoritative probe.

## Shutdown and data retention

Use the normal close/stop path. An accepted exit writes an atomic resume checkpoint containing the latest editable settings and current cycle, records an audit event, stops the worker, and requests a restore-validated backup.

For an orderly Windows update restart, sign-out, or controlled battery shutdown, Qt's session-management request invokes the same resume-preserving checkpoint without displaying a dialog. No broker order is cancelled or submitted, and an active cycle is not marked stopped. After restarting the app, reconnect, inspect Reconciliation when required, and explicitly click **4. Start strategy** or the applicable resume action.

A sudden loss of all power, forced process kill, operating-system crash, or storage failure cannot execute the shutdown hook. SQLite can then recover only the transactions committed before the interruption. Native IBKR orders may continue independently, so compare the local cycle with broker orders, positions, and executions before resuming.

Keep together:

- `bot_state.sqlite`;
- `backups/`;
- `debug_reports/`;
- any audit bundles needed for investigation;
- completed capture ZIPs relevant to disputed fills.

Do not publish audit bundles or databases without reviewing them for account identifiers and trading data.
