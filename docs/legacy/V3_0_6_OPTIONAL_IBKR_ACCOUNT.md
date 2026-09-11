# v3.0.6 optional IBKR account routing

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Live trading can now start with a blank Connection settings Account field. In that case, the adapter deliberately leaves the IBKR order account property unset and delegates account selection to the logged-in TWS/IB Gateway session.

A configured account remains an optional explicit routing override. When supplied for live trading, the existing managed-account validation remains active.

The change removes only application-side account requirements. Strategy calculations, stage transitions, order types, and risk thresholds are unchanged.
