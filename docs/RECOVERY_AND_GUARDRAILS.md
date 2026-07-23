# Recovery invariants and guardrails

This document is the technical companion to [`RECOVERY_AND_FAILSAFE.md`](RECOVERY_AND_FAILSAFE.md). It lists the invariants the controller attempts to preserve.

## Ownership invariants

- Application orders have an `OrderRef` beginning with `IBKRBOT|`.
- Cancel/recovery actions do not intentionally target an order without that prefix.
- An external/manual account position does not become application-owned merely because its ticker matches.
- Application-owned quantity is computed from persisted app BUY fills minus persisted app SELL/protective fills, excluding cycles marked manually handled.

## State invariants

- A submitted-order stage is entered only after the adapter returns a submission handle rather than raising a submission error.
- Submission failure rolls the pure state back to a waiting stage or raises recovery state; it does not leave a fictional active broker order.
- Positive BUY fills are managed even when the original order is only partially filled.
- A cycle is complete only when the locally recorded app-owned quantity has been sold/resolved.
- A stored active cycle requires explicit operator Start/resume after process launch.
- A cached recovery-probe order row is not treated as permanently current: a newer matching terminal broker poll removes/supersedes it, while a newer broker probe remains visible.

## SELL-order invariants

- The controller does not intentionally submit a final SELL while a protective SELL may still be working.
- The controller does not intentionally submit a market-close SELL while another app-created SELL may still be working.
- Cancellation request and cancellation confirmation are separate states.
- Replacement quantity is recalculated after known protective/final fills.

## Entry fail-closed invariants

A new BUY is blocked when required facts are missing, stale, inconsistent, or explicitly disallowed. Relevant facts include:

- local API socket, confirmed Gateway/TWS upstream IBKR connectivity, and completed post-reconnect reconciliation;
- qualified contract;
- current RTH state;
- selected price from a newly consumed market-data event and actual-event freshness;
- data type in live mode;
- ATR readiness when configured;
- current session window;
- configured limits, including the fixed user-saved Maximum spread % (live bid/ask never rewrites the threshold);
- local unsold app quantity;
- strict what-if acceptance based on a real non-error `OrderState` and finite margin/equity output;
- route-specific market-rule price normalization when IBKR advertises a rule;
- minimum-tick-valid payload;
- recovery confidence.

The complete GUI blocker list is informational; the order path uses fail-closed priority and stops at the first submission blocker.

## Broker rejection invariants

- App-owned order errors are retained and tied to the app `OrderRef`; manual orders are not attributed to BouncyBot.
- A short bounded cache may hold a definitive order error that arrives before the new `Trade` object, but unrelated contract/market-data request errors are not cached as order ownership facts.
- An unfilled BUY that becomes `Inactive` or `Rejected`, or has a substantive terminal rejection, moves to `ERROR` and is not automatically retried.
- `Cancelled`/`ApiCancelled` without a substantive rejection remains an ordinary Stage-2 reset. Code 202 alone does not activate the circuit breaker.

## Exit policy

Entry-specific hard risk limits do not intentionally block risk-reducing SELLs for an existing app position. Exit actions still require coherent order/quantity state, connection, valid contract, RTH/order constraints, and safe cancellation sequencing.

## Persistence invariants

- Draft settings and cycle snapshots are separate.
- Order, execution, decision, broker, and event records preserve identities and raw diagnostic payloads where available.
- Execution IDs are deduplicated.
- Schema migration is additive.
- Backups use the SQLite online backup API and are restore-validated before retention.
- UTC is the canonical application timestamp zone.

## GUI presentation invariants

- The GUI does not determine trading actions.
- The flowchart and audit timeline are explanatory views.
- The input lock prevents accidental command/configuration clicks but does not stop monitoring.
- Expected guard pauses use caution presentation.
- Red indicates a broker/local inconsistency or manual-review condition, not a routine configured wait.
- A routine guard pause or ordinary strategy wait disables recovery-changing buttons; read-only broker refresh and audit export remain available.
- The fixed workflow command bar is the only dashboard control surface; Recovery / audit log uses the full dashboard width in all view modes.
- Stop, exit, and Reconciliation market-close quantities come from the persisted app-owned fill ledger, not the account-wide broker position.

## Market-data invariants

- ATR samples are accepted only during open RTH.
- ATR uses app-observed prices and completed fixed-duration bars. Observation/bar collection continues while adaptation is disabled, but calculated percentages are not applied.
- ATR observations are in-memory session state and are not restored after an application restart.
- When warmup blocking is active, Stage 1 has no manual/fallback drop trigger before ATR readiness.
- A local socket reconnect invalidates cached subscription handles and rebuilds quote diagnostics.
- Code 1101 discards lost subscription handles; code 1102 retains handles but invalidates prior event metadata.
- Re-reading non-null cached `Ticker` fields does not refresh quote age, advance Stage 1/3, or add ATR/volatility samples.
- Every actual update is consumed once by subscription identity and sequence; its callback time defines freshness and ATR bucketing.
- A completed debug capture is written only after the entire post-fill window is observed.

## Assumptions outside application control

The invariants cannot guarantee:

- broker/exchange fill prices;
- continuity of the API connection;
- completeness of recent-execution recovery windows;
- availability/accuracy of market data;
- broker-side separation of manual and app-created shares;
- successful cancellation before an order fills;
- account buying power or permissions remaining unchanged after what-if.

When one of these assumptions is uncertain, recovery state is preferable to an inferred action.
