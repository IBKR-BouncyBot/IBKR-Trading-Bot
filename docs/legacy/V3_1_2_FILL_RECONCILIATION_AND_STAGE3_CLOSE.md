# v3.1.2 fill reconciliation, instance isolation, and Stage-3 pre-close exit

**Release:** v3.1.2
**Baseline:** corrected v3.1.1 source
**Database migration:** additive, one defaulted cycle-state column

## Purpose

v3.1.2 addresses defects found by comparing live IREN and NBIS audit bundles. A BUY could receive another partial fill while cancellation was in flight, but the cycle had already moved to Stage 3 and stopped supervising the original BUY. Late execution and commission callbacks were primarily audited rather than applied to the durable fill ledger. A Master-client feed could also associate another portable instance's app-prefixed callback with the current cycle, changing audit state despite a mismatched full order reference.

The release also reduces repeated native-trail diagnostics, corrects execution-time persistence, and extends the existing pre-close option to a narrowly defined profitable Stage-3 exit.

## 1. BUY settlement remains in Stage 2 until terminal

A first positive BUY fill no longer ends Stage 2 immediately. BouncyBot:

1. records the cumulative fill;
2. requests cancellation of the unfilled remainder once;
3. keeps polling the original BUY;
4. applies any additional fills received during the cancellation race;
5. enters Stage 3 only when IBKR reports the original order terminal.

`buy_remainder_cancel_requested` is persisted so restart/recovery does not submit duplicate cancellation requests. A failed cancellation call clears the one-shot flag so a later broker cadence can retry.

## 2. Idempotent late execution and commission reconciliation

Execution rows are keyed by exact IBKR execution ID. Replayed `execDetails` or commission callbacks update the same row. Commission can arrive before or after execution details.

When order status exposes a cumulative fill before execution IDs arrive, SQLite stores one residual placeholder identified by the order reference and side. The placeholder represents only cumulative quantity and commission not yet backed by real execution IDs. It shrinks as callbacks arrive and is removed when the individual ledger fully represents the broker total.

The controller projects the ledger back to cycle totals monotonically. A partial callback set cannot reduce a larger broker cumulative quantity already recorded. Late commission callbacks can update completed-cycle net P/L without duplicating shares.

Safety checks stop in `ERROR` when:

- a late BUY increases app-owned quantity after an exit order already exists;
- cumulative SELL quantity exceeds app-owned BUY quantity;
- a terminal BUY or replacement close reports inconsistent quantities.

## 3. Strict multi-instance order ownership

The shared `IBKRBOT|` prefix identifies BouncyBot orders, but it is not sufficient ownership proof. This release requires the complete `OrderRef` to exactly match a value already persisted in the local database before the controller may:

- attach a broker callback to a cycle;
- apply an execution or commission;
- display an order error as the current cycle's error;
- include an open order in recovery cancellation.

Unmatched app-prefixed Master-feed events remain unowned diagnostics with no cycle link. No installation-specific identifier was added to the reference format. The trade-off is deliberate: when the local database has been lost, broad prefix-based automatic recovery is no longer attempted and manual reconciliation may be required.

## 4. Stable native-trail diagnostic throttle

The earlier throttle key included the complete diagnostic text. Changing prices therefore created a new key and could write a warning every strategy tick. The key is now stable for one cycle, order side, and exact order reference. Numeric details remain current in the emitted message, but repeated diagnostics are limited by the configured interval.

## 5. Execution timestamps

For live `execDetails` callbacks, ib_async records a receipt timestamp from its wrapper clock. v3.1.2 uses that aware UTC value as the canonical execution timeline and preserves the broker-decoded execution time separately in raw/audit data. For recovered fills, the decoded broker execution time is used. This removes the observed host-timezone double-offset while retaining both facts for diagnosis.

## 6. Profitable Stage-3 close before RTH

The existing **Cancel SELL trail and liquidate before close** setting remains off by default and retains the same configurable cutoff. In Stage 3, the workflow starts only when:

- RTH is confirmed open;
- the contract-specific cutoff has been reached;
- a fresh selected current price is available;
- that selected price is strictly greater than the weighted average BUY fill price.

Commissions are intentionally ignored for this eligibility comparison.

If no protective SELL is working, BouncyBot submits one RTH-only `DAY` market SELL for the app-owned unsold quantity. If a protective SELL is working, it requests cancellation once, waits for a terminal status, applies any fills during cancellation, rechecks the price condition, and submits only the remaining quantity. If the price is no longer strictly above the average BUY price after cancellation, the cycle enters `ERROR` rather than transmitting the replacement.

A qualifying quote does not guarantee a profitable fill. The market order can execute below the checked quote, below the average BUY price, or at a loss. No outside-RTH fallback is submitted. The established Stage-4 cancel-confirm-replace behavior remains intact.

## Database compatibility

v3.1.2 adds one column to `cycles`:

```text
buy_remainder_cancel_requested INTEGER NOT NULL DEFAULT 0
```

Migration uses the existing additive, idempotent schema path. Existing v3.1.1, v3.1.0, and v3.0.19 databases remain forward-compatible. Existing cycles default the new flag to false. No order, execution, event, or history table is rewritten.

## Verification boundary

The release includes deterministic tests for partial-fill races, callback ordering/replay, cumulative placeholders, completed-cycle commission updates, exact multi-instance isolation, throttle identity, timestamp conversion, Stage-3 cutoff boundaries, protective cancel/replace, restart recovery, and quantity conflicts, in addition to the complete repository gates.

Automated offline tests do not prove live exchange behavior. Before production use, run the Windows `run_all_tests.bat` gate, build the PyInstaller package, and exercise at least one paper-account partial BUY/cancel race and Stage-3 pre-close workflow with TWS or IB Gateway.
