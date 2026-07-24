# Strategy rules

This document is the current functional description of the five-stage strategy in v3.2.0. It describes application decisions; IBKR remains authoritative for accepted order state and execution.

## Scope and invariants

- One active cycle is monitored at a time.
- The cycle is long-only and uses whole shares.
- The selected contract is an ordinary `STK`, denominated in USD or EUR, routed through `SMART`, and identified by an exact positive IBKR `conId` plus its returned primary exchange.
- New strategy orders are submitted only when the controller’s local socket, upstream IBKR connectivity, post-reconnect reconciliation, exact-contract identity/capability checks, database currency boundary, RTH, actual-event market-data, guard, and recovery checks allow them.
- Orders use `outsideRth=False` and app-owned `OrderRef` values.
- A native trailing stop triggers a market-style order; displayed stops and profit levels are not guaranteed fills.

## Stage 1 — `WAIT_INITIAL_DROP`

### Anchor behavior

The first usable strategy price initializes `anchor_price`. “Usable” means a newly consumed ticker event with confirmed upstream connectivity; rereading cached non-null fields cannot initialize or move the anchor. Before the drop condition occurs:

```text
if last price > anchor:
    anchor = last price
```

Manual mode sets:

```text
drop trigger = anchor × (1 - initial_drop_pct / 100)
```

A price at or below the trigger prepares the entry action.

### ATR warmup behavior

When all three conditions are true:

- ATR adaptive mode is enabled;
- the “block new BUY until ATR has enough RTH data” option is enabled;
- ATR is not ready;

Stage 1 has no `drop_trigger_price`. Every usable warmup price becomes only the current reference. The manual initial-drop percentage is ignored for entry.

When ATR becomes ready, the ready price establishes a fresh anchor and ATR-derived trigger. That same price update cannot trigger entry. A later update must make the required drop from the new anchor.

If the warmup block is disabled, the current configured/manual percentages can drive Stage 1 before ATR readiness.

## Stage 2 — `BUY_TRAIL_ACTIVE`

At the drop condition:

```text
projected BUY stop = current price × (1 + buy_rebound_trail_pct / 100)
```

The controller raises the broker stop as needed so it remains valid relative to visible ask/last fields. It then normalizes the stop upward to the increment for the selected IBKR route and proposed price. When the contract advertises a market rule, its price bands are authoritative; contract `minTick` is only the fallback when no rule is advertised. An unresolved advertised rule blocks the order.

### Quantity

Without the optional slippage assumption:

```text
quantity = floor(budget / projected BUY stop)
```

With the assumption:

```text
sizing price = projected BUY stop × (1 + slippage_buffer_pct / 100)
quantity = floor(budget / sizing price)
```

A quantity below one blocks order submission. Before intent is stored, the live adapter applies the selected contract's whole-share minimum size and size increment. A BUY may be rounded down to a valid whole-share step. A SELL is never rounded down because doing so could leave an application-owned remainder untracked.

### Order type

- `buy_rebound_trail_pct > 0`: submit a native BUY `TRAIL` order.
- `buy_rebound_trail_pct == 0`: submit a market BUY immediately after the initial-drop condition.

After the first positive BUY fill, the controller requests cancellation of any unfilled remainder once, but the cycle remains in Stage 2 until the original BUY order is terminal. While cancellation is pending, every later cumulative fill and execution callback is reconciled. Stage 3 begins only after IBKR reports `Filled`, `Cancelled`, `ApiCancelled`, `Inactive`, or `Rejected`, using the final app-owned quantity, weighted average BUY price, and all commissions received so far. A substantive rejection still activates the rejection circuit breaker.

An unfilled BUY that becomes `Inactive` or `Rejected`, or reaches a terminal state with a substantive broker rejection, stops the cycle in `ERROR` for manual review. It is not automatically retried. An ordinary confirmed cancellation without a substantive rejection still resets Stage 2 to Stage 1.

## Stage 3 — `WAIT_RISE_TRIGGER`

The average BUY fill is the profit reference. The cycle anchor is not used to measure profit.

### Minimum initial SELL stop

Without slippage planning:

```text
minimum stop = avg_buy_price × (1 + minimum_profit_pct / 100)
```

The user-facing minimum profit is clamped to at least the small positive guard epsilon used by the model.

With slippage planning:

```text
minimum stop =
    avg_buy_price × (1 + minimum_profit_pct / 100)
    / (1 - slippage_buffer_pct / 100)
```

The buffer estimates a worse market fill after the stop; it remains a planning assumption.

### Required selected price

For a positive final SELL trail:

```text
required selected price = minimum stop / (1 - sell_trailing_stop_pct / 100)
```

For a zero final SELL trail, the minimum stop itself is the market-exit threshold.

The controller does not submit the final exit until the selected price reaches the required level and the current SELL checks allow submission.

## Stage 4 — `SELL_TRAIL_ACTIVE`

### Order type

- `sell_trailing_stop_pct > 0`: submit a native SELL `TRAIL` order for the unsold app-owned quantity.
- `sell_trailing_stop_pct == 0`: submit a market SELL when the Stage-3 minimum-profit condition is met.

The initial native SELL stop is calculated below the current visible SELL reference, rounded down to the applicable route-specific IBKR market-rule increment (or contract minimum tick when no rule is advertised), and checked against the required minimum-profit stop.

