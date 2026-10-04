# Limitations and non-goals

This document states the boundaries of v5.6.0. Treat each limitation as an operational constraint, not as a future guarantee.

## Strategy scope

- One active strategy cycle is supported at a time.
- The strategy is long-only: BUY whole shares, then SELL the application-owned quantity.
- Supported strategy contracts are exact API-selected ordinary `STK` listings in USD or EUR, routed through `SMART`, with a positive conId and usable IBKR capability/session metadata.
- The GUI does not implement short selling, options, futures, forex, bonds, crypto, fractional shares, bracket portfolios, pairs, or multi-leg orders.
- It is not a portfolio optimizer, scanner, signal marketplace, or backtesting platform.

## Execution limits

- Native IBKR trailing orders trigger market-style execution. The displayed stop is not a guaranteed fill price.
- The Stage-2 partial-BUY grace, introduced in v3.8.0, is fixed at 3.0 seconds. It is not a persisted setting and cannot guarantee that a cancellation reaches IBKR before additional or complete fills occur.
- A configured minimum-profit percentage is a pre-submission stop-level condition. It does not guarantee net profit after slippage, gaps, partial fills, commissions, fees, or broker adjustments.
- The optional slippage buffer changes planning math only. It is not a limit order and does not cap slippage.
- The protective SELL is placed only after the BUY is terminal with a positive filled quantity; nonterminal partial fills remain under remainder supervision first. It cannot guarantee protection during gaps, market closures, halts, disconnections, rejection, or insufficient liquidity.
- The application cannot override IBKR risk checks, exchange rules, account restrictions, or order simulations.
- The optional Stage-3/Stage-4 close-before-RTH policy is not guaranteed to finish before the close. Cancellation acknowledgement, partial fills, order rejection, halts, connectivity, and limited remaining time can leave shares unsold and require manual review. Its market replacement can realize a loss.
- In Stage 3, the close-before-RTH policy acts only from a complete, independently fresh, non-crossed quote whose spread is within the configured maximum and whose executable bid is strictly above the weighted average BUY price, ignoring commissions. The market fill can still be below that bid or realize a loss. The policy does not create an extended-hours or overnight protective order.

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
- The live adapter tracks update and numerical-change identity separately for bid, ask, Last, close, mark, and the selected-price basis. A fresh callback can still contain unchanged cached fields, but those fields do not become fresh merely because another field, a size, or a timestamp updated. These are application callback timestamps, not guaranteed exchange-origin timestamps.
- ATR uses prices observed while this application is running and RTH is open, and only when the raw field or quote basis underlying the selected price updated in that event. An unchanged cached Last exposed by another ticker update is excluded. Observation/bar collection continues when adaptation is disabled, but the buffer is not persisted. It resets on restart, is not exchange-native historical ATR, and does not warm up while the application is closed.
- The recent-volatility filter uses the application’s observed sample range, not a broker historical-volatility product.
- Normal RTH and session-timing guards use date-specific IBKR ContractDetails `liquidHours` and `timeZoneId`, including early closes; the schedule is not inferred from price ticks. `LSE` and `LSEETF` additionally use a verified 08:00-16:30 `Europe/London` continuous-session cap. The cap is not an independently maintained LSE holiday or special early-close calendar: an IBKR late open, earlier close, or closed day remains authoritative. This release does not provide an independent continuous-session calendar for every SMART-routable venue. BouncyBot does not guess a weekday schedule or substitute a timezone when the contract timezone is missing or invalid. Missing/unusable authoritative session metadata blocks RTH-restricted submissions for every contract. Without a usable authoritative window, the GUI has no session hours or countdown to display. A published schedule does not establish whether an instrument is currently halted.

## Contract, route, currency, and quantity limits

- v5.0.0 supports only USD and EUR ordinary `STK` contracts selected from an exact IBKR API result. Other currencies and security types remain unsupported.
- Order routing is `SMART` only. The primary exchange identifies the selected native listing; direct-routing workflows are not implemented.
- “SMART supported” is capability-driven, not a guarantee for every listing or venue. BouncyBot requires nonempty broker route/order-type metadata advertising SMART, `MKT`, and `TRAIL`, plus usable regular-session metadata. When a market rule is advertised it must be resolved; otherwise contract `minTick` is used. Missing size metadata uses the one-share default. Missing required route/order-type metadata or an incompatible capability blocks qualification.
- Each portable SQLite database is single-currency. A zero-cycle draft can switch between USD and EUR, but the first persisted cycle locks the database. Mixed USD/EUR history and automatic FX conversion are not supported.
- Quantity handling is whole-share only. BUY quantity may round down to a compatible `minSize`/`sizeIncrement`; SELL quantity is not rounded down because that could leave an untracked remainder. Broker lot-size metadata can still be incomplete or change.
- A commission reported in another currency is preserved for audit but excluded from local net P/L, and Auto-repeat is disabled. This is not a substitute for statement-level FX accounting.

