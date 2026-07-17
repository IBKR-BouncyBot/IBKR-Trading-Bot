# v3.0.11 Gateway connectivity and quote-freshness corrections

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

> This is a historical release note for v3.0.11. For current behavior, use [`../../README.md`](../../README.md), [`ARCHITECTURE.md`](../ARCHITECTURE.md), [`RISK_CONTROLS.md`](../RISK_CONTROLS.md), [`OPERATIONS.md`](../OPERATIONS.md), and [`RECOVERY_AND_FAILSAFE.md`](../RECOVERY_AND_FAILSAFE.md).

## Problem addressed

A local API socket can remain connected to TWS or IB Gateway while that platform has lost its upstream connection to IBKR servers. In addition, an `ib_async` `Ticker` object retains its last populated values. Earlier controller logic could therefore keep seeing non-null bid, ask, and last fields, reset the quote-freshness timer, and feed repeated cached values to waiting strategy stages and ATR collection even though no new ticker event had arrived.

## Separate connectivity states

v3.0.11 records the local API socket and Gateway/TWS upstream link independently.

- IBKR code **1100** or **2110** marks the upstream link unavailable, invalidates quote freshness, and pauses strategy advancement, order polling, and new order submission.
- Code **1101** marks connectivity restored with market data lost. Obsolete ticker handles are discarded so the next price read creates new subscriptions.
- Code **1102** marks connectivity restored with subscriptions maintained. Existing handles are retained, but their update metadata is reset and a new ticker event is required before prices are usable.
- Code **1300** marks an API-port reset and requires normal local-socket reconnection.

After 1101 or 1102, the controller reconciles app-owned open orders and recent executions before ordinary worker processing resumes.

## Actual update identity

The production adapter listens to `pendingTickersEvent` and assigns:

- a unique subscription identity to each created market-data handle;
- a monotonically increasing sequence number to each actual ticker event;
- the event callback wall-clock and monotonic arrival times.

The controller consumes each `(subscription identity, sequence)` only once. A repeated read of the same populated `Ticker` is diagnostic cached data, not another quote update. If the production adapter cannot register `pendingTickersEvent`, it fails closed and does not substitute cached-field reads. Cached data cannot:

- refresh the selected-price age;
- advance Stage 1 or Stage 3;
- trigger a BUY or SELL decision;
- add an ATR observation;
- add a recent-volatility observation.

The event callback time is used for age calculations and ATR bucketing so delayed controller processing does not move an older event into a newer bar.

## GUI behavior

The status area now distinguishes:

- **Disconnected** — no local API socket;
- **Gateway only** — local socket exists but the upstream IBKR link is unavailable;
- **Reconciling** — upstream connectivity returned but broker state is still being verified;
- **Data pending** — broker connectivity is available but no post-connect/post-recovery ticker event has arrived;
- **Connected** — the link is available and ordinary monitoring can continue.

The Data indicator distinguishes fresh actual updates, recent-but-not-new reads, stale data, cached-only fields, invalidated data, and an upstream outage. Cached values may remain visible to support diagnosis, but their tooltip states that they are not strategy-usable.

While the upstream link is unavailable or post-restoration reconciliation is pending, the fixed workflow command bar disables contract search, ticker confirmation, and strategy start. The controller independently rejects those commands, recovery refresh, cancellation, and market-close requests at the worker boundary, so GUI state is not the only safeguard. The local **Stop strategy** path remains available for operator review of an already active cycle.

## Order behavior

New BUY, final SELL, protective SELL, and market-close transmission paths recheck local and upstream connectivity immediately before placing the order. Post-reconnect reconciliation must also be complete.

An order already accepted by IBKR is not cancelled merely because connectivity is lost. It may continue operating at the broker. When connectivity returns, the application imports visible order status and executions before resuming normal processing. A BUY that fills while the application is unable to receive events can delay application-side follow-up, including protective-order placement, until recovery succeeds.

## Regression coverage

Focused tests cover:

- actual ticker-event identity versus cached reads;
- 1100 fail-closed behavior;
- late cached callbacks during an outage;
- 1101 subscription recreation;
- 1102 subscription retention plus fresh-event gating;
- callback-age propagation into stale-data and ATR timing;
- exclusion of repeated cached reads from ATR history;
- worker-loop pausing during an upstream outage;
- BUY and SELL submission blocking;
- post-reconnect reconciliation gating;
- fail-closed behavior when ticker-event tracking is unavailable;
- stale-streaming-data presentation for a waiting SELL;
- a final callback pump at the order-submission boundary;
- a local-only initial connect and auto-reconnect that do not continue into broker recovery;
- command-boundary blocking for search, confirmation, start, refresh, cancel, and market close;
- workflow-button gating while upstream connectivity or reconciliation is incomplete;
- one-time reconciliation when a restoration message arrives during the synchronous connection handshake.
