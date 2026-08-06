# v3.7.0 field-level market-data freshness and Stage-3 SELL guard

**Release:** v3.7.0

**Previous release:** v3.6.0

## Purpose

v3.7.0 corrects the market-data failure reconstructed from CHIP cycle 3. A fresh `pendingTickersEvent` had been allowed to make the complete cached ticker snapshot look current even though only the quote changed and the `Last` field remained fixed at 129.44. When the bid disappeared, `Ticker.marketPrice()` fell back to that unchanged Last. The stale value crossed the Stage-3 rise trigger, armed a native trailing SELL at 129.12, and the later broker execution occurred near the actual market around 127.84.

The release keeps the existing controller, broker adapter, strategy engine, order types, database, and GUI architecture. It narrows the correction to field-level market-data identity, the normal Stage-3 final-SELL gate, pre-submission revalidation, ATR input eligibility, and focused regressions.

## Implemented safeguards

### 1. Independent bid, ask, and Last identity

The live adapter now records, per raw price field:

- latest update sequence and callback time;
- latest numerical-change sequence and callback time;
- whether the field updated in the current ticker event;
- whether the numerical value changed in the current ticker event.

A same-price trade or quote price tick is therefore still a real field update. Size-only and timestamp-only events do not refresh the corresponding price field.

### 2. Selected-price basis tracking

Every snapshot identifies the raw field or fields behind the selected convenience price. In particular, `marketPrice` is classified as Last-derived, bid/ask-derived, mark-derived, or close-derived. A callback caused by another field no longer makes an unchanged Last-derived price strategy-usable.

### 3. Fresh complete quote required in Stage 3

The normal final SELL may be armed only from a complete, non-crossed, independently fresh bid/ask pair. A missing bid, missing ask, crossed quote, expired side, unknown quote identity, or unavailable field-level tracking keeps the cycle in `WAIT_RISE_TRIGGER`.

### 4. Maximum spread applies to the final SELL

The configured `max_spread_pct` is now enforced when the normal Stage-3 final SELL is evaluated, even when the optional hard BUY-risk group is disabled. A wide quote cannot be used merely because a Last value happens to lie inside the spread.

### 5. Executable bid confirms the trigger

The current SELL-side bid must reach the recalculated rise trigger, allowing only the selected contract's minimum-tick tolerance. Last, midpoint, mark, close, and `marketPrice` cannot independently arm the final SELL.

### 6. Two consecutive quote observations

One qualifying quote starts a confirmation. A second distinct quote update for the same cycle and market-data subscription must also pass every guard before Stage 4 can be entered. Any intervening non-quote, incomplete, stale, crossed, over-wide, or below-trigger observation clears the pending confirmation.

### 7. Revalidation before intent and broker submission

The confirmed quote identity, subscription, sequence, bid, ask, ages, spread, and trigger are rechecked:

1. immediately before the durable order intent is written; and
2. immediately before the broker adapter is called.

If either check fails, the unsubmitted SELL transition is rolled back to Stage 3, any recorded intent is marked failed, no broker order is sent, and `SELL_MARKET_DATA_REVALIDATION_BLOCKED` is written to the audit trail.

### 8. ATR excludes stale fallback Last values

ATR and price-history observations are appended only when the raw field underlying the selected strategy price updated in that event. An unchanged cached Last exposed by a bid/ask or size event is still shown diagnostically, but it cannot enter ATR or advance Stage 1/Stage 3.

### 9. Incident-shaped regression coverage

The focused regression layer reproduces both critical CHIP states:

- bid 127.82, ask widening to 130.42, unchanged Last 129.44;
- missing bid, ask near 128.04, unchanged Last 129.44.

It also verifies same-value price ticks, size/timestamp-only ticks, post-reconnect field invalidation, independent bid/ask ages, two consecutive quote confirmations, bid-side trigger confirmation, spread enforcement with hard BUY-risk limits disabled, worker-cycle handling of a stale selected Last plus a valid current bid, and both pre-submission revalidation points.

## Close-before-RTH behavior

The optional profitable Stage-3 close-before-RTH path now uses the same fresh, complete, spread-checked quote boundary before it may use the current bid for its profitability comparison. The quote is revalidated after any protective-order cancellation and again immediately before the market-order broker call. If native protection has already become terminal and the quote can no longer be proved safe, the cycle fails closed for manual review; otherwise the unsubmitted close request is rolled back to Stage 3. An invalid quote cannot cause a market liquidation from an old selected price.

## Compatibility

v3.7.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.6.0 databases, active cycles, order references, execution rows, checkpoints, captures, backups, and watchdog state remain compatible.

The correction does not change native IBKR trailing-stop semantics. A submitted trailing stop still triggers and executes according to IBKR and market liquidity; the quote guard prevents the app from arming it from unverified stale data but cannot guarantee the eventual fill price.
