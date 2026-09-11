# legacy release changes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

- Reworked Strategy Flowchart sizing so the right side is no longer clipped.
- Added Trade History click-through audit dialog for per-cycle orders, executions, verbose log rows, and structured decision events.
- Moved Recovery to a right-side tab panel, leaving the three main workflow tabs on the left.
- Kept default market data as Auto best available.
- Added one UI-only example history result for empty databases; it does not affect completed-trade summary calculations.
- Removed visible question-mark help badges and moved tooltip help directly to fields/buttons/selectors.
- Expanded protective SELL and slippage-buffer tooltip explanations.
- Changed default minimum trade price to 0.00 so that specific hard-risk limit is off by default.
- Build script now runs simulated-data scenarios in addition to pytest.
