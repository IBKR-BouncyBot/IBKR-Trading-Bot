# legacy release market-data capture and Stage 2 review

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

> legacy release note: terminal no-fill orders no longer enter Manual Review. BUY no-fill now stops the cycle without a position; SELL-side no-fill states pause in ERROR while market data continues to update.


## Rolling market-data capture

The controller now owns a RAM-only `MarketDataCaptureManager`.

* It keeps the recent market-data snapshots in memory.
* On each BUY, protective SELL, or final SELL fill, it starts a pending capture.
* The capture contains the 15 minutes before the fill and keeps collecting the 15 minutes after the fill.
* No capture file is written until the full post-trade window is complete.
* If the app or Windows closes before the post-trade window completes, the pending capture is intentionally lost.

Completed capture packages are written under:

```text
<AppFolder>\debug_captures\<ticker>\<cycle>\*.zip
```

Each package contains:

```text
manifest.json
market_data.csv
market_data.jsonl
event.json
```

## Stage 2 review

Stage 2 means a native IBKR BUY trailing-stop order has been accepted and is working in TWS/IB Gateway. The app does not manually trigger the fill; it polls the broker order state and records market data.

With ATR adaptive mode enabled, ATR-derived percentages can continue to update future exit settings, but the already-submitted native BUY trailing-stop order is not modified. Therefore, if Stage 2 remains active, the likely causes are:

* the rebound threshold was not reached from the lowest price seen by the native trailing order;
* the ATR-derived buy-rebound/trail percentage was wider than expected at submission time;
* the order was not actually working anymore but TWS returned a terminal no-fill status.

legacy release fixes the last case. If a BUY trailing-stop, protective SELL, or final SELL order reaches a terminal no-fill state such as `Cancelled`, `ApiCancelled`, `Inactive`, or `Rejected`, the app no longer leaves the cycle apparently active. It moves the cycle to manual review and logs a structured audit event.
