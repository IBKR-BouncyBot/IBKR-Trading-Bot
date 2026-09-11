# v3.0.15 contract-session RTH guards and Trade-history graph alignment

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This release changes two bounded areas: session-window guard timing and the horizontal scale used by the two historical trade graphs.

## Contract-derived RTH session boundaries

The production adapter already used IBKR contract `liquidHours` to decide whether regular trading hours were open. It now also exposes the parsed date-specific regular-session open and close, in the contract timezone, on the RTH status snapshot.

The controller uses those same boundaries for:

- blocking a new BUY during the configured first minutes after the regular-session open;
- blocking a new BUY during the configured last minutes before the regular-session close;
- cancelling an unfilled app BUY trail during the configured pre-close window.

This removes the previous independent 09:30–16:00 assumption from those controller guards. A contract day reported by IBKR as 09:30–13:00 therefore uses 13:00 for both the last-minute entry guard and cancel-before-close logic.

The main order-submission RTH check and `outsideRth=False` broker setting are unchanged. The adapter retains its existing conservative weekday 09:30–16:00 New York fallback only when IBKR does not return usable contract hours. If no usable boundary is available at all, a new BUY fails closed; the controller does not invent a cancellation time for a working BUY.

## Trade-history graph alignment

The Trade-history audit view contains two stacked plots:

- captured market-data prices;
- app actions such as anchor/drop, BUY, final SELL, stage transitions, and guards.

Both plots now use the plotted market-data capture interval as their shared horizontal timestamp window whenever a usable captured path exists. Older cycle metadata or diagnostic events can no longer expand the axis and compress the blue market path into one side of the graph. Timed app events outside the capture interval remain visible at the nearest edge and are disclosed in the graph explanation text.

Matching vertical time guides are drawn through both plots to make the shared scale visually explicit. Histories without a usable market-data time window retain the existing all-event/fallback positioning behavior.

## Safety boundaries

- Strategy percentages, price selection, order types, order quantities, fills, reconciliation, persistence, and backup behavior are unchanged.
- The RTH source remains IBKR contract metadata where available; no external calendar or internet service was added.
- A `CLOSED` contract day remains closed and has no fabricated open/close boundary.
- Split `liquidHours` ranges use the earliest regular-session start and latest regular-session end for first/last-minute calculations, while the existing base RTH check still treats gaps between ranges as closed.

## Verification

Focused tests cover normal sessions, date-specific early closes, closed days, split sessions, fallback boundaries, first/last-minute blocking, pre-close BUY cancellation, and the shared historical-graph axis when older action timestamps exist.
