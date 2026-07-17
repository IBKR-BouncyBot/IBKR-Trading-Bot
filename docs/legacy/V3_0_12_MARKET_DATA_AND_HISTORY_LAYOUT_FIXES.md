# v3.0.12 market-data and Trade-history maintenance fixes

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

This maintenance update keeps the application version at **3.0.12** and addresses four focused runtime/UI defects.

## Start workflow button gating

The command bar is the sole owner of the five workflow buttons' enabled state. The configuration-field lock routine no longer re-enables **4. Start strategy** or **5. Stop strategy** after command-bar evaluation.

Consequently, Start remains disabled while an active cycle is running or blocked, including ATR warmup, configured BUY guards, post-reconnect reconciliation, recovery/manual-review states, and the operator input lock.

## Frozen and competing market-data sessions

The live adapter continues to identify freshness only from actual `pendingTickersEvent` callbacks. The controller now also re-evaluates the callback age on every emitted GUI snapshot. A cached `Ticker` can therefore remain visible for diagnosis, but the **Live / Update** and API-data indicators change from green to stale after the configured freshness limit even if no new quote read occurred.

The adapter handles these market-data-specific IBKR messages explicitly:

- **10197** — another session has priority for live market data;
- **2103** — a market-data farm connection is broken;
- **2104** — a market-data farm reports available again.

Each message invalidates the cached event timestamp and requires a later actual ticker event before the quote is considered fresh. Code 10197 and the farm messages do not automatically classify the entire order/API channel as disconnected. Existing broker-order monitoring can therefore continue where the upstream API channel remains confirmed, while price-driven strategy decisions remain paused. A market-data-only message cannot override a stronger 1100/2110/1300 outage; only the normal explicit restoration path can restore that state.

A fresh callback clears the adapter's waiting state. The controller clears its invalidation only after consuming that new event sequence; merely rereading populated bid, ask, or last fields cannot recover the feed.

## Trade history layout

### Summary tab

The lower detail area now uses three Field/Value pairs per row: six columns and four rows for the current eleven summary fields. Label columns size to their contents, value columns share the remaining width, and both table scrollbars are disabled. The graph receives the remaining vertical space.

### Timeline tab

The timeline graph is vertically expandable and no longer capped at 390 pixels. The two lower tables are fixed to four visible rows each; additional records remain accessible through their vertical scrollbars.

## Release naming

The Windows build script produces:

```text
release\IBKRTradingBot_3.0.12_Windows\
release\IBKRTradingBot_3.0.12_Windows.zip
```

This follows the corresponding `IBKRMarketReplayLab_<version>_Windows.zip` naming pattern.

## Regression coverage

Automated tests cover:

- command-bar disablement surviving the subsequent input-lock refresh;
- code 10197 invalidation and recovery only after a new ticker event;
- 2103/2104 farm-state invalidation;
- preservation of a stronger full-upstream outage when market-data-only messages arrive afterward;
- time-based conversion of a previously green snapshot to stale;
- controller market-data-only outage state;
- the six-column Summary table and expanding graph;
- the four-visible-row Timeline tables and expanding graph.
