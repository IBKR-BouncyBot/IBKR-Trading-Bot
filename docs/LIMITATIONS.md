# Limitations and non-goals

This document states the boundaries of v3.2.0. Treat each limitation as an operational constraint, not as a future guarantee.

## Strategy scope

- One active strategy cycle is supported at a time.
- The strategy is long-only: BUY whole shares, then SELL the application-owned quantity.
- Supported strategy contracts are exact API-selected ordinary `STK` listings in USD or EUR, routed through `SMART`, with a positive conId and usable IBKR capability/session metadata.
- The GUI does not implement short selling, options, futures, forex, bonds, crypto, fractional shares, bracket portfolios, pairs, or multi-leg orders.
- It is not a portfolio optimizer, scanner, signal marketplace, or backtesting platform.

## Execution limits

- Native IBKR trailing orders trigger market-style execution. The displayed stop is not a guaranteed fill price.
- A configured minimum-profit percentage is a pre-submission stop-level condition. It does not guarantee net profit after slippage, gaps, partial fills, commissions, fees, or broker adjustments.
- The optional slippage buffer changes planning math only. It is not a limit order and does not cap slippage.
- The protective SELL cannot guarantee protection during gaps, market closures, halts, disconnections, rejection, or insufficient liquidity.
- The application cannot override IBKR risk checks, exchange rules, account restrictions, or order simulations.
- The optional Stage-3/Stage-4 close-before-RTH policy is not guaranteed to finish before the close. Cancellation acknowledgement, partial fills, order rejection, halts, connectivity, and limited remaining time can leave shares unsold and require manual review. Its market replacement can realize a loss.
- In Stage 3, the close-before-RTH policy acts only while a fresh selected price is strictly above the weighted average BUY price, ignoring commissions. The market fill can still be below that reference or realize a loss. The policy does not create an extended-hours or overnight protective order.

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
- Normal RTH and session-timing guards use date-specific IBKR contract `liquidHours`, including early closes. The 09:30–16:00 New York fallback is permitted only for recognized U.S. primary exchanges and may not represent holidays, halts, or special sessions perfectly. Non-U.S. or unknown contracts with missing/unusable session metadata fail closed.

## Contract, route, currency, and quantity limits

- v3.2.0 supports only USD and EUR ordinary `STK` contracts selected from an exact IBKR API result. Other currencies and security types remain unsupported.
- Order routing is `SMART` only. The primary exchange identifies the selected native listing; direct-routing workflows are not implemented.
- “SMART supported” is capability-driven, not a guarantee for every listing or venue. BouncyBot requires the selected contract to advertise or accept SMART, `MKT`, `TRAIL`, market-rule pricing, whole-share quantity rules, and usable regular-session metadata. A missing capability blocks the contract.
- Each portable SQLite database is single-currency. A zero-cycle draft can switch between USD and EUR, but the first persisted cycle locks the database. Mixed USD/EUR history and automatic FX conversion are not supported.
- Quantity handling is whole-share only. BUY quantity may round down to a compatible `minSize`/`sizeIncrement`; SELL quantity is not rounded down because that could leave an untracked remainder. Broker lot-size metadata can still be incomplete or change.
- A commission reported in another currency is preserved for audit but excluded from local net P/L, and Auto-repeat is disabled. This is not a substitute for statement-level FX accounting.

## Broker validation and order-price limits

- IBKR market rules are broker-provided session facts. BouncyBot can normalize to the rule returned for the selected route and proposed price, but it cannot guarantee that a later order will be accepted after market, exchange, account, or broker-control changes.
- When a market rule is advertised but unavailable or ambiguous, the application blocks submission rather than guessing. This can prevent an otherwise acceptable order until the broker metadata becomes available.
- The what-if request is a broker preflight, not a reservation of buying power, price, route, or permission. A successful result does not guarantee live acceptance or execution.
- Error callbacks can arrive before, during, or after order-status callbacks. The adapter retains a bounded short-lived race cache, but a process/network failure can still prevent some diagnostics from reaching local SQLite. Gateway/TWS logs remain an important external source.
- `Inactive` is treated as a structural no-fill failure for an app-owned BUY and stops the cycle. An unusual broker workflow that uses `Inactive` for a benign condition therefore requires manual review rather than automatic continuation.

## Availability and recovery limits

- This is a desktop process, not a redundant service. Application-side monitoring stops when Windows, Python, the executable, TWS/Gateway, the network, or the API session stops.
- IBKR-native orders already accepted by the broker may continue to work after the application closes or loses connectivity; application-side Stage 1/3 observation does not.
- A BUY can fill while callbacks are unavailable. Application-side follow-up, including protective SELL placement that did not already exist, is delayed until connectivity and reconciliation return.
- Handling 1100/1101/1102 and retrying a lost local API connection every 10 seconds reduces stale-data and unattended-disconnect risk but does not provide redundant Internet, Gateway, machine, process, credential, or session failover.
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
- The application retries a lost local API socket every 10 seconds indefinitely, but it does not automate credentials, two-factor authentication, platform login, session restarts, or daily IBKR maintenance windows. Manual Disconnect and application shutdown stop the retries.

## Security and distribution limits

- The application stores configuration and trading audit data locally without application-level database encryption.
- Audit bundles can contain account identifiers, contract details, order references, fills, strategy settings, timestamps, usernames, and local paths. Treat them as sensitive and follow [`../SECURITY.md`](../SECURITY.md).
- The project is licensed under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted only under those terms; commercial use is not granted.
- The project license does not replace the separate licenses of PySide6, `ib_async`, Python packages, TWS/IB Gateway, or other third-party components.
- Public source availability does not imply operational support, suitability for live trading, regulatory approval, or a warranty.


## Multi-instance ownership boundary

Multiple BouncyBot copies can share a Master API feed. v3.2.0 rejects attribution of any order or callback whose complete `OrderRef` is not already persisted locally. This prevents one installation from acting on another installation's app-prefixed order, but it also means a lost or replaced local database can require manual recovery instead of broad prefix-based discovery.
