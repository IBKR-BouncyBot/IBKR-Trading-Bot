# Risk controls and trading blockers

Risk controls are layered. Some are always part of safe order submission, some are enabled by default, and some are optional hard limits. The GUI’s **Trading** status summarizes current BUY/SELL eligibility and provides a tooltip with the complete blocker list.

These controls reduce specific risks; none guarantees safety or profitability.

## Control layers

### Controller and broker-state invariants

A new order is not submitted when required state is unknown or inconsistent. Examples:

- disconnected local API socket;
- unavailable or unconfirmed Gateway/TWS-to-IBKR server connection;
- unfinished post-reconnect broker reconciliation;
- unqualified contract;
- unresolved recovery-required state;
- invalid/missing selected price;
- app-created SELL cancellation still pending;
- unconfirmed order submission;
- unsold application-owned quantity before a new cycle.

These are not disabled by the optional hard-risk master.

### RTH and order settings

Strategy orders use `outsideRth=False`. New order placement requires the controller’s current RTH evaluation to permit it. Unknown or stale RTH status fails closed where the corresponding check applies.

The production adapter obtains date-specific regular-session ranges from the qualified contract's IBKR `liquidHours` and `timeZoneId`. Those parsed boundaries also drive the first-minutes, last-minutes, and pre-close BUY-cancellation controls, including early-close days. The adapter retains a weekday 09:30–16:00 New York fallback only when IBKR returns no usable contract hours; if no usable boundaries are available to the controller, new BUY entry fails closed and an active BUY is not cancelled at a guessed time.

Native orders accepted by IBKR can remain working according to broker rules. The application’s RTH guard controls its own submissions/activation decisions, not the broker’s entire account.

### Data-type and freshness controls

Defaults:

- block delayed/non-live data for live BUYs: on;
- stale-data guard: on;
- selected/API price maximum age: 3 seconds;
- bid/ask maximum age: 3 seconds;
- RTH-status maximum age: 60 seconds.

Freshness is based on actual `pendingTickersEvent` delivery, not on whether a cached `Ticker` still contains non-null bid, ask, or last fields. Each live subscription has an identity and each actual callback has a sequence/timestamp. The controller consumes a sequence once; rereading it does not reset quote age, advance a waiting stage, or add ATR/volatility data. If event tracking is unavailable, the production adapter fails closed rather than treating cached fields as fresh.

A quote can legitimately remain numerically unchanged while fresh events continue. The GUI therefore shows both actual-update age/count and value-change age/count. After an upstream outage or reconnect, cached fields remain invalid until a new event arrives.

### IBKR what-if preflight

Enabled by default. Before a BUY, the adapter asks IBKR to evaluate the order without transmitting it. A rejection or failed/uncertain response blocks the actual BUY.

What-if success is not an execution guarantee. Buying power, prices, account state, and broker controls can change between preflight and submission.

### ATR warmup

Enabled by default when ATR adaptive mode is on. No initial-drop trigger is armed until enough RTH-only observed bars exist. The ready update resets the Stage-1 anchor; the drop must occur afterward.

RTH observation and bar collection is independent of the adaptation switch. Turning adaptation off prevents calculated ATR values from changing strategy percentages, but the current-session in-memory RTH buffer continues warming. Collection pauses outside RTH and resets when the application restarts.

This prevents a manual fallback drop from triggering before the adaptive entry percentage is available.

### Session-timing guard

Enabled by default:

- no new BUY during the first 5 minutes of the regular session;
- no new BUY during the last 15 minutes;
- request cancellation of an unfilled app BUY trail 5 minutes before close.

Each minute value can be set to zero to disable that sub-control while leaving the master on.

### Recent-volatility filter

Off by default. When enabled, the controller examines the range of recent application-observed usable prices over the configured window (default 300 seconds). A range above the configured maximum (default 5%) blocks a new BUY.

This is not a historical-volatility model and does not predict future movement.

## Optional hard risk limits

