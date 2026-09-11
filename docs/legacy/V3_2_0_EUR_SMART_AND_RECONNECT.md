# v3.2.0 EUR SMART contracts and fixed reconnect cadence

**Release:** v3.2.0
**Baseline:** corrected v3.1.2 source
**Database migration:** no table/column change; one additive `app_settings` currency-lock key

## Purpose

v3.2.0 extends BouncyBot from USD-only stock contracts to ordinary USD and EUR `STK` contracts routed through `SMART`. Live operation now requires selecting an exact IBKR API contract result with a positive `conId`, and each portable SQLite database is restricted to one contract currency so P/L, risk limits, budgets, and reinvestment are never added across currencies without an FX conversion model.

The release also changes local API reconnect behavior to a fixed ten-second retry cadence with no attempt limit. A manual Disconnect or application shutdown remains authoritative and stops retrying.

## Same-release bugscan correction

The final v3.2.0 source includes a same-version maintenance correction found during a focused review of the new exact-contract, currency-lock, commission, market-data fallback, and reconnect paths:

- exact qualification now rejects a broker-returned symbol, order route, or nonblank primary exchange that differs from the selected API result;
- the selected primary exchange remains attached to the qualified contract when IBKR leaves the returned field blank;
- market-data fallback variants use the qualified contract currency, preventing a blank raw contract currency from producing a USD request for an EUR contract;
- atomic shutdown/resume checkpoints claim or validate the contract currency in the same SQLite transaction as the settings and active cycle;
- a failed reconnect recorded at monotonic time zero still waits the full ten-second interval before another attempt;
- an exactly zero commission does not trigger a cross-currency mismatch, while non-zero mismatch events are deduplicated from SQLite across process restarts, including earlier v3.2.0 event rows without an explicit dedupe key; and
- the two reported Ruff `I001` import-boundary spacing errors are corrected without changing imported names or runtime behavior.

## Supported contract scope

The supported strategy contract is now:

- security type `STK`;
- order routing exchange `SMART`;
- contract currency `USD` or `EUR`;
- an exact positive IBKR `conId` selected from the API contract-search results;
- a native/primary exchange returned by IBKR for disambiguation;
- broker metadata that advertises SMART routing and the `MKT` and `TRAIL` order types used by BouncyBot.

The selected `conId`, symbol, currency, security type, SMART route, and primary exchange are rechecked during qualification. A mismatch fails closed before strategy monitoring or order submission.
Account-position lookups and local position/risk reconciliation use the exact positive `conId`; when an exact identifier is available, BouncyBot does not fall back to another contract that merely shares the same ticker symbol.

The feature does not add options, funds, CFDs, futures, forex, short selling, direct-exchange routing, or currencies other than USD and EUR.

## One contract currency per portable database

BouncyBot stores a `database_contract_currency` value in the existing `app_settings` table.

- A new database has no currency until an exact contract is selected.
- Before the first cycle is created, the draft selection may be changed between USD and EUR.
- The first persisted cycle locks the database to that cycle's currency.
- Later contract search results in the other currency are shown as unsupported for that database.
- Cycle creation, active-cycle recovery, and settings persistence fail closed on a currency conflict.
- Existing v3.1.2 databases infer the lock from their historical cycle rows. A normal existing USD database therefore remains USD without rewriting its cycle history.
- A database already containing cycles in multiple currencies is treated as inconsistent and requires manual review.

There is no FX conversion and no automatic FX conversion. Investment amount, risk limits, realized P/L, and reinvestment remain denominated in the database's one contract currency.

If IBKR reports a non-zero commission in another currency, BouncyBot preserves the raw broker fact but excludes that commission from locally calculated net P/L, disables Auto-repeat after the current cycle, and records a `COMMISSION_CURRENCY_MISMATCH` decision event. This prevents an unconverted amount from being subtracted as though it were in the contract currency.

## Exact contract selection and broker capability checks

The GUI exposes read-only **Contract currency** and **IBKR conId** fields populated only by the API selector. Editing the ticker invalidates the prior exact selection. Start and price confirmation remain blocked until an exact USD/EUR `STK` result is selected.