After submission, IBKR controls trailing behavior. The application polls and records status/fills.

### Optional close-before-RTH liquidation

The policy is disabled by default and uses the contract's confirmed, date-specific RTH close. It applies to positions in both Stage 3 and Stage 4.

In Stage 3, at the configured cutoff:

1. the selected current price must be strictly greater than the weighted average BUY fill price;
2. commissions are deliberately ignored for this eligibility comparison;
3. if no protective SELL is working, one RTH-only `DAY` market SELL is submitted for the app-owned unsold quantity;
4. if a protective SELL is working, BouncyBot requests cancellation once, waits for a terminal broker status, includes any fills received during that race, rechecks the selected price, and submits only the remaining quantity;
5. if the selected price is no longer strictly above the average BUY price after cancellation, the cycle enters `ERROR` for manual review rather than transmitting the replacement.

In Stage 4, the established cancel-confirm-replace workflow remains:

1. request cancellation of the normal final native SELL trail once;
2. continue polling the original order and accept full or partial fills during the cancellation race;
3. wait for a terminal broker status;
4. calculate remaining app-owned shares from persisted SELL executions;
5. submit one `DAY`, `outsideRth=False` market SELL for only that remainder;
6. complete Stage 5 only after cumulative app-owned SELL fills equal cumulative app-owned BUY fills.

The Stage-3 quote test is not a fill-price guarantee. A market order can execute below the checked quote, below the average BUY price, or at a loss. No outside-RTH fallback is submitted. Unknown session timing, unresolved cancellation, rejected or incomplete replacement orders, late BUY fills after an exit was created, and quantity conflicts fail closed into manual review.

## Stage 5 — `CYCLE_COMPLETE`

A cycle completes when the application-owned BUY quantity has been sold according to recorded fills. The controller records:

- order and execution identities;
- average BUY/SELL prices and quantities;
- commissions received from IBKR when they are in the cycle/database currency; a non-zero commission in another currency remains a raw broker fact, is excluded from local net P/L, and disables Auto-repeat because BouncyBot does not perform FX conversion;
- gross and net P/L;
- stage timestamps and audit events.

If auto-repeat is enabled, stop-after-current-cycle is false, and the enabled maximum completed-cycle cap has not been reached, a new Stage-1 cycle is created with the current strategy settings. Reaching the cap leaves the completed cycle terminal and stops repetition.

## Optional protective SELL

When enabled, a positive BUY fill initiates a protective native SELL `TRAIL` for the app-owned quantity. It can fill before the minimum-profit condition, producing a loss-limiting exit rather than a profit exit.

When Stage 3 becomes eligible for the normal final SELL:

1. request cancellation of the protective order;
2. wait until IBKR reports it no longer working;
3. account for any protective fills;
4. submit the final SELL only for remaining app-owned quantity.

The controller does not intentionally leave both protective and final app-created SELL orders working for the same shares.

## ATR calculation and application

### Data source

ATR uses selected prices from actual ticker-update events consumed by the controller while RTH is open. Repeated cached reads are excluded. Samples are grouped by callback arrival time into fixed-duration OHLC bars. Observation and diagnostic-bar collection continues when ATR adaptation is disabled; the adaptation switch controls whether ready values are applied, not whether the current-session RTH buffer is maintained. The buffer is in memory and resets on process restart. True range uses the current high/low and previous completed close. ATR% is ATR divided by the latest reference price.

It is not based on a requested IBKR historical-bar series.

### Readiness

A period of `N` requires at least `N + 1` observed bar buckets to calculate `N` true ranges. The newest bucket can still be forming. With defaults, readiness therefore needs observations spanning 15 distinct 60-second buckets.

### Derived values

For each enabled field:

```text
adaptive percentage = clamp(ATR% × multiplier, minimum %, maximum %)
```

Entry drop, BUY trail, and final SELL trail are adapted. Minimum profit is adapted only when its toggle is on. Protective SELL is adapted only when its own toggle is on.

When adaptation is enabled and ready, the controller rewrites the same percentage fields used by the pure strategy; there is not a separate ATR trading engine. When adaptation is disabled, it can still report ATR readiness/bars but does not rewrite those fields.

## Budget and reinvestment

Base cycle budget is the configured investment amount. When reinvestment is enabled:

```text
budget = base investment + max(completed app net P/L for ticker, 0)
```

Stored completed losses do not reduce the configured base. This is local application P/L in the portable database's single contract currency, not available cash, buying power, account-wide P/L, or an FX-converted total. IBKR preflight remains authoritative for order acceptance.

## External positions

The strategy does not use the account-wide IBKR position to block a new entry. It checks whether persisted application BUY fills remain unsold after application SELL fills. A cycle explicitly marked manually handled is excluded from that blocker.

This is local accounting; the broker does not segregate shares by source.

## Mid-cycle setting changes

Draft settings are persisted as the operator edits them. The controller applies only fields considered safe for the current stage. Order-driving values already committed to a submitted native order cannot be changed merely by editing the GUI; use the stop/recovery workflow when broker action must change.

## Stop behavior

Stop is not a single state transition. The operator chooses whether to cancel app orders, market-close app-owned quantity, leave orders working, stop after the cycle, or stop locally without broker action. See [`OPERATIONS.md`](OPERATIONS.md) and [`ORDER_FLOW.md`](ORDER_FLOW.md).