The master is off by default. A numeric zero disables the corresponding limit.

### Loss limits

- maximum completed application net loss for the selected ticker during the current stored date scope;
- maximum completed application net loss across stored tickers during that scope.

The values come from local completed cycles, not real-time account P/L. Open losses, unrelated trades, FX, financing, and broker adjustments are outside the calculation.

### Completed-cycle cap

The persisted field name is `max_cycles_per_ticker_day`, but current runtime behavior treats it as a total completed-cycle cap for the selected ticker, not a per-day count. Zero disables it. When the cap is reached after a completed SELL, auto-repeat stops without creating another cycle.

### Consecutive-loss cap

Counts consecutive completed application cycles with negative net P/L. Zero disables it.

### Spread limit

Uses current bid and ask to calculate:

```text
spread % = (ask - bid) / midpoint × 100
```

The calculated spread is compared with the fixed **Maximum spread %** saved by the user. Live bid/ask data never changes that configured value. It can change only through explicit user input or loading persisted settings. Missing/stale bid/ask can also block through the data guard. Zero disables the configured percentage limit, not freshness requirements.

### Previous-close gap

Uses the absolute difference between the selected market price and previous close. Zero disables it. A missing previous close means the gap limit cannot be evaluated safely when enabled.

### Minimum trade price

Blocks a BUY below the configured selected-price floor. Zero disables it.

## Position and order ownership controls

### Application-owned long quantity

A new app cycle is blocked when local persisted fills show unsold shares created by the application, unless the cycle was marked manually handled.

An account-wide external long position does not block a new app BUY. This prevents unrelated manual holdings from stopping the strategy, but it does not create broker-side lot separation.

### Order prefix

Cancel and recovery operations filter `OrderRef` by `IBKRBOT|`. This reduces the risk of touching manual or third-party orders. Reusing the prefix manually defeats the boundary.

### One app SELL at a time

Before replacing a protective/final SELL or performing a market close, the controller waits until the prior app-created SELL is confirmed nonworking. This reduces overselling risk.

## BUY versus SELL policy

Most configurable hard limits are entry controls. They do not intentionally trap an existing app-owned position by blocking risk-reducing SELL actions.

SELL submission still requires coherent state, a live local socket, confirmed upstream IBKR connectivity, completed post-reconnect reconciliation, valid quantity/contract, appropriate RTH/order conditions, and safe cancellation sequencing. A missing fresh quote does not by itself prevent every risk-reducing exit path, because a protective or market-close order may be based on known fills rather than a new strategy-price trigger. A protective or final native order can still be rejected or fill poorly.

## Trading-status presentation

The controller builds a complete blocker list for the GUI. The compact label displays the first blocker and count of additional blockers. The tooltip contains the explanations.

Expected configured pauses are caution/yellow states. An upstream outage is shown as a connectivity failure even when the local Gateway socket remains open. Red is reserved for broker/local inconsistency, failed recovery confidence, or a condition that requires intervention rather than a normal configured wait. A guard pause or ordinary strategy wait does not enable **Reconcile and resume**, **Stop after current cycle**, **Cancel visible app-owned orders**, **Sell app-bought unsold position**, **Leave orders working**, or **Mark manually handled**; read-only **Refresh from IBKR/TWS** and audit export remain available.

A green/ready status means no evaluated blocker is currently active. It does not guarantee that IBKR will accept or fill the next order.

## Stop and recovery controls

Risk management includes the operator’s stop choice. “Stop” must be interpreted literally:

- cancel app orders;
- market-close local app quantity after safe cancellation;
- leave orders working;
- stop after completion;
- stop locally without broker action.

Stop, exit, and Reconciliation derive market-close quantity from the persisted application-owned fill ledger rather than account-wide holdings. Recovery never assumes that a missing local callback means an order did not execute. It compares open app orders and recent executions, supersedes an older point-in-time probe with a newer terminal poll for the same app order, and requires manual review when facts remain ambiguous.
