# Recovery invariants and guardrails

This document is the technical companion to [`RECOVERY_AND_FAILSAFE.md`](RECOVERY_AND_FAILSAFE.md). It lists the invariants the controller attempts to preserve.

## Ownership invariants

- Application orders have an `OrderRef` beginning with `IBKRBOT|`. The prefix alone is not ownership proof.
- Cancel/recovery actions require an exact full `OrderRef` already persisted locally; unmatched prefixed Master-feed orders are ignored. Cancellation also requires current API-client ownership of that exact broker order and a matching order ID when supplied.
- An external/manual account position does not become application-owned merely because its ticker matches.
- Application-owned quantity is computed from persisted app BUY fills minus persisted app SELL/protective fills, excluding cycles marked manually handled.

## State invariants

- Order intent is durable before the broker call; returned submission handles provide the reported status and broker IDs.
- Definite pre-transmission failures may roll back to a waiting stage. An uncertain transmission retains the exact reference and available IDs with `SUBMISSION_UNKNOWN`, `MANUAL_REVIEW`, and recovery required; it cannot be automatically retried from an empty open-order response.
- A positive partial BUY remains in Stage 2 until the original order is terminal. The controller allows a fixed 3.0-second completion grace, then requests one remainder cancellation after timeout or immediately when an enabled market/session safety boundary becomes unsafe; later fills and commissions are reconciled idempotently before Stage 3 begins.
- A cycle is complete only when the locally recorded app-owned quantity has been sold/resolved.
- A stored active cycle requires explicit operator Start/resume after an ordinary process launch. The authenticated immediate watchdog replacement exception requires the exact prior monitored cycle and normal broker reconciliation.
- A cached recovery-probe order row is not treated as permanently current: a newer matching terminal broker poll removes/supersedes it, while a newer broker probe remains visible.

## SELL-order invariants

- The controller does not intentionally submit a final SELL while a protective SELL may still be working.
- The controller does not intentionally submit a market-close SELL while another app-created SELL may still be working.
- Cancellation request and cancellation confirmation are separate states.
- Replacement quantity is recalculated after known protective/final fills. The normal protective-to-final handoff verifies replacement feasibility before cancellation and revalidates afterward.
- Initial protective SELL placement waits for a terminal BUY with a positive filled quantity; the first nonterminal partial fill does not itself place protection.

## Entry fail-closed invariants

A new BUY is blocked when required facts are missing, stale, inconsistent, or explicitly disallowed. Relevant facts include:

- local API socket, confirmed Gateway/TWS upstream IBKR connectivity, and completed post-reconnect reconciliation;
- exact positive-conId contract matching the stored USD/EUR ordinary STK selection, SMART route, and required order capabilities;
- current RTH state;
- selected price from a newly consumed market-data event and actual-event freshness;
- data type in live mode;
- ATR readiness when configured;
- current session window;
- configured limits, including the fixed user-saved Maximum spread % (live bid/ask never rewrites the threshold);
- local unsold app quantity;
- strict what-if acceptance based on a real non-error `OrderState` and finite margin/equity output;
- route-specific market-rule price normalization when IBKR advertises a rule;
- market-rule-valid price and whole-share quantity conforming to the contract minimum/size increment;
- recovery confidence.

The complete GUI blocker list is informational; the order path uses fail-closed priority and stops at the first submission blocker.

## Exact-contract and currency invariants

- The live adapter requires a positive API-selected conId; symbol-only qualification is not accepted.
- Qualification must return the same conId, USD/EUR currency, ordinary STK type, and SMART route.
- IBKR must provide nonempty route/order metadata supporting SMART, `MKT`, and `TRAIL`; absent or incompatible metadata blocks qualification.
- The first persisted cycle locks the portable database to one contract currency. A zero-cycle draft may be rebound; a database with cycles may not.
- Every contract without usable authoritative IBKR ContractDetails `liquidHours` and `timeZoneId` is blocked for RTH-restricted submissions. The schedule is not inferred from price ticks. No weekday session or timezone is guessed, and an unavailable authoritative window produces no estimated hours or countdown in the GUI. `LSE` and `LSEETF` are also capped at their verified 08:00-16:30 `Europe/London` continuous session; the cap may narrow but never extend IBKR's date-specific window.
- A commission in another currency is not subtracted from local net P/L and disables Auto-repeat because no FX conversion is implemented.

