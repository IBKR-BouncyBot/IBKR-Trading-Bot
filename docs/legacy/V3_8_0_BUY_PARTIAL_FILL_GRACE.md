# v3.8.0 BUY partial-fill grace and safety cancellation

**Release:** v3.8.0

**Previous release:** v3.7.0

## Purpose

v3.8.0 changes the Stage-2 policy for a triggered marketable BUY that reports a positive partial fill. Earlier releases requested cancellation immediately after the first partial execution. For small marketable orders, that status is commonly only an intermediate multi-print state, so the cancellation request usually raced the remaining executions and could sometimes leave the cycle unnecessarily under-invested.

The new policy lets the original BUY finish normally for a short fixed grace period. BouncyBot still remains fail-closed: it cancels the unfilled remainder once the grace expires or immediately when a configured market/session safety condition is no longer satisfied. The original order remains under supervision until IBKR reports a terminal state, and all fills received before or during cancellation continue to be reconciled.

## Implemented behavior

### 1. Three-second grace from the first positive fill

The first positive BUY fill starts a fixed **3.0-second** grace period. The policy applies to both:

- a native BUY `TRAIL` order after it has triggered and become marketable; and
- a zero-trail BUY submitted as `MKT`.

During the grace period, a nonterminal partial fill remains in `BUY_TRAIL_ACTIVE`. No cancellation is sent merely because the broker has reported fewer shares than the requested quantity.

### 2. Normal multi-print completion is preferred

If the original BUY reaches a terminal state during the grace period, BouncyBot settles it normally without submitting an extra cancellation. A complete fill therefore becomes Stage 3 with the complete quantity. A broker-terminal partial, such as a cancellation initiated by the exchange or an existing session guard, becomes Stage 3 with the final app-owned filled quantity.

### 3. Timeout cancellation

If the order is still nonterminal after 3.0 seconds, BouncyBot requests cancellation of the working remainder once. The timeout is measured from the first positive fill rather than from the most recent partial execution, so a slow trickle of fills cannot keep the entry order working indefinitely.

The cancellation remains a race with the broker and market. Additional or complete fills can still arrive after the request. Those executions are recorded normally, and Stage 3 starts only after the original BUY reaches a terminal broker status.

### 4. Immediate cancellation when safety changes

The grace period is bypassed when a configured market/session condition becomes unsafe while the remainder is still working. The controller rechecks:

- confirmed regular trading hours for an RTH-only cycle;
- live-market-data mode when delayed data is blocked in a live profile;
- the configured stale-data guard, including invalidated/missing price, bid/ask, and RTH freshness;
- the configured pre-close BUY-cancellation window;
- the configured recent-volatility filter;
- the configured minimum trade price and maximum gap from the previous close; and
- the configured maximum bid/ask spread, including missing or crossed quotes.

Portfolio budgets, daily-loss limits, cycle counts, and what-if checks are intentionally not rerun after a fill. Those controls decide whether a new BUY may be submitted; they do not retroactively redefine ownership of shares already bought.

### 5. Persistent restart-safe timing

The existing `buy_filled_at` timestamp is used as the grace-period origin. It already survives reconnects, watchdog replacement, and application restart. A recovered order whose first partial fill is older than the grace period is therefore cancelled promptly after broker reconciliation if it is still working.

A legacy or malformed cycle with no usable first-fill timestamp starts a new grace period instead of cancelling from an unknown age. A timestamp in the future, such as after a wall-clock rollback, is likewise reset to the current observation so the remainder cannot stay active indefinitely.

### 6. One-shot cancellation and retry behavior

`buy_remainder_cancel_requested` remains the persisted one-shot flag. After a confirmed cancellation request, repeated polls do not send duplicates. If the broker call fails, the flag remains clear so a later cadence can retry. A pre-close cancellation and the partial-fill timeout share the same duplicate-prevention state.

### 7. Audit evidence

The audit trail now records:

- `BUY_PARTIAL_FILL` with grace elapsed/remaining time and the current filled/remainder quantities; and
- `BUY_REMAINDER_CANCEL_REQUESTED` with `timeout` or `safety` as the decision result and a structured reason code.

The policy does not change execution-id idempotence, cumulative placeholder reconciliation, commission handling, exact OrderRef ownership, or late-fill safeguards.

## Simulation and regression coverage

The deterministic simulation runner models a partial BUY as remaining active through the grace period and then receiving a successful timeout cancellation. Focused controller tests verify:

- no cancellation on the first partial fill;
- the same grace policy for native trailing and market BUYs;
- full multi-print completion inside the grace period;
- one cancellation after timeout;
- immediate cancellation for RTH closure, live-data downgrade, session cutoff/unavailability, stale data, volatility, minimum-price/gap limits, and unavailable or excessive spread;
- retry after an unavailable cancellation call;
- no duplicate request while cancellation is pending;
- terminal partial settlement without an unnecessary cancellation; and
- continued reconciliation of the historical NBIS multi-print fixture and of a full fill that arrives after cancellation was requested.

## Compatibility

v3.8.0 adds no SQLite table, column, index, migration, or persisted setting. Existing v3.7.0 databases, active cycles, order references, execution rows, checkpoints, captures, backups, and watchdog state remain compatible.

The grace duration is intentionally fixed rather than added as a new GUI/database setting in this release. The change does not alter IBKR market-order semantics: a cancellation request cannot undo filled shares and cannot guarantee that the still-working remainder will not fill before the broker acknowledges cancellation.
