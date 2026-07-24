# Operations guide

This guide describes the normal operator workflow for v3.2.0. It does not replace the broker’s API documentation or account controls.

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
6. Select the intended exact API result. It must be an ordinary `STK` contract in USD or EUR with a positive conId. BouncyBot keeps routing on `SMART` and copies the result's primary exchange, currency, and conId into the read-only contract fields.
7. Click **3. Confirm ticker + get price**.
8. Review the local/upstream connection state, selected currency and conId, price source, data type, bid/ask, actual update age/sequence, contract minimum tick, quantity rules, and RTH status. The route-specific market rule is resolved at order-preflight time and recorded in the order/audit diagnostics. A populated cached quote is not a fresh update.

Contract `minTick` is not treated as universally valid when IBKR advertises a market rule. Before a priced order is transmitted, BouncyBot loads the rule for the selected route and normalizes the proposed price to the applicable band. If that broker metadata cannot be resolved, the order is blocked rather than guessed.

Do not infer contract identity from the symbol alone. Search again after manually editing the ticker or primary exchange; manual edits clear the exact conId selection. The live adapter verifies that qualification returns the selected conId, currency, ordinary STK type, SMART capability, required order types, and usable session metadata.


### Portable database currency

Each portable SQLite database uses one contract currency. A new database may switch between USD and EUR while it has no cycles. The first persisted cycle locks the database to that currency. When upgrading an existing v3.1.2 database, BouncyBot infers and records the lock from its historical cycles; a normal v3.1.2 database is therefore locked to USD.

Use a separate portable folder/database for EUR contracts after a USD cycle exists, and vice versa. Do not copy cycles of different currencies into one database. BouncyBot does not convert historical P/L, risk limits, reinvestment, or commissions through FX.

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
- contract-specific RTH status; a non-U.S. contract without usable `liquidHours` and `timeZoneId` is blocked rather than assigned U.S. fallback hours;
- app-owned order status and fill quantities;
- warning/error events;
- Reconciliation state after any disconnect.

The top lock button is an accidental-edit guard. When engaged, editable settings and all five workflow buttons are disabled. It does not stop the worker or cancel an order.

Simple, Advanced, and Debug modes all show **Recovery / audit log** across the full dashboard width. The removed duplicate Controls panel is not needed because the fixed five-button command bar remains visible.

## Expected pauses versus recovery errors

Normal configured pauses, such as ATR warmup, closed RTH, session windows, stale data, or a hard-risk limit, are caution states. They do not mean the local database and broker disagree. Reconciliation therefore disables Reconcile-and-resume, Stop, Cancel, Sell, Leave-working, and Mark-handled actions during an ordinary guard/strategy wait; read-only **Refresh from IBKR/TWS** and audit export remain available.

Red is reserved for actual or suspected broker/local inconsistency, an uncertain recovery state, or a condition requiring operator review.

## Contract and commission validation failures

Start or recovery is blocked when the selected exact conId no longer resolves to the same USD/EUR ordinary stock, when SMART or the required market/trailing order types are unavailable, or when IBKR supplies no safe non-U.S. regular-session schedule. Verify the selected API result and contract details instead of typing a replacement symbol manually.

If IBKR reports a commission in a currency different from the database/cycle currency, the execution remains recorded but that commission is excluded from local net P/L. BouncyBot records a `COMMISSION_CURRENCY_MISMATCH` decision event and disables Auto-repeat for the current cycle because it performs no FX conversion.

## Broker validation failures

If an app-owned BUY becomes `Inactive` or `Rejected` without a fill, BouncyBot stops the cycle in `ERROR` for manual review. It does not automatically return to Stage 1 and resubmit the same structure. Open the Live Strategy event list or Cycle Audit to inspect the retained IBKR error code, message, order reference, and advanced rejection details when provided.

A normal confirmed cancellation is different: `Cancelled` or `ApiCancelled` without a substantive rejection still resets an unfilled Stage-2 setup to Stage 1. IBKR code 202 by itself is treated as the ordinary cancellation notification.

When the optional what-if check is enabled, missing state, validation/rejection status, rejection warnings, unset values, or absent margin/equity output block the BUY. A successful result is only a preflight and does not guarantee that the later live order will be accepted or filled.

## External positions and manual activity

The application does not block a new BUY merely because IBKR reports an existing long position for the same stock. Its local BUY blocker uses unsold quantities reconstructed from application-recorded fills.

Avoid manually selling, modifying, or replacing application-owned shares/orders without recording the outcome through Reconciliation. Manual activity can make the broker account position diverge from the application ledger.

Never create a manual order with an `OrderRef` beginning with `IBKRBOT|`.

## Monitoring optional pre-close liquidation

When **Cancel SELL trail and liquidate before close** is enabled for the active cycle, configure it before the position reaches the cutoff. The controls are locked once the Stage-4 final SELL trail is working. The default cutoff is five minutes before the contract-specific RTH close.

In Stage 3, BouncyBot acts only when a fresh selected current price is strictly above the weighted average BUY price. Commissions are ignored for that eligibility comparison. If no protective SELL is working, the app submits one RTH-only `DAY` market SELL for the app-owned unsold quantity. If a protective SELL is working, the expected sequence is:

1. Stage-3 profitable close workflow started;
2. protective SELL cancellation requested;
3. cancellation or another terminal status confirmed, including any fills during the race;
4. selected price checked again against average BUY;
5. one RTH-only `DAY` market SELL submitted for the remaining app-owned quantity;
6. cumulative SELL fills complete the cycle.

In Stage 4, the expected sequence remains:

1. close-before-RTH workflow started;
2. final SELL-trail cancellation requested;
3. cancellation or another terminal status confirmed;
4. one RTH-only `DAY` market SELL submitted for the remaining app-owned quantity;
5. cumulative SELL fills complete the cycle.

Do not submit a second SELL manually while either sequence is active. The app refuses its own Stop-screen market-close request during the workflow, but independently submitted TWS orders remain outside app ownership controls. If the cycle enters `ERROR`, inspect current IBKR orders, executions, and the account position before taking manual action.

The Stage-3 quote comparison does not guarantee a profitable fill. A market order can execute below the checked quote or average BUY price. The cutoff must leave enough time for broker cancellation acknowledgement and market execution. A one-minute setting is valid but materially increases the chance that the workflow cannot finish before the close.

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

- **Local socket loss:** the application can no longer reach TWS/Gateway. It pauses, discards subscription handles, and retries the same endpoint every 10 seconds indefinitely. Manual **Disconnect** or application shutdown stops those retries.
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

## v3.2.0 paper-account validation

Before live use, validate at least one exact USD SMART stock and one exact EUR SMART stock in a paper account. Confirm contract search, conId qualification, market-data entitlement, market-rule price normalization, whole-share size rules, what-if validation, BUY and SELL order acceptance, commissions, contract-specific RTH status, reconnect/reconciliation, and pre-close behavior. A successful source test run cannot prove venue-specific broker acceptance.

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
