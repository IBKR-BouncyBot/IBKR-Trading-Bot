# Order flow and broker interaction

`StrategyEngine` decides the next logical action. `TradingController` verifies current broker/data/guard state and executes that action through `IbAsyncTwsAdapter`. SQLite records intent and results but does not replace broker confirmation.

## Application order identity

All strategy orders receive an `OrderRef` beginning with:

```text
IBKRBOT|
```

The suffix identifies cycle/side intent. The prefix alone is not ownership proof when several portable installations share a Master API feed. Recovery, cancellation, and callback attribution require the complete `OrderRef` to exactly match a reference already persisted by that installation. Unmatched prefixed orders are left unowned and are never assigned to the active cycle. Manual orders must not reuse an app reference.

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
- a broker-valid order payload normalized to the applicable route-specific IBKR market-rule increment, or to the contract minimum tick only when no market rule is advertised.

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

The controller chooses a stop reference at or above visible ask/last/selected values. When IBKR advertises market rules, the adapter selects the rule for the requested route, requests its price bands, and rounds the stop upward to the increment applicable at that price. The quantity sizing price is normalized independently because a slippage adjustment can cross a market-rule boundary. If the advertised rule cannot be resolved or loaded, submission is blocked. Contract `minTick` is used only when no market rule is advertised.

### Market BUY

When BUY trail is zero, the drop condition produces a market BUY rather than a native trail. The same quantity, account, RTH, what-if, ownership, and guard checks still apply.

### Acceptance and persistence

Before transmission the application creates a database backup. After the adapter returns a submission handle, the controller records the reported IDs, reference, status, payload, and cycle stage. A submission failure rolls the logical cycle back to the waiting stage because the application must not claim an unconfirmed active order.

### What-if validation

When enabled, the BUY preflight uses `IB.whatIfOrder` with `whatIf=True` and `transmit=True`. IBKR interprets the request as a what-if evaluation rather than a live order. The result is accepted only when an `OrderState` exists, its status and warning text do not indicate validation failure, and at least one finite margin/equity impact is present. IBKR unset-value sentinels are treated as missing; legitimate zero changes are retained.

What-if approval is not order acceptance. The market, account, contract, and broker controls can change before the real order is transmitted.

### Broker errors and no-fill rejection circuit breaker

For app-owned orders, the adapter retains order-specific IBKR error callbacks and associates them with the app `OrderRef`, broker order ID, permanent ID, ticker, message, and advanced rejection JSON when supplied. A bounded 30-second callback-race cache handles errors that arrive before the new `Trade` object has been registered. Manual orders are ignored.

If a Stage-2 BUY has no fill and becomes `Inactive` or `Rejected`, or reaches a terminal no-fill state with a substantive broker rejection, the cycle moves to `ERROR` for manual review. BouncyBot does not create a replacement or automatically return to Stage 1. A normal `Cancelled` or `ApiCancelled` result without a substantive rejection still resets the entry setup; code 202 by itself is treated as the ordinary cancellation notification.

## BUY fills, cancellation races, and late callbacks

Order-status polling, execution callbacks, commission callbacks, and recent-execution recovery can report the same economic fill in different orders. SQLite uses the exact IBKR execution ID as the idempotency key. A duplicate callback enriches the existing row rather than adding quantity again. Commission-before-execution callbacks are held briefly and applied when the matching execution arrives; commission-after-execution callbacks update that same row and cycle P/L.

Order status can expose cumulative filled quantity before individual execution IDs arrive. BouncyBot stores a stable residual cumulative placeholder for only the unrepresented quantity and commission. As real execution callbacks arrive, the placeholder shrinks and is deleted when the callback ledger fully represents the broker cumulative total. This prevents both lost fills and double counting.

After the first positive BUY fill:

1. the cycle remains in Stage 2;
2. cancellation of the unfilled remainder is requested once;
3. the original BUY continues to be polled until terminal;
4. additional fills received during the cancellation race update quantity, weighted average price, and commission;
5. Stage 3 begins only after terminal settlement, using the final cumulative app-owned BUY quantity.

If cancellation submission itself fails, the one-shot flag is cleared so a later poll can retry. A late BUY execution after an exit order already exists, or any SELL ledger above the app-owned BUY quantity, stops the cycle in `ERROR` for manual review.

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
- downward rounding to the route-specific IBKR market-rule increment, with contract `minTick` fallback only when no rule is advertised;
- `TIF=GTC`, `outsideRth=False`, app `OrderRef`, and optional explicit account.

### Market SELL

When final SELL trail is zero, Stage 3 submits a market SELL after the minimum-profit threshold is met.

The configured minimum profit is not a limit price. Both native-stop-triggered and explicit market SELLs can fill below the projected threshold.

### Optional pre-close cancel-and-liquidate path

When enabled, the controller supervises two related workflows at the contract-specific RTH cutoff.

**Stage 3:** the selected current price must be strictly above the average BUY fill price; commissions are ignored for that eligibility test. With no protective SELL, one RTH-only `DAY` market SELL is submitted for the app-owned unsold quantity. With a working protective SELL, BouncyBot cancels it once, waits for a terminal broker status, accounts for fills during cancellation, rechecks the price condition, and then submits only the remainder.

**Stage 4:** BouncyBot cancels the normal final SELL trail once, waits for a terminal broker status, accounts for full or partial fills during cancellation, and submits a `DAY`, `outsideRth=False` market SELL for only the remaining app-owned quantity.

For both stages:

- no replacement is submitted while another app-created SELL may still execute;
- cumulative protective, final-trail, and replacement executions determine completion and P/L;
- no outside-RTH fallback is sent;
- missing timing, cancellation uncertainty, rejection, incomplete close, or quantity conflict stops in `ERROR`;
- the Stage-3 price test does not guarantee a profitable market fill.

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

It may attach to a locally known exact `OrderRef`, import missing executions idempotently, complete a cycle, or require manual review. The cached broker probe is point-in-time: a newer normal terminal poll updates or removes its matching order row, while a later explicit probe that still reports the order remains visible. It does not recreate or cancel an order when exact ownership/state is uncertain.

## Manual intervention boundary

Manual cancellation or selling in TWS can be operationally necessary, but it can leave local state incomplete. Use Reconciliation to refresh and either resume or mark the cycle manually handled. Routine ATR/data/session/spread waits are not manual-intervention states and therefore do not enable recovery-changing buttons. Do not edit the database to imitate a broker event.
