# Strategy flowchart tab

The Strategy flowchart is a read-only explanation of the configured five-stage cycle. It is generated from `app/flowchart_model.py` using the same model helpers used by strategy calculations where practical.

It is not a second trading engine and does not submit, cancel, or modify orders.

## What it shows

The full view contains all five business stages:

1. wait for initial drop;
2. BUY trail/market entry;
3. wait for minimum-profit activation;
4. SELL trail/market exit;
5. cycle complete/repeat.

Cards show configured percentages, current cycle values, projected prices/quantity when calculable, order summaries, and active-stage highlighting.

Optional behavior appears inside the relevant stage rather than adding a sixth business stage. For example, a protective SELL is described in the post-BUY/Stage-3 context.

## View modes

- **Full strategy** displays all five cards regardless of simple/advanced GUI mode.
- **Current cycle only** narrows the display to the active/current context.
- GUI simplicity controls can hide explanatory inputs, but they do not change strategy stages or collapse the full flow to three cards.

## Flowchart data selector

The **Flowchart data** selector is available in Simple, Advanced, and Debug GUI modes. It can display either the current strategy/active cycle or a completed cycle supplied by Trade history.

Selecting a completed cycle is stable while a live strategy is running: incoming live snapshots update the cached current-cycle data but do not force the selector back to the active cycle. Selecting **Current strategy / active cycle** later displays the latest cached live state.

## Current versus projected values

The flowchart can combine:

- persisted active-cycle values;
- current draft strategy settings;
- latest selected price;
- model projections.

A projection is not an accepted IBKR order. Broker tick-size normalization, ask/last reference selection, current guards, what-if results, and order acceptance are applied later by the controller/adapter.

## ATR display

When ATR is ready, the flowchart reflects the order-driving adaptive percentages applied to the active/draft strategy. During blocked warmup, Stage 1 should indicate that no initial-drop trigger is armed.

The flowchart does not create historical bars or make ATR ready; it displays controller/model state.

## Profit display

Minimum profit is measured from the actual average BUY fill once available. Before a fill, the GUI can show projections based on the configured/reference prices. These remain before commissions and actual slippage.

## Status colors

Color and active-card emphasis are operator aids. The authoritative eligibility summary is the top **Trading** status and its blocker tooltip; the authoritative broker state is the Reconciliation/order data.

## Audit limitation

The flowchart describes current strategy logic. For a completed cycle, use Trade history and the cycle audit dialog to inspect actual stored inputs, orders, executions, events, and timeline. Later draft edits do not rewrite the completed cycle snapshot.
