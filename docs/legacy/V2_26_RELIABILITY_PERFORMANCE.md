# v2.26 reliability and performance hardening

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Scope: implemented the requested reliability and performance proposals except splitting `gui.py`/`controller.py` and live-paper integration tests. Strategy math, ATR formulas, stage-trigger thresholds, and native order parameters were not intentionally changed.

## Reliability changes

### Durable order intent ledger

Before each broker submission the controller now writes an `INTENT_CREATED` row to the `orders` table. After IBKR accepts the order, the accepted order identity and the updated cycle state are persisted in one SQLite transaction through `BotStorage.record_order_submission()`.

If the adapter raises during submission, the latest order-intent row is marked `SUBMIT_FAILED` with the error payload. This closes the previous crash window where IBKR could accept an order before the local durable order row existed.

### Broker callback event persistence

`IbAsyncTwsAdapter` now registers best-effort handlers for IBKR order/execution callback streams when available:

- `openOrderEvent`
- `orderStatusEvent`
- `execDetailsEvent`
- `commissionReportEvent`

The adapter queues normalized callback records, and the controller drains them into the new `broker_events` SQLite table. The existing polling/recovery paths remain in place; this is additive audit/recovery telemetry.

### Explicit live-account requirement

Starting a strategy in live mode now requires an explicit IBKR account in connection settings. The GUI validates this before Start, and the controller enforces the same rule independently. For already-persisted active cycles, the cycle account is still accepted as the fallback account during broker submission/recovery.

### Final BUY pre-flight gate

Immediately before every BUY submission, the controller now verifies:

- broker connection is still active;
- live account is explicitly known and, when the adapter exposes it, present in the managed-account list;
- qualified contract is still available;
- stored cycle `conId` still matches the qualified contract when both values exist;
- current broker position is confirmed and no pre-existing long position is present before a fresh BUY;
- live market-data mode is confirmed as live when the live delayed-data guard is enabled.

A failed pre-flight rolls the unsubmitted BUY back to the previous waiting stage and logs the block reason. No broker order is sent.

### More atomic critical persistence

Order acceptance now updates the cycle row and the latest order row in one SQLite transaction. Existing separate decision/audit events remain additive logs and are not part of the critical state transition.

### Safer shutdown

The controller now tracks worker shutdown completion, waits longer for deterministic shutdown, and records a warning if the worker does not finish within the timeout. Completed market-data capture write jobs are joined during shutdown.

### Typed submission escalation

Order-submission exceptions are now normalized into `BrokerAdapterError` after the order intent is marked failed. Expected broker failures remain typed and flow through the existing fail-closed controller path.

### Recovery confidence

Snapshots now include `recovery_confidence` with one of:

- `fully_reconciled`
- `broker_partially_checked`
- `local_state_only`
- `manual_review_required`

The Recovery panel displays this value and includes it in the recovery details log.

## Performance changes

### Cached and narrower history summary

`history_summary()` no longer calls `history_cycles(limit=100000)` and no longer enriches full cycle dictionaries for every snapshot. It now queries only the fields needed for summary metrics and caches the result per ticker using completed-cycle count and max `updated_at` as the invalidation key.

### Reduced open-order polling overhead

`poll_order()` now checks the cached trade and current `ib.trades()` first. It only calls `reqOpenOrders()` when the order is not already known. Recovery and `open_app_orders()` still force a full broker refresh.

### Snapshot diffing for heavy GUI panels

The GUI now avoids rebuilding the event log, history summary cards, and Recovery panel when their relevant snapshot payloads have not changed.

### Async completed-capture ZIP writing

The live controller now uses `MarketDataCaptureManager(async_writes=True)`. Completed market-data captures are queued to a writer thread, so ZIP compression does not block the trading worker tick. Tests keep the default synchronous mode unless explicitly testing async behavior.

### SQLite indexes

Added composite indexes for common cycle/order/execution query patterns:

- `idx_cycles_stage_ticker_updated`
- `idx_cycles_stage_sell_updated`
- `idx_cycles_stage_ticker_sell_updated`
- `idx_orders_cycle_status_ref`
- `idx_exec_cycle_time`
- broker-event indexes for timestamp, order ref, and execution id

## Tests added

Added `tests/test_v226_reliability_performance.py` covering:

- order intent exists before broker submission;
- accepted order identity and cycle state are updated together;
- submit failures mark order intent as `SUBMIT_FAILED`;
- live Start requires explicit account;
- BUY pre-flight blocks pre-existing broker position;
- broker callback events persist to SQLite;
- cached `poll_order()` avoids full `reqOpenOrders()` refresh;
- recovery `open_app_orders()` still forces refresh;
- history summary cache invalidates when completed cycles change;
- expected SQLite composite indexes exist;
- async capture writing moves ZIP work off the caller path.

## Final validation

Baseline before changes:

- `319 passed` pytest
- `18 CSV simulation files/scenarios passed`

Final after changes:

- `329 passed` pytest
- `18 CSV simulation files/scenarios passed`
- `python -m compileall -q .` passed
- static AST audit passed: no mutable literal defaults and no duplicate literal dict keys in `app/*.py`

## Files changed

- `app/controller.py`
- `app/gui.py`
- `app/ib_adapter.py`
- `app/market_data_capture.py`
- `app/storage.py`
- `tests/test_v226_reliability_performance.py`
- `docs/legacy/V2_26_RELIABILITY_PERFORMANCE.md`
