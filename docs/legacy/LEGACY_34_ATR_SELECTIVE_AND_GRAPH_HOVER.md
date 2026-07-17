# legacy release selective ATR minimum-profit mode and live graph hover

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Changes:

- ATR adaptive mode can now leave **Minimum profit %** manual while adapting Initial drop %, BUY rebound/trail %, and SELL trailing-stop %.
- The live market-data monitor continues updating after Stop/cancel/manual-review/stopped states as long as the app remains connected and a contract is confirmed.
- A terminal no-fill BUY order no longer enters manual-review mode. Because no position was opened, the app marks the cycle stopped and keeps market data visible.
- Terminal no-fill protective/final SELL states pause in ERROR rather than the legacy release automatic Manual Review transition, while market data remains visible.
- The Market and strategy graph displays the nearest observed time and price while hovering over the plotted price line area, both as a tooltip and as an in-chart marker.

Notes:

- ATR still does not modify a native IBKR order that has already been submitted.
- The selectable Minimum profit % ATR option only changes future app-side calculations before the final SELL trailing-stop is placed.
