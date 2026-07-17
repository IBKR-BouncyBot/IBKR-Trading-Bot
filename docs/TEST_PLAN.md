# Manual test plan

This checklist complements automated tests. Record the application version, Windows version, Python/build type, TWS/Gateway version, account mode, API port/client ID, and UTC timestamps for each run.

Do not perform live-account order tests unless the financial consequences are explicitly accepted. The paper environment is suitable for verifying the workflow, but paper fills are not representative of all live execution behavior.

## 1. Clean source launch

- Extract/clone into a new writable folder.
- Confirm no `.venv` exists.
- Run `run_dev.bat` without administrator elevation.
- Verify `.venv` creation and dependency installation.
- Verify the visible GUI uses the normal Windows platform and readable light palette.
- Close normally and confirm the batch file returns the application exit code.

## 2. Single-instance protection

- Launch one instance.
- Attempt a second launch from the same folder.
- Verify the second instance is rejected without disturbing the first.
- Force-close a test instance, then confirm a stale lock is safely recovered only after the process is gone.

## 3. Connection profiles

For Gateway paper/live and TWS paper/live as available:

- verify profile host/port/mode values;
- connect with a unique client ID;
- confirm the status wraps long errors;
- confirm connected managed account display;
- leave Account blank and verify it is shown as IBKR default behavior;
- enter an invalid explicit live account and verify BUY preflight blocks;
- enter a reported managed account and verify explicit routing is accepted.

## 4. Contract search and qualification

- Search a unique US stock symbol.
- Search an ambiguous symbol and select the intended primary exchange/conId.
- Confirm the price and inspect bid, ask, last/market source, previous close, minimum tick, data type, and RTH status.
- Disconnect/reconnect and verify actual-update timestamps/counters resume with a fresh subscription; cached fields alone must not clear data-pending state.

## 5. Workflow lock

- Engage the top lock.
- Verify all editable connection/strategy inputs are disabled.
- Verify all five workflow buttons are not clickable.
- Verify tab navigation, history, flowchart, monitoring, and Reconciliation remain viewable.
- Unlock and verify each workflow button returns to its state-dependent enablement rather than all becoming blindly enabled.
- In Simple, Advanced, and Debug modes, verify the duplicate Controls group is absent and Recovery / audit log spans the dashboard width.

## 6. Trading blockers

Induce or configure each practical blocker and verify the Trading label/tooltip:

- disconnected local API state;
- local Gateway socket alive but upstream IBKR link unavailable;
- post-reconnect reconciliation and post-recovery fresh-event wait;
- missing/stale/cached-only selected price;
- missing/stale bid/ask;
- stale/unknown/closed RTH;
- delayed data in live mode;
- first/last-minute window where testable;
- on a paper-account early-close or controlled contract-hours fixture, verify the last-minute BUY block and pre-close BUY cancellation use the IBKR-reported close rather than 16:00;
- spread, gap, minimum-price, volatility, daily-loss, cycle, and loss-streak limits;
- failed what-if response;
- unsold app-owned quantity;
- recovery-required state.

Verify routine configured pauses are caution/yellow and not presented as broker/local inconsistency. While one is active, verify Reconciliation disables Reconcile and resume, Stop, Cancel, Sell, Leave-working, and Mark-handled actions while Refresh from IBKR/TWS and audit export remain enabled. Verify red is used for an actual reconciliation/manual-review condition.

Set Maximum spread to a distinctive value, change bid/ask repeatedly, and verify the configured field never changes even though the Trading blocker can switch on/off as the live spread crosses that fixed threshold. Restart and verify only the persisted user value is restored.

## 7. ATR warmup

With ATR mode and warmup blocking enabled:

- start during RTH with an empty observation buffer;
- verify Stage 1 shows warmup and `drop_trigger_price` is absent;
- feed/observe a price below the prior reference and verify no manual-drop BUY occurs;
- wait for observations in `period + 1` distinct bar buckets (the newest bucket may still be forming);
- verify readiness establishes a fresh anchor and does not submit a BUY on the readiness update;
- verify a later ATR-derived drop can initiate entry.

Repeat with warmup blocking disabled and verify currently configured percentages can drive Stage 1 before readiness.

Turn ATR adaptation off during open RTH and verify the observed bar count/readiness continues to advance while Initial drop, BUY rebound, Minimum profit, SELL trail, and protective settings are not rewritten. Restart the application and verify the in-memory ATR count begins empty again.

## 8. BUY order paths

In paper mode with controlled settings:

- positive BUY trail: verify action, type `TRAIL`, trailing percent, stop, quantity, `GTC`, `outsideRth=False`, app order reference, and optional account behavior in TWS/Gateway;
- zero BUY trail: verify the drop condition produces a market BUY;
- slippage buffer: verify quantity is lower/equal compared with unbuffered sizing while the transmitted order type is unchanged;
- partial fill: verify the remainder cancellation request and transition using the filled quantity;
- rejection: verify the application does not remain in a fictional active-order stage.

## 9. External and application-owned positions

