# v3.1.0 Close-before-RTH liquidation

## Purpose

v3.1.0 adds one optional Stage-4 exit policy for operators who prefer not to carry an app-owned position beyond the regular trading session after the final profit-protecting SELL trail is active.

The **Risk and timing** section now contains:

- **Cancel SELL trail and liquidate before close** — default **OFF**.
- **Liquidate before close** — default **5 min**, configurable from 1 through 240 minutes.

The controls can be configured before the Stage-4 native SELL trail becomes active. They are locked with the other current-cycle trading inputs after that native order is working.

## Exact workflow

The policy applies only when all of these facts are true:

1. the active cycle is in Stage 4 (`SELL_TRAIL_ACTIVE`);
2. the current final SELL order is the normal app-owned `SELL_TRAIL` order;
3. the option was enabled for that cycle;
4. IBKR contract-hours data confirms that RTH is open; and
5. the remaining time to the contract-specific RTH close is greater than zero and no more than the configured cutoff.

At the cutoff BouncyBot:

1. persists that close-before-RTH liquidation has started;
2. requests cancellation of the final SELL trail once;
3. continues polling and processing fills while cancellation is pending;
4. waits until IBKR reports a terminal order state;
5. calculates the cumulative final-SELL executions already persisted for the cycle;
6. submits one SELL market order for only the remaining app-owned quantity;
7. sets that replacement to `DAY` with `outsideRth=False`; and
8. completes Stage 5 only after the cumulative original and replacement fills equal the app-owned bought quantity.

The trailing percentage is not modified to zero. The native trail is explicitly cancelled and its terminal state is confirmed before a replacement can be submitted.

## Cancellation and fill races

Cancellation is not instantaneous. The original trailing order may fill while the cancellation request is in transit.

- A complete original-order fill finishes the cycle and suppresses the replacement.
- A partial original-order fill is persisted and deducted from the replacement quantity.
- A nonterminal original order remains the only SELL order. BouncyBot does not submit a second SELL merely because the cutoff or closing time has passed.
- Cumulative execution quantity is checked against the app-owned bought quantity. Any apparent over-sell moves the cycle to `ERROR` for manual review.

Manual **Sell app-bought unsold position with market order** is also blocked while this automatic cancel-confirm-liquidate workflow is active, preventing two app-created market exits from being started for the same shares.

## Failure behavior

The policy deliberately fails closed:

- If the contract's RTH boundary cannot be verified, automatic liquidation does not start.
- If cancellation is not confirmed before the close, no replacement is submitted and the original trail remains the only app exit order while its broker status is monitored.
- If cancellation becomes terminal after RTH has closed, no market replacement is submitted.
- If the replacement cannot be submitted, is rejected, is cancelled, becomes inactive, or remains incomplete at the RTH boundary, BouncyBot requests cancellation where possible and moves the cycle to `ERROR` for manual review.
- BouncyBot never creates an outside-RTH fallback for this policy and never silently recreates the cancelled trailing order.

The market order prioritizes liquidation rather than price. It may fill below the former trailing stop, may fill below the BUY price, and may realize a loss.

## Persistence and recovery

The cycle stores both the configured policy and two runtime workflow flags in SQLite. The schema change is additive and existing databases receive default values automatically.

After an application or TWS/Gateway interruption, recovery uses the current broker order identity, persisted executions, order status, and app-owned bought quantity. It can continue monitoring the original cancellation or the replacement order without intentionally duplicating already-recorded executions or submitting a second replacement. Ambiguous or incomplete states remain subject to the existing manual-recovery controls.

## Scope boundary

This release does not change:

- Stage-1 drop detection;
- Stage-2 BUY trailing-stop behavior or its existing near-close cancellation guard;
- Stage-3 minimum-profit behavior;
- protective SELL behavior;
- ATR calculations or multipliers;
- normal Stage-4 trailing calculations;
- order ownership prefixes;
- position sizing;
- normal fill, commission, P/L, Auto-repeat, or Stage-5 behavior; or
- market-data capture and Trade History audit presentation.

The only production behavior added is the optional Stage-4 cancel-confirm-market liquidation path described above.

## Automated verification

The v3.1.0 source release was validated with the complete repository test collection:

- 861 pytest cases collected and executed;
- 860 passed and one documented strict expected failure remained expected;
- `ResourceWarning` was promoted to an error;
- 77.1% combined statement/branch coverage against the repository's 75% gate;
- 848/848 executable application callables entered;
- 6/6 safety mutation checks killed;
- 58/58 deterministic simulation contracts passed across 54 CSV price paths; and
- Python compilation, patch whitespace, release-metadata, documentation-link, and clean-archive checks passed.

Coverage was collected in isolated test chunks because running the complete suite under one instrumented process was impractically slow on the validation host. The two timing-sensitive large-database performance tests were executed separately without instrumentation and both passed. Ruff, Pyright, a native Windows executable launch, and a live TWS/IB Gateway paper-account exercise remain required external release checks because those tools and services were unavailable in the offline validation environment.
