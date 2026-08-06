# Risk controls and trading blockers

Risk controls are layered. Some are always part of safe order submission, some are enabled by default, and some are optional hard limits. The GUI’s **Trading** status summarizes current BUY/SELL eligibility and provides a tooltip with the complete blocker list.

These controls reduce specific risks; none guarantees safety or profitability.

## Optional Stage-3/Stage-4 liquidation before RTH close

**Cancel SELL trail and liquidate before close** is off by default. The configured minute value uses the contract's date-specific regular-session close and may therefore start earlier on an early-close day.

In Stage 3, the policy is conditional: a complete, independently fresh, non-crossed bid/ask pair must be within the configured Maximum spread, and the executable bid must be strictly above the average BUY fill price at the cutoff, with commissions intentionally ignored. If a protective SELL is working, the safety invariant is cancel-confirm-replace; its fills reduce the remaining quantity, and the quote/bid condition is checked again before replacement. With no protective SELL, the app can submit the market SELL directly.

In Stage 4, the established invariant remains cancel-confirm-replace: no replacement market SELL is transmitted while the original final SELL trail may still execute. Any fills reported during cancellation reduce the remaining quantity.

The replacement is RTH-only, `DAY`, and market-priced. The Stage-3 quote comparison is not a price guarantee: the fill can be below the checked quote, below the average BUY price, and can realize a loss.

Failure behavior is conservative:

- unknown session boundary: do not start the automatic workflow;
- Stage-3 executable bid not strictly above average BUY: do not sell at that observation; a later fresh profitable quote before close may still qualify;
- cancellation not terminal by close: do not submit a second SELL;
- cancellation terminal after RTH: do not submit outside RTH;
- Stage-3 quote becomes invalid or its executable bid falls to/below average BUY after a protective cancellation: stop in `ERROR`;
- replacement rejected, failed, or incomplete at close: stop in `ERROR` for manual review;
- conflicting manual market-close request while the workflow is active: refuse the second request.

This policy does not guarantee protection against gaps, broker/exchange halts, connection outages, or execution slippage.

## Control layers

### Controller and broker-state invariants

A new order is not submitted when required state is unknown or inconsistent. Examples:

- disconnected local API socket;
- unavailable or unconfirmed Gateway/TWS-to-IBKR server connection;
- unfinished post-reconnect broker reconciliation;
- missing, unqualified, identity-mismatched, or capability-incompatible exact contract;
- unresolved recovery-required state;
- invalid/missing selected price;
- app-created SELL cancellation still pending;
- unconfirmed order submission;
- unsold application-owned quantity before a new cycle.

These are not disabled by the optional hard-risk master.

### RTH and order settings

Strategy orders use `outsideRth=False`. New order placement requires the controller’s current RTH evaluation to permit it. Unknown or stale RTH status fails closed where the corresponding check applies.

The production adapter obtains date-specific regular-session ranges from the exact qualified contract's IBKR `liquidHours` and `timeZoneId`. For `LSE` and `LSEETF`, the effective range is the intersection of that broker window and the verified 08:00-16:30 `Europe/London` continuous session; this prevents timing-sensitive market orders from being scheduled in a later auction/post-continuous phase. An IBKR early close remains earlier and therefore still wins. The effective boundaries drive the first-minutes, last-minutes, pre-close BUY-cancellation, Stage-3/Stage-4 liquidation, and RTH-only ATR controls. The weekday 09:30-16:00 New York fallback is permitted only for a recognized U.S. primary exchange. A non-U.S. or unknown contract with missing or unusable session metadata fails closed; BouncyBot does not guess U.S. hours for an EUR listing.

A configured BUY blocker is not a broker submission failure. Before an order intent exists, BouncyBot records `PreflightBlocked` and returns the cycle to Stage 1. The guard remains active on every evaluation, but v3.9.0 coalesces repeated audit rows per cycle while retaining stable blocker reason codes. Expected waits use INFO entry/15-minute summaries; hard risk, data, or broker blockers use an immediate WARN, a one-minute first WARN summary, and later WARN summaries at five-minute intervals. Recovery is recorded once when the blocker clears.

Native orders accepted by IBKR can remain working according to broker rules. The application’s RTH guard controls its own submissions/activation decisions, not the broker’s entire account.