## Broker validation and order-price limits

- IBKR market rules are broker-provided session facts. BouncyBot can normalize to the rule returned for the selected route and proposed price, but it cannot guarantee that a later order will be accepted after market, exchange, account, or broker-control changes.
- When a market rule is advertised but unavailable or ambiguous, the application blocks submission rather than guessing. This can prevent an otherwise acceptable order until the broker metadata becomes available.
- The what-if request is a broker preflight, not a reservation of buying power, price, route, or permission. A successful result does not guarantee live acceptance or execution.
- Error callbacks can arrive before, during, or after order-status callbacks. The adapter retains a bounded short-lived race cache, but a process/network failure can still prevent some diagnostics from reaching local SQLite. Gateway/TWS logs remain an important external source.
- Audit-condition coalescing is an in-memory diagnostic facility. It deliberately suppresses duplicate SQLite rows, not guard evaluation. A process restart resets the coalescer, so a still-active condition can create a new entry event. Summary cadence is not a guarantee that every intermediate age, retry count, or price will be persisted; the GUI and structured summary context carry the current/latest evidence.
- `Inactive` is treated as a structural no-fill failure for an app-owned BUY and stops the cycle. An unusual broker workflow that uses `Inactive` for a benign condition therefore requires manual review rather than automatic continuation.

## Availability and recovery limits

- This is a desktop process, not a redundant service. Application-side monitoring stops when Windows, Python, the executable, TWS/Gateway, the network, or the API session stops.
- IBKR-native orders already accepted by the broker may continue to work after the application closes or loses connectivity; application-side Stage 1/3 observation does not.
- A BUY can fill while callbacks are unavailable. Application-side follow-up, including protective SELL placement that did not already exist, is delayed until connectivity and reconciliation return.
- Handling 1100/1101/1102 and retrying a lost local API connection every 10 seconds reduces stale-data and unattended-disconnect risk but does not provide redundant Internet, Gateway, machine, process, credential, or session failover.
- The built-in worker watchdog runs in the Qt GUI thread. It can detect a dead or blocked controller worker while the GUI event loop remains responsive and can replace that complete process. It cannot act when the Qt main event loop, entire process, Windows session, storage device, or machine is itself frozen or unavailable.
- Automatic replacement is best-effort and local. It depends on the executable/source tree, portable directory, restart-history file, Python/Windows process creation, and database being usable enough for the replacement to start. A constructor/startup failure before the GUI watchdog starts has no in-process supervisor.
- Automatic continuation is deliberately narrow: exact persisted cycle facts and normal IBKR reconciliation must succeed. The watchdog cannot guarantee continuity through missing broker history, changed manual orders/positions, corrupt storage, or a Gateway login/2FA failure.
- Replacement attempts are rate-limited. After three rapid attempts in 15 minutes, the current fail-closed process waits five minutes between further attempts; persistent faults can therefore require operator intervention.
- Ordinary startup recovery is conservative and requires explicit operator action for a stored active cycle. The only automatic exception is a short-lived authenticated watchdog replacement of an already monitored exact cycle, and it still uses the normal broker reconciliation gates.
- Orderly Windows update/sign-out/shutdown requests receive a final resume checkpoint, but a sudden power cut, forced process kill, operating-system crash, or storage failure cannot execute that hook. Only state already committed to SQLite is recoverable in those cases.
- Broker responses and recent execution windows may be incomplete; ambiguous states are moved to manual review rather than guessed. A cached recovery probe is only a point-in-time view. Newer normal order polls can supersede its matching rows, but any later explicit probe that still reports an order must be investigated.
- The single-instance lock protects one portable folder. It cannot prevent a separately copied folder, different database, or different client ID from running elsewhere.
- Incomplete RAM-only market captures are lost on shutdown by design.

## Accounting and compliance limits

