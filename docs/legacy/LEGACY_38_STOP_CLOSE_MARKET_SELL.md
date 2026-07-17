# legacy release Stop strategy market-sell option

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

The 5. Stop strategy dialog has a user-initiated option to close the app-owned
unsold position by market order.

Behavior:

1. Calculate the app-owned unsold quantity from the active cycle:
   `buy_filled_qty - max(sell_filled_qty, protective_sell_filled_qty)`.
2. Cancel app-owned open order references stored on the cycle.
3. If the adapter can read broker position size, cap the market-sell quantity to
   the current long position.
4. Submit a `SELL MKT` order with a new app-owned `OrderRef` containing
   `STOP_CLOSE_SELL_MKT`.
5. Continue polling the submitted sell order through the existing final SELL
   path, so fills are stored in executions/history and debug captures continue.

The feature intentionally does not sell unrelated manual positions. RTH-only
protection remains active and the market order is transmitted with
`outsideRth=False`.
