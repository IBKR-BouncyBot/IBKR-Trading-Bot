# legacy release Stop strategy market-exit option

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

The 5. Stop strategy dialog now has an option to sell the active cycle's app-bought unsold quantity with a market order.

The controller calculates the close quantity from app state:

```text
remaining = buy_filled_qty - max(final_sell_filled_qty, protective_sell_filled_qty)
```

Before the market close-out order is sent, the app cancels any app-owned working BUY, protective SELL, or final SELL orders. If an app-created SELL is already working, the controller waits until TWS/IB Gateway reports it is no longer working before submitting the market SELL. This avoids two app-created SELL orders working for the same app-owned position at the same time.

The market order uses the existing order path with `outsideRth=False`.