The production adapter then verifies:

1. the returned `conId` exactly matches the selected result;
2. the returned symbol, currency, and security type match the selection;
3. order routing remains `SMART`;
4. SMART appears in `ContractDetails.validExchanges` when that field is supplied;
5. `MKT` and `TRAIL` appear in `ContractDetails.orderTypes` when that field is supplied;
6. the applicable market-rule price increment can be resolved when IBKR advertises a market rule;
7. whole-share quantities satisfy the advertised minimum size and size increment.

BUY quantities may be rounded down to a supported whole-share step before intent is recorded. A SELL quantity is never rounded down because that could leave an application-owned remainder untracked; a nonconforming SELL fails closed instead.

## Exchange sessions and RTH safety

Date-specific `liquidHours` and `timeZoneId` from the exact contract drive RTH status and the configured open/close timing guards.

The historical New York 09:30–16:00 fallback is retained only for recognized US equity primary exchanges. A EUR or otherwise non-US contract with missing, invalid, or unparseable session metadata fails closed. BouncyBot does not guess US hours for a European listing.

This supports ordinary EUR stocks on SMART-routable European primary exchanges without claiming that every global venue or every order-type combination is supported. The broker metadata for the exact selected contract remains authoritative.

## Fixed reconnect cadence

After a previously enabled local TWS/IB Gateway API connection is lost:

- the first reconnect attempt may run immediately when loss is detected;
- after a failed attempt, the next attempt occurs no sooner than ten seconds later;
- all later failures continue on the same ten-second interval;
- there is no exponential backoff and no maximum attempt count;
- a successful reconnect resets the failure count and cadence state;
- manual **Disconnect** and application shutdown disable retries.

Launching TWS/IB Gateway through BouncyBot also enables the same indefinite ten-second retry loop while the operator completes login or two-factor authentication.

This local-socket retry is separate from Gateway/TWS-to-IBKR server status. If the local socket is connected but IBKR reports an upstream outage, trading remains paused under the existing upstream reconciliation rules rather than repeatedly tearing down a healthy local socket.

## Compatibility and migration

There is no SQLite schema-column migration in v3.2.0. The currency lock uses the existing `app_settings` key/value table and is created lazily.

Existing v3.1.2, v3.1.1, v3.1.0, and v3.0.19 USD databases remain forward-compatible. As with every upgrade:

1. shut down the old application cleanly;
2. retain a backup of the complete portable folder;
3. copy `bot_state.sqlite` and `debug_captures/` when moving to a new release directory;
4. start v3.2.0 and inspect the displayed database currency and active-cycle recovery state before resuming live trading.

Do not copy one live database to two concurrently running instances. Exact full-`OrderRef` ownership remains database-local, and cloned databases can contain the same persisted references.

## Validation scope

The automated release gate covers USD/EUR validation, exact contract identity, SMART/order-type capability checks, European timezone/session parsing, non-US RTH fail-closed behavior, market-rule and quantity normalization, database currency migration/locking, mismatched-commission handling, GUI contract selection and currency display, active-cycle recovery boundaries, fixed ten-second reconnect attempts, manual disconnect, documentation, and release metadata.

The corrected source passed 971/971 collected pytest cases (966 non-soak and five bounded soak tests), 77.5% combined statement/branch coverage against the 75% gate, entry into all 917/917 executable application callables, 6/6 safety mutation checks, and all 58 deterministic simulation contracts across 54 CSV paths. Eleven focused same-release bugscan regressions cover the corrected boundaries. The two large-database timing tests were included in the complete non-instrumented test collection and run outside coverage instrumentation to keep the release-host run bounded.

A Windows packaged-executable smoke test and representative IBKR paper-account tests remain required before production use. Paper validation should include at least one USD NASDAQ stock and one EUR stock on a European primary exchange, with contract search, qualification, live market data, what-if, BUY, SELL, commission currency, RTH close, disconnect, and reconnect observed in TWS/IB Gateway.
