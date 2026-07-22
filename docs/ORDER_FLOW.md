# Order flow and broker interaction

`StrategyEngine` decides the next logical action. `TradingController` verifies current broker/data/guard state and executes that action through `IbAsyncTwsAdapter`. SQLite records intent and results but does not replace broker confirmation.

## Application order identity

All strategy orders receive an `OrderRef` beginning with:

```text
IBKRBOT|
```

The suffix identifies cycle/side intent. Recovery and cancellation operate only on orders with this prefix. Manual orders must not reuse it.

## Before any new BUY

The controller requires, as applicable:

- live local API socket and confirmed Gateway/TWS-to-IBKR server connection;
- completed post-reconnect broker reconciliation;
- qualified contract;
- no unresolved recovery/manual-review state;
- no unsold application-owned quantity from persisted fills;
- valid open/current RTH state;
- selected price delivered by a newly consumed ticker event and still within the configured freshness window;
- current bid/ask and RTH facts when the stale-data guard is enabled;
- live data in live mode when delayed-data blocking is enabled;
- ATR readiness when the ATR entry blocker is enabled;
- session timing, fixed user-configured spread threshold, gap, price, volatility, P/L, cycle, and streak limits;
- a successful IBKR what-if check when enabled;
- a broker-valid minimum-tick-normalized order payload.

The controller returns the first fail-closed blocker to the submission path while the GUI can display the complete evaluated list.

An account-wide external stock position is not part of this BUY block. Only the local unsold quantity reconstructed from application fills is considered.

## Connectivity boundary for every order

BUY, final SELL, protective SELL, and market-close paths check connectivity before preflight/construction and again immediately before the database backup and broker call. A local socket alone is insufficient: upstream IBKR connectivity must be confirmed and post-restoration reconciliation must be complete.

During code 1100/2110, no new order is sent and normal app-order polling is paused. Code 1101 recreates market-data subscriptions; code 1102 keeps them but requires a new event. Existing native orders are not cancelled merely because connectivity is lost and can continue at IBKR. Their later status/fills are recovery facts, not evidence that the application was monitoring continuously.

A risk-reducing SELL based on already-known app fills may not require a new selected-price event, but it still requires a live local socket, confirmed upstream connection, coherent quantity/order state, and completed reconciliation.

## BUY order construction

### Native trailing BUY

For a positive BUY trail, the adapter creates a BUY trailing-stop order with:

- action `BUY`;
- order type `TRAIL`;
- whole-share total quantity;
- configured trailing percent;
- explicit initial trail stop;
- `TIF=GTC`;
- `outsideRth=False`;
- app `OrderRef`;
- account set only when an explicit override is configured.

The controller chooses a stop reference at or above visible ask/last/selected values and rounds up to the contract’s minimum tick.

### Market BUY

When BUY trail is zero, the drop condition produces a market BUY rather than a native trail. The same quantity, account, RTH, what-if, ownership, and guard checks still apply.

### Acceptance and persistence

Before transmission the application creates a database backup. After the adapter returns a submission handle, the controller records the reported IDs, reference, status, payload, and cycle stage. A submission failure rolls the logical cycle back to the waiting stage because the application must not claim an unconfirmed active order.

## BUY fills and partial fills

Order polling and recent-execution recovery can both report fills. Execution IDs are deduplicated in SQLite.

After any positive BUY fill:

- the filled quantity and weighted average price are recorded;
- the app captures the commission when reported;
- remaining BUY quantity is cancelled when possible;
- strategy state proceeds using only the filled quantity;
- a post-fill database backup and RAM market-data capture session are initiated;
- optional protective SELL submission is evaluated.

A partial fill is a real position. The app does not wait indefinitely for the full original quantity before managing the exit.

## Protective SELL flow

When enabled, the controller submits a native SELL trail after a positive BUY fill for the current unsold application-owned quantity.

If it fills, those shares reduce the remaining app quantity. If the normal minimum-profit exit becomes eligible first, the controller:

1. requests protective cancellation;
2. records cancellation-request state;
3. polls until the order is no longer working;
4. re-evaluates protective fills;
5. submits a final SELL only for the remaining quantity.

No final SELL is intentionally sent while another app-created SELL may still execute for the same shares.

## Final SELL order construction

### Native trailing SELL

For a positive SELL trail, the adapter creates a SELL `TRAIL` order with:

- remaining whole-share app-owned quantity;
- configured trailing percent;
- initial stop below the current SELL reference;
- initial stop not below the minimum-profit planning floor;
- minimum-tick rounding downward;
- `TIF=GTC`, `outsideRth=False`, app `OrderRef`, and optional explicit account.

### Market SELL

When final SELL trail is zero, Stage 3 submits a market SELL after the minimum-profit threshold is met.

The configured minimum profit is not a limit price. Both native-stop-triggered and explicit market SELLs can fill below the projected threshold.

### Optional pre-close cancel-and-liquidate path

When enabled for a cycle before Stage 4, the controller supervises a narrow cancel/replace workflow for the normal final `SELL_TRAIL`:

- the date-specific cutoff comes from the current contract's RTH boundary;
- cancellation is requested before any replacement is created;
- broker polling continues during the cancellation race;
- original-trail partial fills are persisted and deducted from the replacement quantity;
- the replacement is one SELL `MKT`, `TIF=DAY`, `outsideRth=False` order with an `RTH_CLOSE_SELL_MARKET` app reference;
- cumulative original and replacement executions determine completion and P/L;
- a full original fill suppresses the replacement;
- an unconfirmed cancellation suppresses the replacement;
- a failed or incomplete replacement moves the cycle to `ERROR` instead of starting a second order or using extended hours.

The operator-requested Stop-screen market close remains a separate workflow. While automatic close-before-RTH liquidation is active, a second manual market-close request is refused to avoid duplicate SELL exposure.

## Market-close stop action

The operator-requested market-close path sells the local unsold application quantity from persisted app fills, not the account-wide broker position. The Stop dialog, main-window close path, and Reconciliation tab use the same quantity source.

Before sending it, the controller cancels any working app-created SELL and waits for broker confirmation that it is no longer working. It then submits one market SELL for the remaining local quantity. This sequencing reduces duplicate-exit risk.

## Account routing

When Account is blank, order construction omits the IBKR account field. TWS/Gateway selects the account according to the connected session. When an explicit account is present, it is added to BUY, SELL, protective, market, trailing, and what-if orders.

In live mode, an explicit account must be among the managed accounts reported by IBKR before a BUY is allowed. Blank is not an error.

## Broker-side versus application-side trailing

Live orders use IBKR-native trailing behavior. After acceptance, TWS/IBKR owns the moving stop and trigger. The application polls and displays diagnostics but does not simulate every broker tick to move the live stop.

`app/simulation.py` uses deterministic app-side trailing logic for tests. It validates strategy rules, not every nuance of TWS order simulation, trigger methods, exchange routing, or gap execution.

## Recovery sources

After a local reconnect, 1101/1102 restoration, or startup recovery, the controller compares:

- persisted active cycle and order records;
- open app-owned orders reported by IBKR;
- recent app-owned executions;
- current contract/account facts;
- local fill ledger and unsold quantity.

It may attach to a known app order, import missing executions, complete a cycle, or require manual review. The cached broker probe is point-in-time: a newer normal terminal poll updates or removes its matching order row, while a later explicit probe that still reports the order remains visible. It does not recreate an order when ownership/state is uncertain.

## Manual intervention boundary

Manual cancellation or selling in TWS can be operationally necessary, but it can leave local state incomplete. Use Reconciliation to refresh and either resume or mark the cycle manually handled. Routine ATR/data/session/spread waits are not manual-intervention states and therefore do not enable recovery-changing buttons. Do not edit the database to imitate a broker event.