- Hold shares of the same ticker acquired outside the application.
- Confirm a new app BUY is not blocked solely by that account-wide position.
- Complete an app BUY without an app SELL and verify a second app BUY is blocked by local unsold quantity.
- Resolve the app quantity outside the application, refresh Reconciliation, and mark manually handled.
- Verify the manually handled cycle no longer blocks entry.

Document the account-position implications; do not assume broker lots are segregated.

## 10. Protective SELL

- Enable protective SELL and fill a BUY.
- Verify one protective app SELL is submitted for the filled quantity.
- Trigger a protective fill and verify local remaining quantity/P&L state.
- In another run, reach minimum-profit eligibility before protective fill.
- Verify cancellation is requested and the final SELL is not submitted until the protective order is confirmed nonworking.

## 11. Final SELL paths

- Positive final trail: verify Stage 3 waits for the calculated required price, then submits native SELL `TRAIL` with a stop that protects the configured gross minimum at submission.
- Zero final trail: verify a market SELL occurs at the threshold.
- Observe a gap/poor paper fill and verify the UI does not claim guaranteed profit.
- Verify completed cycle metrics and history details.

## 12. Stop choices

Exercise each option in a safe paper scenario:

- cancel open app orders;
- market-sell local app quantity after cancellation confirmation;
- leave orders working;
- stop after current cycle;
- stop immediately without broker action.

Close the window with and without an active cycle and verify it uses the same stop decision path.

With hard limits enabled, set Maximum completed cycles to 1, complete one BUY/SELL round, and verify auto-repeat stops. Confirm Stop and window-close do not claim an active order or offer market SELL when the persisted app-owned quantity is zero, even if unrelated external shares of the same ticker exist.

## 13. Recovery scenarios

For each, export an audit bundle before final resolution:

- restart while Stage 1/3 is waiting;
- restart with an open app BUY trail;
- restart after BUY fill but before local fill processing;
- restart with a working protective SELL;
- restart with a final SELL working;
- disconnect during cancellation;
- let a stored active cycle become stale;
- create a deliberate manual order/position mismatch.

Verify the application reattaches/imports only when facts are clear and enters recovery/manual review when they are not. Capture a broker probe while an app SELL is working, then process a newer terminal fill poll and verify the old probe row is retired. Perform a later explicit refresh that still reports a working order and verify it remains visible as a real inconsistency.

### Upstream-only Internet outage

In paper mode, induce or simulate a Gateway/TWS upstream outage while keeping the local API socket connected:

- verify the Connection indicator changes to **Gateway only** and code 1100/2110 appears in diagnostics;
- verify waiting stages do not advance, actual-update age increases, and repeated cached fields do not increase the update count or ATR bar history;
- verify app-order polling and every new BUY/SELL submission path remain paused;
- for 1101 restoration, verify a new market-data subscription identity is created;
- for 1102 restoration, verify the existing subscription remains but cached data stays invalid until a new event;
- verify **Reconciling** precedes normal processing and app-owned fills/orders that changed during the outage are imported;
- verify a BUY fill during the outage is not assumed absent and any required protective-order follow-up occurs only after recovery.

## 14. Database and export

- Verify `bot_state.sqlite` and expected generated folders appear beside the app.
- Run through multiple fills and confirm backups are created and `latest_restore_validation.json` reports success.
- Open a backup read-only with SQLite tooling after shutdown and run `PRAGMA integrity_check`.
- Export trade history and inspect columns/UTC timestamps.
- Export an audit bundle and verify manifest, snapshot, database backup, reports, and JSON table exports.
- Confirm sensitive identifiers are present before sharing externally.

## 15. Market-data capture

- Produce a fill and keep the application running through the post-fill window.
- Verify the capture ZIP is written only after completion and contains expected metadata/rows.
- In a separate test, close before completion and verify no partial ZIP is written.

## 16. Full validation and build

- Run `run_all_tests.bat`; require compilation, pytest with `ResourceWarning` failures enabled, at least 75% combined statement/branch coverage, entry coverage for every effective executable application callable, all CSV simulations, Ruff, and Pyright to pass.
- Inspect `run_tests_coverage.log` and `run_tests_callable_coverage.log`; do not rely only on the final pass line.
- Preserve `coverage.json` or `coverage.xml` as a release/CI artifact when traceable machine-readable coverage evidence is required.
- Run `build_windows.bat`; verify the final output is not falsely red on success.
- Confirm `build_pyinstaller.log` ends with a successful build and the onedir executable exists.
- Run the packaged application from a clean folder with the complete onedir contents.
- Verify data is created beside the executable and the folder is writable.

## 17. Documentation consistency

Before a release:

- compare README/default tables with `ConnectionSettings` and `StrategySettings`;
- compare strategy formulas with `app/models.py` and `app/strategy.py`;
- compare schema documentation with `_ensure_schema()`;
- compare build/test instructions with current scripts;
- ensure the `docs/` root contains only current material and superseded notes are indexed under `docs/legacy/`;
- verify all relative links across `README.md`, `SECURITY.md`, `CHANGELOG.md`, and `docs/**/*.md`;
- confirm `LICENSE` exactly matches the selected published license text and is referenced from current documentation;
- inspect the staged file list for databases, audit bundles, reports, captures, credentials, keys, personal paths, and generated test/build output;
- confirm no documentation claims guaranteed execution or profit.
