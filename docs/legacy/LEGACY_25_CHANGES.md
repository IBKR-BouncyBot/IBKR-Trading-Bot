# legacy release changes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release continues from the active legacy release-risk branch, keeps the pre-rebrand app identity, and applies workflow/GUI/test updates.

## GUI workflow

- `1. Connect to ... API` is bold and adapts to TWS or IB Gateway.
- Pressing Enter in the ticker field runs the same action as `Search for ticker`.
- `2. Use selected match`, `3. Confirm ticker + get first price`, and `4. Start strategy` make the intended order of operations explicit.
- The app path placeholder now reads `Optional path to IB Gateway` or `Optional path to TWS`.

## Strategy inputs

- Question-mark tooltips were added next to editable/selectable strategy controls.
- Risk-limit suggestions are scaled from the investment amount.
- `0` disables an individual optional hard-risk limit while preserving the master hard-risk checkbox.
- The Strategy input map is larger so text fits the boxes, and the native-trailing-order note is displayed below the map.

## Flowchart and history

- The Strategy flowchart tab can render the current strategy/cycle or a selected previous completed trade.
- Trade history is now before Recovery, and Recovery is the right-most tab.

## Tests

- Added offline examples shaped after documented IBKR TWS API callback data: contract search, market data, open/order status, executions/commissions, and what-if margin data.