### Working BUY remainder after a partial fill

A positive partial fill proves that the triggered BUY is marketable and has already created an app-owned position. BouncyBot therefore does not cancel merely because the first broker update is partial. It allows the original order a fixed 3.0-second grace period to finish an ordinary multi-print execution, while remaining in Stage 2 and reconciling every execution and commission.

The still-working remainder is cancelled once after the timeout, or immediately when an enabled safety fact becomes unsafe: RTH closes; required live/fresh data is lost; session timing becomes unavailable or enters the configured cancellation window; the volatility ceiling is exceeded; the configured minimum price or previous-close gap is breached/unverifiable; or the configured spread becomes missing, crossed, or excessive. Portfolio P/L limits, cycle counts, and what-if are not rerun after a fill because they govern new submission rather than ownership of shares already bought.

Cancellation is not atomic with exchange execution. More shares, including the full requested quantity, can fill before IBKR confirms the request. Stage 3 begins only after the original BUY is terminal and uses the final reconciled app-owned quantity.

### Data-type and freshness controls

Defaults:

- block delayed/non-live data for live BUYs: on;
- stale-data guard: on;
- selected/API price maximum age: 3 seconds;
- bid/ask maximum age: 3 seconds;
- RTH-status maximum age: 60 seconds.

Freshness is based on actual `pendingTickersEvent` delivery, not on whether a cached `Ticker` still contains non-null bid, ask, or Last fields. Each live subscription has an identity and each actual callback has a sequence/timestamp. The live adapter additionally records update and numerical-change identity for each price field. A bid-size, ask-size, last-size, or timestamp event therefore cannot refresh an unchanged cached price; a same-value raw price tick can refresh that specific field.

The controller consumes a whole-event sequence once. ATR and generic Stage-1/Stage-3 selected-price handling additionally require the raw selected-price basis to have updated in that event. The normal Stage-3 final SELL requires both bid and ask to be independently fresh, a valid spread, the bid at the trigger, and two distinct qualifying quote updates. Rereading cached fields does not reset age or create another confirmation. After an upstream outage or reconnect, field timestamps remain invalid until the corresponding fields update again. If field tracking is unavailable on the production path, the guarded Stage-3 exit fails closed.

A quote can legitimately remain numerically unchanged while fresh same-value price ticks continue. The GUI therefore shows both actual-update age/count and value-change age/count.

### IBKR what-if preflight

Enabled by default. Before a BUY, the adapter uses IBKR's dedicated what-if request with `whatIf=True` and `transmit=True`. The what-if flag prevents a normal live transmission while satisfying the API validation requirement.

The check fails closed when IBKR returns no order state, a validation/rejection terminal status, rejection text, only unset floating-point sentinels, or no usable margin/equity impact. Numeric zero changes are valid and remain visible. A failed or uncertain response blocks the actual BUY.

What-if success is not an execution guarantee. Buying power, prices, account state, market-rule data, and broker controls can change between preflight and submission.


### Market-rule price validation

IBKR contract `minTick` can be the smallest increment seen anywhere for a contract rather than the valid increment for the selected route and current price. When a contract advertises `marketRuleIds`, BouncyBot maps the selected exchange to its rule, requests the rule's price bands, and applies the increment for the proposed order price. BUY prices round upward and SELL prices round downward. If an advertised rule cannot be resolved, the order is blocked before transmission.

### Exact contract, database currency, and quantity validation

The live adapter accepts only an exact IBKR API selection with a positive `conId`, ordinary `STK` security type, `SMART` routing, and contract currency `USD` or `EUR`. It verifies that qualification returns the same conId and currency, that SMART is an advertised route when exchange metadata is supplied, and that the contract advertises the `MKT` and `TRAIL` order types required by the strategy. A mismatch blocks Start, recovery, or order creation rather than falling back to symbol-only qualification.

Each portable SQLite database uses one contract currency. A draft database can be rebound between USD and EUR while it has no cycles. The first persisted cycle locks the database currency; an existing v3.1.2 database is inferred from its historical cycles. Mixed-currency evidence fails closed because BouncyBot does not convert P/L, risk limits, reinvestment, or commissions through FX.

