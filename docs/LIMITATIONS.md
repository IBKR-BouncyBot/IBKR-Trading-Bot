# Limitations and non-goals

This document states the boundaries of v3.0.19. Treat each limitation as an operational constraint, not as a future guarantee.

## Strategy scope

- One active strategy cycle is supported at a time.
- The strategy is long-only: BUY whole shares, then SELL the application-owned quantity.
- Supported strategy contracts are `STK`, `USD`, and `SMART` routed.
- The GUI does not implement short selling, options, futures, forex, bonds, crypto, fractional shares, bracket portfolios, pairs, or multi-leg orders.
- It is not a portfolio optimizer, scanner, signal marketplace, or backtesting platform.

## Execution limits

- Native IBKR trailing orders trigger market-style execution. The displayed stop is not a guaranteed fill price.
- A configured minimum-profit percentage is a pre-submission stop-level condition. It does not guarantee net profit after slippage, gaps, partial fills, commissions, fees, or broker adjustments.
- The optional slippage buffer changes planning math only. It is not a limit order and does not cap slippage.
- The protective SELL cannot guarantee protection during gaps, market closures, halts, disconnections, rejection, or insufficient liquidity.
- The application cannot override IBKR risk checks, exchange rules, account restrictions, or order simulations.

## Position ownership limits

The controller deliberately ignores account-wide external long positions when deciding whether a new application BUY is allowed. It blocks based on unsold BUY fills recorded by this application.

This has consequences:

- IBKR does not tag individual shares by originating application.
- Manual and application-created shares can be commingled in the same account position.
- A manual SELL can reduce the broker position without updating the application ledger.
- The application may believe its recorded quantity still exists until the operator reconciles or marks the cycle handled. Stop, exit, and Recovery intentionally trust this persisted app ledger rather than inferring ownership from the account-wide broker position.
- Tax-lot selection, average-cost effects, and account reporting remain broker/account concerns.

Use separate accounts or deliberate operating procedures when strict position segregation is required.

## Market-data limits

- Price selection depends on fields supplied by TWS/Gateway and the account’s subscriptions.
- Delayed, frozen, stale, absent, crossed, or illiquid quotes can block BUYs or make diagnostics less representative.
- A `Ticker` object can retain old non-null fields after delivery stops. The GUI may display those fields as cached diagnostics, but they do not count as fresh data. Actual event delivery is required.
- Freshness is tracked at the ticker-update event level, not as an exchange timestamp for every individual field; a fresh callback can contain an unchanged value.
- ATR uses prices observed while this application is running and RTH is open. Observation/bar collection continues when adaptation is disabled, but the buffer is not persisted. It resets on restart, is not exchange-native historical ATR, and does not warm up while the application is closed.
- The recent-volatility filter uses the application’s observed sample range, not a broker historical-volatility product.
- Normal RTH and session-timing guards use date-specific IBKR contract `liquidHours`, including early closes. The fallback used when IBKR contract hours are unavailable is designed for US equities and may not represent holidays, halts, or special sessions perfectly.

## Availability and recovery limits

- This is a desktop process, not a redundant service. Application-side monitoring stops when Windows, Python, the executable, TWS/Gateway, the network, or the API session stops.
- IBKR-native orders already accepted by the broker may continue to work after the application closes or loses connectivity; application-side Stage 1/3 observation does not.
- A BUY can fill while callbacks are unavailable. Application-side follow-up, including protective SELL placement that did not already exist, is delayed until connectivity and reconciliation return.
- Handling 1100/1101/1102 reduces stale-data risk but does not provide redundant Internet, Gateway, machine, or process failover.
- Startup recovery is conservative and requires explicit operator action for a stored active cycle.
- Orderly Windows update/sign-out/shutdown requests receive a final resume checkpoint, but a sudden power cut, forced process kill, operating-system crash, or storage failure cannot execute that hook. Only state already committed to SQLite is recoverable in those cases.
- Broker responses and recent execution windows may be incomplete; ambiguous states are moved to manual review rather than guessed. A cached recovery probe is only a point-in-time view. Newer normal order polls can supersede its matching rows, but any later explicit probe that still reports an order must be investigated.
- The single-instance lock protects one portable folder. It cannot prevent a separately copied folder, different database, or different client ID from running elsewhere.
- Incomplete RAM-only market captures are lost on shutdown by design.

## Accounting and compliance limits

- P/L is based on recorded application fills and commissions that IBKR reports to this client. It is not a complete account statement.
- The application does not calculate tax, regulatory reporting, wash sales, FX conversion, corporate actions, dividends, financing, borrow fees, or portfolio margin.
- Daily and historical guard calculations are local SQLite calculations and do not replace broker account-level risk limits.
- The project does not provide legal, tax, or investment advice.

## Platform limits

- Windows is the supported GUI and packaging target.
- Python 3.11 or newer is required when running from source.
- TWS or IB Gateway must be installed, logged in, authenticated, and configured for API access.
- The application does not automate credentials, two-factor authentication, session restarts, or daily IBKR maintenance windows.

## Security and distribution limits

- The application stores configuration and trading audit data locally without application-level database encryption.
- Audit bundles can contain account identifiers, contract details, order references, fills, strategy settings, timestamps, usernames, and local paths. Treat them as sensitive and follow [`../SECURITY.md`](../SECURITY.md).
- The project is licensed under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted only under those terms; commercial use is not granted.
- The project license does not replace the separate licenses of PySide6, `ib_async`, Python packages, TWS/IB Gateway, or other third-party components.
- Public source availability does not imply operational support, suitability for live trading, regulatory approval, or a warranty.
