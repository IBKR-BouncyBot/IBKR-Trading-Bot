# v3.1.1 IBKR order validation and rejection handling

## Purpose

v3.1.1 corrects four related broker-boundary failures found while comparing an IREN instance whose BUY trailing orders were rejected with an NBIS instance whose orders were accepted.

The IREN contract advertised a very small `ContractDetails.minTick`, but its SMART route also advertised a price-dependent market rule. BouncyBot treated `minTick` as the valid increment at every price and submitted four-decimal trailing-stop prices. IB Gateway returned error 201, `Invalid Price`. At the same time, the optional what-if request used an invalid transmit flag, its `ValidationError` result was reported as a pass, the broker rejection callback was discarded, and the terminal `Inactive` order reset to Stage 1 and could be retried repeatedly.

## Market-rule price normalization

For live IBKR contracts, BouncyBot now:

1. reads `validExchanges` and the positionally corresponding `marketRuleIds` from the selected `ContractDetails` row, preserving empty rule positions so another exchange's rule cannot be shifted onto the selected route;
2. selects the market rule for the requested route, normally SMART;
3. requests the rule's price bands through `reqMarketRule`;
4. selects the increment that applies at the proposed price;
5. rounds BUY stop and sizing prices upward and SELL stop prices downward;
6. re-evaluates the price band if rounding crosses a rule boundary; and
7. caches successfully loaded contract details and market rules for the adapter session.

If IBKR advertises market-rule pricing but the relevant rule cannot be selected or loaded, order submission fails closed before an order intent is transmitted. The app does not silently fall back to the smallest contract-level `minTick` in that condition.

Adapters and deterministic test doubles that do not advertise a market rule retain the established contract-level minimum-tick fallback.

## Strict what-if validation

What-if orders now use the dedicated `IB.whatIfOrder` request with `whatIf=True` and `transmit=True`. The what-if flag prevents a normal live transmission while satisfying the broker API's validation requirement.

The result fails closed when:

- IBKR returns no `OrderState`;
- the status is `ValidationError`, `Inactive`, `Rejected`, `Cancelled`, `ApiCancelled`, or another error state;
- warning text reports rejection, invalid input, insufficient funds, or another validation failure;
- the response contains only IBKR unset-value sentinels; or
- no usable margin or equity impact is returned.

Legitimate numeric zero changes remain valid and are not converted to missing values.

## Broker rejection diagnostics

Order-related IBKR error callbacks for app-owned orders are now retained with:

- error code and message;
- request/order ID;
- app `OrderRef`;
- permanent order ID when available;
- ticker and currency;
- advanced rejection JSON when supplied; and
- callback timestamp and diagnostic callback representation.

The adapter handles the callback race where IBKR reports the error before the newly placed trade has been registered locally. Only definitive order-validation codes or clearly order-specific messages enter the unbound race cache; unrelated contract-details and market-data request errors are excluded. Pending candidates are bounded to 256 entries and expire after 30 seconds. Manual orders without the `IBKRBOT|` prefix are not attributed to BouncyBot.

The controller persists these records in `broker_events`, adds a structured `BROKER_ORDER_ERROR` decision event, and exposes the broker reason through the existing status and audit interfaces.

## No-fill rejection circuit breaker

A BUY order that reaches `Inactive` or `Rejected` with no fill no longer returns to Stage 1 for an automatic fresh attempt. A terminal no-fill state carrying a substantive broker validation/rejection error also stops the cycle.

The cycle moves to `ERROR` for manual review and records that no replacement or automatic fresh-cycle retry will be sent. The order reference and broker identifiers remain available for diagnosis.

Normal confirmed cancellations remain separate. `Cancelled` or `ApiCancelled` with no substantive rejection resets an unfilled Stage-2 BUY to Stage 1. IBKR code 202, the ordinary cancellation notification, does not activate the rejection circuit breaker by itself.

## Compatibility and scope

- There is no SQLite schema change in v3.1.1.
- Existing v3.1.0 and v3.0.19 databases remain forward-compatible.
- Strategy percentages, ATR calculations, Stage-4 close-before-RTH liquidation, order ownership, quantity calculations, fill accounting, and recovery policy are unchanged except where an order is blocked or stopped by the corrected validation behavior.
- A successful what-if result is a preflight fact, not a guarantee that a later live order will be accepted or filled.
- Market-rule and callback behavior still requires a live TWS or IB Gateway paper-account exercise before production use.

## Verification

The final source release is verified by the complete repository test collection, focused market-rule/what-if/error/circuit-breaker regressions, branch coverage, callable-entry coverage, safety mutation tests, deterministic CSV simulations, bounded soak tests, Python compilation, patch reconstruction, clean-archive extraction, and artifact checksum validation. Exact final counts are recorded in the release test report distributed with the source ZIP.
