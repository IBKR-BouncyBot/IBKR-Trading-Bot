# legacy release Bug-hunting and simulation validation

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release focuses on reliability review rather than changing the trading idea.
The reviewed areas were the strategy state machine, simulated trailing-order behavior,
recovery UI placement, build-time test gates, SQLite durability, and prolonged-runtime
memory behavior.

## Issues found and fixed

1. **Recovery tab placement drift**
   - Finding: legacy release had moved Trade Recovery into a right-side tab group.
   - Fix: restored it to the main tab strip as the rightmost tab after Trade History.

2. **Rapid SQLite backups could overwrite each other**
   - Finding: backup filenames used second-level timestamps, so several backups made in
     the same second could collide.
   - Fix: backup filenames now use microsecond UTC timestamps.

3. **SQLite backup consistency under WAL mode**
   - Finding: copying only the main database file can be incomplete while SQLite WAL pages
     are active.
   - Fix: backup_database now uses SQLite's online backup API and then prunes old backups.

4. **Build-time CSV simulation coverage was narrow**
   - Finding: the Windows build only ran a few fixed CSV scenarios and duplicated some calls
     in earlier scripts.
   - Fix: the build script now runs pytest and every CSV in
     tests/simulated_data. The assertion-heavy protective-sell and slippage-buffer
     scenarios live in pytest and use the same CSV fixtures.

5. **Stale single-instance lock after crash/restart**
   - Finding: a leftover lock file could block startup after a crash or Windows restart.
   - Fix: the lock stores a PID and removes the lock automatically when that PID is no
     longer running.

6. **Long-runtime cache growth risks**
   - Finding: prolonged sessions need bounded in-memory caches.
   - Fix/validation: price-history buffers and warning-throttle caches are now covered by
     long-runtime tests.

7. **Protective SELL cancel/replace ordering**
   - Finding: a final profit SELL could be requested before protective SELL cancellation
     had been confirmed.
   - Fix: the strategy now waits until the protective SELL is no longer working before
     requesting the final profit-protecting SELL trail.

8. **Audit logging during recovery windows**
   - Finding: a warning tied to a not-yet-persisted cycle could fail on a SQLite foreign key.
   - Fix: the event is retained at ticker/global scope if the cycle row is not yet present.

## New simulated CSV scenarios

- `anchor_reset_multiple.csv`
- `long_anchor_reset_then_drop.csv`
- `long_flat_runtime.csv`
- `no_sell_trigger_holds_position.csv`
- `prolonged_no_order_anchor_reset.csv`
- `protective_cancel_then_profit_sell.csv`
- `protective_replaced_by_profit_sell.csv`
- `protective_sell_exits_before_profit.csv`
- `protective_sell_loss.csv`
- `rth_reopens_after_drop.csv`
- `slippage_buffer_budget.csv`
- `slippage_sizing_wide_rebound.csv`

These simulations are deterministic. They do not connect to IBKR and do not model exchange
liquidity, gaps, or queue priority. They are intended to verify the app's state machine and
planning math before native orders are sent to IBKR.

## Validation command

```bash
bash scripts/run_tests.sh
```

Expected result for legacy release in this environment:

```text
119 passed, 3 skipped
```

The skipped tests require a real PySide6 GUI runtime.
