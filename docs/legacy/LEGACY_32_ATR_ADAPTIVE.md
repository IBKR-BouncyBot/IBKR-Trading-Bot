# legacy release ATR-adaptive percentages

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This version adds optional ATR-adaptive percentage settings. The implementation does not request a separate historical or real-time bar subscription. It uses the current app price buffer built from the existing IBKR/TWS market-data subscription, aggregates those sampled prices into fixed-time OHLC bars, calculates ATR from true ranges, and writes the resulting adaptive percentages into the same four fields used by manual mode:

- Initial drop %
- BUY rebound/trail %
- Minimum profit %
- SELL trailing-stop %

The strategy engine continues to read only those four percentage fields. ATR mode is therefore a settings generator, not a second order path. Until enough bars are available, the existing visible percentage values remain in use.

Default ATR settings:

- ATR period: 14 bars
- Bar size: 60 seconds
- Initial drop: 1.50 x ATR%
- BUY rebound/trail: 0.75 x ATR%
- Minimum profit: 1.00 x ATR%
- SELL trail: 1.00 x ATR%
- Clamp: 0.10% to 20.00%

The toggle tooltip explains the behavior in the GUI.