## Broker rejection invariants

- App-owned order errors are retained and tied to the app `OrderRef`; manual orders are not attributed to BouncyBot.
- A short bounded cache may hold a definitive order error that arrives before the new `Trade` object, but unrelated contract/market-data request errors are not cached as order ownership facts.
- An unfilled BUY that becomes `Inactive` or `Rejected`, or has a substantive terminal rejection, moves to `ERROR` and is not automatically retried.
- With no fill, `Cancelled`/`ApiCancelled` without a substantive rejection remains an ordinary Stage-2 reset. With a positive fill, terminal settlement advances to position management. A substantive terminal partial-BUY rejection stops repetition while the acquired position continues to be managed. Code 202 alone does not activate the circuit breaker.

## Exit policy

Entry-specific hard risk limits do not intentionally block risk-reducing SELLs for an existing app position. Exit actions still require coherent order/quantity state, connection, valid contract, RTH/order constraints, and safe cancellation sequencing.

## Persistence invariants

- Draft settings and cycle snapshots are separate.
- Order, execution, decision, broker, and event records preserve identities and raw diagnostic payloads where available.
- Execution IDs are deduplicated.
- Schema migration is additive. The one-database contract-currency lock is stored in existing `app_settings`; mixed USD/EUR cycle evidence fails closed.
- Backups use the SQLite online backup API and are restore-validated before retention.
- UTC is the canonical application timestamp zone. Monetary totals are kept in the database contract currency; there is no automatic FX conversion.

## GUI presentation invariants

- The GUI does not determine trading actions.
- The flowchart and audit timeline are explanatory views.
- The input lock prevents accidental command/configuration clicks but does not stop monitoring.
- Expected guard pauses use caution presentation.
- Red can indicate a broker/local inconsistency or manual review. The Start card also retains its existing red Blocked state for Stage 1 BUY guards; that card alone does not establish a recovery fault. Running stages 2–4 do not inherit this classification from old guard messages.
- A routine guard pause or ordinary strategy wait disables recovery-changing buttons; read-only broker refresh and audit export remain available.
- The fixed workflow command bar is the only dashboard workflow surface and is hidden while the GUI lock is engaged; Recovery / audit log uses the full dashboard width in Advanced/Debug and is hidden only in Simple; recovery actions remain available.
- Stop, exit, and Reconciliation market-close quantities come from the persisted app-owned fill ledger, not the account-wide broker position.

## Market-data invariants

- ATR samples are accepted only during open RTH.
- ATR uses app-observed prices in fixed-duration OHLC buckets; the latest bucket may still be forming. Observation/bar collection continues while adaptation is disabled, but calculated percentages are not applied.
- Raw ATR observations are in-memory session state and are not restored after an application restart. A validated saved ready estimate may seed readiness while current-session bars warm up at a verified open RTH session.
- When warmup blocking is active, Stage 1 has no manual/fallback drop trigger before ATR readiness.
- A local socket reconnect invalidates cached subscription handles and rebuilds quote diagnostics. After local loss, the endpoint is retried every 10 seconds indefinitely until connected, manually disconnected, or the application exits.
- Code 1101 discards lost subscription handles; code 1102 retains handles but invalidates prior event metadata.
- Re-reading non-null cached `Ticker` fields does not refresh quote age, advance Stage 1/3, or add ATR/volatility samples.
- Every actual update is consumed once by subscription identity and sequence; its callback time defines freshness and ATR bucketing.
- A completed debug capture is queued for writing only after its monotonic post-fill deadline has elapsed. It contains the available observations; elapsed time does not guarantee continuous market-data coverage.

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