- P/L is based on recorded application fills and commissions that IBKR reports to this client. It is not a complete account statement.
- The application does not calculate tax, regulatory reporting, wash sales, FX conversion, corporate actions, dividends, financing, borrow fees, or portfolio margin.
- Daily and historical guard calculations are local SQLite calculations and do not replace broker account-level risk limits. Daily P/L uses the stable UTC `completed_at` date; existing completed rows receive a one-time legacy backfill from `updated_at`. Later commission updates do not move a cycle to another day. The consecutive-loss query examines at most the latest 100 completed cycles for the selected ticker/conId, including legacy same-ticker rows without a conId.
- The project does not provide legal, tax, or investment advice.

## Platform limits

- Windows is the supported GUI and packaging target.
- standard GIL-enabled CPython 3.14.x is required when running from source.
- TWS or IB Gateway must be installed, logged in, authenticated, and configured for API access.
- The application retries a lost local API socket every 10 seconds indefinitely, but it does not automate credentials, two-factor authentication, platform login, Gateway/TWS startup after a complete platform exit, operating-system restart/login, or daily IBKR maintenance windows. Manual Disconnect and application shutdown stop the retries.

## Security and distribution limits

- The application stores configuration and trading audit data locally without application-level database encryption.
- Audit bundles can contain account identifiers, contract details, order references, fills, strategy settings, timestamps, usernames, and local paths. Treat them as sensitive and follow [`../SECURITY.md`](../SECURITY.md).
- The project is licensed under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted only under those terms; commercial use is not granted.
- The project license does not replace the separate licenses of PySide6, `ib_async`, Python packages, TWS/IB Gateway, or other third-party components.
- Public source availability does not imply operational support, suitability for live trading, regulatory approval, or a warranty.


## Multi-instance ownership boundary

Multiple BouncyBot copies can share a Master API feed. v5.0.0 rejects attribution of any order or callback whose complete `OrderRef` is not already persisted locally. This prevents one installation from acting on another installation's app-prefixed order, but it also means a lost or replaced local database can require manual recovery instead of broad prefix-based discovery. Cancellation additionally requires exact current API-client ownership; using another client ID does not grant cancellation authority over the original client’s order.

## Prior-session volatility estimates

A validated ready ATR estimate is checkpointed in the existing SQLite `app_settings` table for the exact confirmed contract, currency, venue, trading/data profile, ATR period, and bar duration. At a verified open RTH session, that estimate can supply the starting ATR/ATR% while current-session bars warm up. It does not insert synthetic observations or make a quote fresh. The first ready current-session calculation replaces it. The GUI identifies the saved session and today's observed bar count.

The seed must be no more than 24 elapsed UTC hours old (the exact 24-hour boundary is accepted) and must contain finite positive, internally consistent values observed inside its recorded RTH window. A next-session or same-session estimate is eligible only within that age limit; weekend and longer holiday gaps require normal warmup. The application does not guess exchange holiday calendars. First use, an expired/corrupt/mismatched checkpoint, or a changed ATR period/bar duration without a matching checkpoint retains normal warmup. A database from before checkpoint support also needs normal warmup until a valid estimate has been saved. A same-session application/watchdog restart can also reuse a valid checkpoint. Current market-data, opening-delay, RTH, gap, spread, two-observation SELL, and broker-reconciliation guards still apply. A saved estimate is not evidence that today's volatility is unchanged.

ATR memory does not detect every corporate action or guarantee suitability of the previous estimate after an overnight event. Retain gap and quote guards and inspect the saved/current source indicator. Only observed sessions can be reused; no historical data request is added.

## v5.0.0 validation limits

The source/build target is standard CPython 3.14.x. The implementation host had Python 3.12 only and its normal 3.14 download failed with HTTP 403. Offline regressions and limited fallback test execution do not certify native 3.14, Windows Qt/DPI, PyInstaller output, actual IBKR behavior or the full pytest/coverage/quality gates. Use the implementation report and qualify those gates before live deployment. A manual-review pause retains uncertainty; it does not guarantee continuous protection or eliminate exchange/broker execution risk.

## v5.1.0 qualification and historical data

The development host could not obtain Python 3.14 or the declared test/build dependencies (HTTP 403). The current verification report distinguishes offline CPython 3.12 and limited-runner checks from unperformed Windows, real Qt/DPI, pytest/coverage, Ruff, Pyright, PyInstaller and IBKR checks.

The additive completion-date migration freezes the existing recorded update date for old completed rows. It cannot reconstruct historical dates already moved by older callbacks. Separate cancellation and replacement still cannot guarantee an uninterrupted broker exit order.