Whole-share quantities are validated against IBKR `minSize` and `sizeIncrement` metadata. BUY quantities may be rounded down to the largest valid whole-share quantity within budget. A SELL quantity is never silently rounded down, because doing so could leave an untracked app-owned remainder. Missing IBKR size metadata uses the conservative one-share default.

### Connection-loss retry

After an established local API connection is lost, BouncyBot pauses broker-dependent work and retries the configured TWS or Gateway endpoint every 10 seconds without a retry limit. Manual **Disconnect** and application shutdown disable the retry loop. Reconnection alone does not authorize trading: upstream IBKR connectivity, exact-contract qualification, broker reconciliation, and a new actual market-data event must all recover before strategy processing resumes.

### Rejection circuit breaker

An unfilled BUY that becomes `Inactive` or `Rejected`, or that carries a substantive terminal broker rejection, stops the cycle in `ERROR` for manual review. This prevents a structural validation failure such as an invalid price from generating repeated fresh entry attempts. Ordinary confirmed cancellations remain recoverable and reset Stage 2 to Stage 1; IBKR code 202 alone is not treated as a rejection.

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

The values come from local completed cycles in the database's single contract currency, not real-time account P/L. Open losses, unrelated trades, FX, financing, and broker adjustments are outside the calculation. A commission reported in another currency is retained for audit but excluded from local net P/L, and Auto-repeat is disabled because no FX conversion is performed.

### Completed-cycle cap

The persisted field name is `max_cycles_per_ticker_day`, but current runtime behavior treats it as a total completed-cycle cap for the selected ticker, not a per-day count. Zero disables it. When the cap is reached after a completed SELL, auto-repeat stops without creating another cycle.

### Consecutive-loss cap

Counts consecutive completed application cycles with negative net P/L. Zero disables it.

### Spread limit

Uses current bid and ask to calculate:

```text
spread % = (ask - bid) / midpoint × 100
```

The calculated spread is compared with the fixed **Maximum spread %** saved by the user. Live bid/ask data never changes that configured value. It can change only through explicit user input or loading persisted settings. Missing/stale bid/ask can also block through the data guard. The percentage ceiling applies to BUY preflight and the normal Stage-3 final-SELL/Stage-3 close-before-RTH gate, even when the optional hard-risk master is off. Zero disables only the configured percentage ceiling, not quote completeness or per-side freshness requirements.

### Previous-close gap

Uses the absolute difference between the selected market price and previous close. Zero disables it. A missing previous close means the gap limit cannot be evaluated safely when enabled.

### Minimum trade price

Blocks a BUY below the configured selected-price floor. Zero disables it.

## Position and order ownership controls

### Application-owned long quantity

A new app cycle is blocked when local persisted fills show unsold shares created by the application, unless the cycle was marked manually handled.

An account-wide external long position does not block a new app BUY. This prevents unrelated manual holdings from stopping the strategy, but it does not create broker-side lot separation.

### Exact order-reference ownership

The `IBKRBOT|` prefix identifies the application family but is not sufficient ownership proof when multiple installations share one account or Master API feed. Cancel, recovery, callback, and error attribution require the complete `OrderRef` to exactly match a value already persisted by this installation. Unmatched prefixed orders remain unowned diagnostics and cannot change the active cycle. Losing the local database can therefore require manual recovery rather than broad prefix-based cancellation.

### One app SELL at a time

Before replacing a protective/final SELL or performing a market close, the controller waits until the prior app-created SELL is confirmed nonworking. This reduces overselling risk.

## BUY versus SELL policy

Most configurable hard limits are entry controls. They do not intentionally trap an existing app-owned position by blocking risk-reducing SELL actions.

SELL submission still requires coherent state, a live local socket, confirmed upstream IBKR connectivity, completed post-reconnect reconciliation, valid quantity/contract, appropriate RTH/order conditions, and safe cancellation sequencing. The normal Stage-3 profit exit additionally requires the field-level bid/ask confirmation and two pre-submission revalidations described above. A missing fresh quote does not by itself prevent every risk-reducing exit path, because a protective or market-close order may be based on known fills rather than a new strategy-price trigger. A protective or final native order can still be rejected or fill poorly.

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
