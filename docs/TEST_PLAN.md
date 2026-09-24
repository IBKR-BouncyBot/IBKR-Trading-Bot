# Manual test plan

This checklist complements automated tests. Record the application version, Windows version, Python/build type, TWS/Gateway version, account mode, API port/client ID, and UTC timestamps for each run.

Do not perform live-account order tests unless the financial consequences are explicitly accepted. The paper environment is suitable for verifying the workflow, but paper fills are not representative of all live execution behavior.

## 1. Clean source launch

- Extract/clone into a new writable folder.
- Confirm no `.venv` exists and standard GIL-enabled CPython 3.14.x is installed.
- Separately verify that Python 3.14t, another Python branch and an incompatible existing `.venv` are rejected before dependency installation.
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
- leave Account blank and verify a new cycle resolves exactly one managed account; zero or multiple managed accounts must block automatic binding;
- verify the unknown Account placeholder is `N/A` while disconnected; when connected to both Gateway/TWS and IBKR it is `Auto (single managed account)` until a concrete account is available;
- enter an invalid explicit live account and verify BUY preflight blocks;
- enter a reported managed account and verify explicit routing is accepted.

## 4. Contract search and qualification

- Search and select one exact USD ordinary stock result with a positive conId, such as a Nasdaq listing.
- Search and select one exact EUR ordinary stock result on a European primary exchange.
- Search an ambiguous symbol and verify that choosing the intended API result populates read-only currency, primary exchange, and conId while routing remains SMART.
- Manually edit the ticker/primary exchange and verify the exact selection is cleared and Start remains blocked until a result is selected again.
- Verify a GBP, CFD, missing-conId, or non-SMART/incompatible result is unavailable or fails closed.
- Confirm the price and inspect bid, ask, last/market source, previous close, currency, exact conId, contract minimum tick, size rules, data type, and RTH status. At order preflight, inspect the selected route-specific market rule and increment in the order/audit diagnostics.
- For a contract whose `minTick` is smaller than the valid increment at the current price, verify the submitted stop follows the applicable `reqMarketRule` band rather than the smallest contract-level tick.
- For both U.S. and non-U.S. listings, verify the session uses the contract's `liquidHours`/`timeZoneId`, including daylight-saving and early-close behavior where practical. For `LSE`/`LSEETF`, verify the effective close is the earlier of the IBKR close and 16:30 `Europe/London`, and that a cached open status becomes closed at that boundary. Missing or invalid hours/timezone metadata must block RTH-restricted actions without guessed hours or countdowns.
- Force persistent BUY preflight blockers. Verify the cycle records `PreflightBlocked`, no order intent is created, and the order remains blocked on every evaluation. Expected waits must produce one INFO entry and 15-minute summaries; hard risk/data/broker blockers must produce an immediate WARN, a one-minute first WARN summary, five-minute recurring WARN summaries, and one recovery event when the blocker clears.
- Disconnect/reconnect and verify actual-update timestamps/counters resume with a fresh subscription; cached fields alone must not clear data-pending state.

## 5. Workflow lock

- Engage the top lock.
- Verify all editable connection/strategy inputs are disabled.
- Verify all five workflow buttons are not clickable.
- Verify tab navigation, history, flowchart, monitoring, and Reconciliation remain viewable.
- Unlock and verify each workflow button returns to its state-dependent enablement rather than all becoming blindly enabled.
- Verify that the duplicate Controls group is absent in all modes, Recovery / audit log spans the dashboard width in Advanced/Debug, and Simple hides only that log panel.

## 6. Trading blockers

Induce or configure each practical blocker and verify the Trading label/tooltip:

- disconnected local API state;
- local Gateway socket lost and fixed 10-second reconnect attempts;
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

Turn ATR adaptation off during open RTH and verify the observed bar count/readiness continues to advance while Initial drop, BUY rebound, Minimum profit, SELL trail, and protective settings are not rewritten. Restart the application and verify the live observation buffer begins empty again; a validated same-contract persisted ATR estimate may be restored separately as a starting estimate. It does not restore old ticks as fresh market data.

## 8. BUY order paths

In paper mode with controlled settings:

- positive BUY trail: verify action, type `TRAIL`, trailing percent, stop, quantity, `GTC`, `outsideRth=False`, app order reference, and optional account behavior in TWS/Gateway;
- zero BUY trail: verify the drop condition produces a market BUY;
- slippage buffer: verify quantity is lower/equal compared with unbuffered sizing while the transmitted order type is unchanged;
- partial fill: verify that the first positive fill receives the fixed 3.0-second completion grace, a full multi-print fill inside the grace is not cancelled, a nonterminal remainder is cancelled after timeout, enabled market/session safety deterioration bypasses the grace, and all fills racing cancellation are reconciled before Stage 3;
- what-if: verify the request uses the broker what-if path with `whatIf=True` and `transmit=True`, and that missing/invalid state or absent finite margin output blocks the live BUY;
- invalid price/rejection: verify the retained IBKR code and message appear in Live Strategy/Cycle Audit, the cycle moves to `ERROR`, and no automatic fresh-cycle retry occurs;
- ordinary cancellation: verify `Cancelled`/`ApiCancelled` without a substantive rejection still resets Stage 2 to Stage 1.

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

## 12. Optional Stage-3/Stage-4 liquidation before RTH close

Use a paper account and a liquid U.S. stock with the option disabled first, then enabled with enough time for manual observation.

- Verify the default is OFF and the minutes field defaults to 5, accepts only 1–240, and is disabled while the checkbox is clear.
- Verify Stage 1 and Stage 2 behavior is unchanged and no close action occurs before the configured cutoff.
- In Stage 3, verify the RTH-only market SELL requires a complete independently fresh non-crossed quote within Maximum spread and an executable bid strictly above average BUY (after protective cancel-confirm-replace when applicable). Missing/stale/crossed/over-wide quotes and equal/lower bids must not submit. Confirm commissions are ignored only for eligibility and the fill is not guaranteed profitable.
- With a normal final SELL trail working, verify exactly one cancellation request is sent at the contract-specific cutoff, including on an early-close fixture.
- Verify no market SELL is submitted until TWS/IBKR shows the original trail in a terminal state.
- Fill the trail during the cancellation race and confirm no replacement is sent after a full fill.
- Partially fill the trail, confirm cancellation, and verify the replacement is a SELL `MKT`, `DAY`, `outsideRth=False` for only the app-owned remainder.
- Verify cumulative original/replacement fills, commissions, P/L, Stage 5, Trade History, and Auto-repeat are correct.
- Leave cancellation unconfirmed through the close and verify the original trail remains the only SELL order.
- Confirm cancellation after the close, reject the replacement, and leave a replacement incomplete at the close in separate tests; each must produce an `ERROR`/manual-review state without an outside-RTH fallback.
- Restart once while original cancellation is pending and once while the replacement is working; reconcile before continuing and verify no duplicate replacement or duplicate fill is created.
- While the automatic workflow is active, attempt the Stop-strategy market close and verify the app refuses to start a second market SELL.

## 13. Stop choices

Exercise each option in a safe paper scenario:

- cancel open app orders;
- market-sell local app quantity after cancellation confirmation;
- leave orders working;
- stop after current cycle;
- stop immediately without broker action.

Close the window with and without an active cycle and verify it uses the same stop decision path.

With hard limits enabled, set Maximum completed cycles to 1, complete one BUY/SELL round, and verify auto-repeat stops. Confirm Stop and window-close do not claim an active order or offer market SELL when the persisted app-owned quantity is zero, even if unrelated external shares of the same ticker exist.

## 14. Recovery scenarios

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

### Local API socket loss and indefinite reconnect

After a successful connection, stop the local TWS/Gateway API endpoint or close the platform without clicking Disconnect:

- verify strategy processing and order submission pause immediately;
- record at least five failed reconnect attempts and verify their start times are no closer than 10 seconds;
- verify retries continue beyond any former maximum and do not use exponential backoff;
- restart/log in to the platform and verify reconnection, broker reconciliation, and a new actual market-data event are required before strategy processing resumes;
- repeat, click **Disconnect**, and verify no further automatic attempts occur;
- verify application shutdown also terminates the retry loop.

### Upstream-only Internet outage

In paper mode, induce or simulate a Gateway/TWS upstream outage while keeping the local API socket connected:

- verify the Connection indicator changes to **Gateway only** and code 1100/2110 appears in diagnostics;
- verify waiting stages do not advance, actual-update age increases, and repeated cached fields do not increase the update count or ATR bar history;
- verify app-order polling and every new BUY/SELL submission path remain paused;
- for 1101 restoration, verify a new market-data subscription identity is created;
- for 1102 restoration, verify the existing subscription remains but cached data stays invalid until a new event;
- keep Last unchanged while widening the ask or removing the bid; verify the unchanged Last remains diagnostic, does not become strategy-usable, does not enter ATR, and cannot arm the normal Stage-3 SELL;
- verify one qualifying Stage-3 bid/ask update starts confirmation, a second distinct qualifying quote is required, and any intervening non-quote/incomplete/stale/over-wide/below-trigger event resets it;
- change or age the quote between confirmation and order construction; verify `SELL_MARKET_DATA_REVALIDATION_BLOCKED` is recorded and no broker order is transmitted;
- verify **Reconciling** precedes normal processing and app-owned fills/orders that changed during the outage are imported;
- verify a BUY fill during the outage is not assumed absent and any required protective-order follow-up occurs only after recovery.

## 15. Database and export

- Verify `bot_state.sqlite` and expected generated folders appear beside the app.
- In a new zero-cycle database, select USD then EUR and verify the draft currency can rebind. Create one cycle and verify the currency becomes locked.
- Upgrade a representative v3.1.2 USD database and verify the currency lock is inferred as USD, existing cycle/order/execution values are preserved, and any needed schema additions are additive and idempotent. Repeat with a representative v4.0.0 database.
- Attempt to select/store the opposite currency after a cycle exists and verify it fails closed; use a separate database for the EUR run.
- Inject or simulate a commission in the wrong currency and verify it remains in audit data, is excluded from local net P/L, emits `COMMISSION_CURRENCY_MISMATCH`, and disables Auto-repeat.
- Run through multiple fills and confirm backups are created and `latest_restore_validation.json` reports success.
- Open a backup read-only with SQLite tooling after shutdown and run `PRAGMA integrity_check`.
- Export trade history and inspect columns/UTC timestamps.
- Export an audit bundle and verify manifest, snapshot, database backup, reports, and JSON table exports.
- Confirm sensitive identifiers are present before sharing externally.

## 16. Market-data capture

- Produce a fill and keep the application running through the post-fill window.
- Verify the capture ZIP is written only after completion and contains expected metadata/rows.
- In a separate test, close before completion and verify no partial ZIP is written.

## 17. Full validation and build

- Run `run_all_tests.bat`; require compilation, pytest with `ResourceWarning` failures enabled, at least 75% combined statement/branch coverage, entry coverage for every effective executable application callable, all configured mutation smoke checks, all CSV simulations, Ruff, and Pyright to pass.
- In a paper-account Gateway/TWS session, exercise one exact USD SMART stock and one exact EUR SMART stock. Confirm conId/currency/primary exchange, required order capabilities, RTH metadata, size rules, market data, what-if, BUY, SELL, and commissions. Exercise a price-dependent market rule and confirm the normalized stop is accepted; verify a deliberately rejected app order records the exact broker reason and does not retry.
- Inspect `run_tests_coverage.log` and `run_tests_callable_coverage.log`; do not rely only on the final pass line.
- Preserve `coverage.json` or `coverage.xml` as a release/CI artifact when traceable machine-readable coverage evidence is required.
- Run `build_windows.bat`; verify the final output is not falsely red on success.
- Confirm `build_pyinstaller.log` ends with a successful build and the onedir executable exists.
- Run the packaged application from a clean folder with the complete onedir contents.
- Verify data is created beside the executable and the folder is writable.

## 18. Documentation consistency

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


## v5.0.0 native GUI release checks

Run the complete `run_all_tests.bat` quality gate first, then build on Windows. These visual/integration checks are not claimed as completed by the headless release run.

The separate `.\.venv\Scripts\python.exe scripts\check_v410_gui_real_qt.py --output-dir gui_smoke` check requires real installed PySide6 and ib_async. It starts real widgets, processes mouse events and captures light/dark header and independent-hover screenshots using an inert controller (no worker, Gateway or orders). Missing dependencies cause a nonzero result, not a skip/pass. Review the generated screenshots as well: its pixel-change assertions do not replace the DPI/readability checks below. The root implementation/test report records whether this check ran for the current archive.

- At 1366x768 and 1920x1080, with Windows scaling at 100%, 150% and 200%, verify all ten status boxes are equal in width (apart from one-pixel layout rounding), while the lock stays compact. Use long status messages and large prices/position amounts; confirm wrapping rather than a wider Trading box and readable top-right Trading/Position details, aligned with the top-left title and using the same font size and weight. Their main values must remain below this header. Scroll Live Strategy and switch all tabs: the ten status boxes and lock must remain above the single five-stage ribbon, with both rows above the tabs and update on new snapshots. Confirm light/dark switching still preserves workflow and manual-lock restrictions. Check USD and EUR displays and missing market data.
- Confirm the five monitor cards align, Data mode includes a continuously refreshed age, and RTH status includes UTC and system time. Expand raw fields and stage details independently; resize and repeat show/hide to verify raw Value columns keep filling the width; hiding either must not change strategy progress or permit a blocked action.
- In Simple, confirm only the Live Strategy Recovery / audit log panel is hidden. Advanced/Debug and the Reconciliation tab must remain accessible and existing audit events must still be recorded.
- In Reconciliation, verify all eight comparison rows, including Inconsistency, fit without an internal table scrollbar. Resize from maximized to narrow/short windows, update broker/local data with long descriptions, change theme and test Windows scaling at 100%, 150% and 200%. Columns must use the available width and wrapped rows must refit. When the page is taller than the window, page scrolling must reach the details log and every action. Check the two-by-two guided-action grid and ensure the advanced action box has no clipped text or buttons.
- In Cycle audit Summary, use long field values and narrower dialog widths. Verify row refitting after resize/font/style changes and vertical scrolling when content exceeds the table's height cap. On other compact tables that need a horizontal scrollbar, confirm the last row stays reachable and is not covered by that scrollbar. These are pending native checks; source/Qt-double validation is not a rendered DPI result.
- Use a long real-format OrderRef and verify card-matching background/foreground/proportional font in both themes, no black inner panel, and wrapping, internal vertical scrolling when necessary, full selection/copy, and identical pasted text. Partial fills and late fees must update Total buy cost; a partial SELL reduces only the remaining Position cost, not the history's entry cost.
- Load no rows, a few rows, and at least 500 history rows. Resize vertically, sort Invested in both directions, filter rows, and open audits from sorted rows. Confirm the horizontal scrollbar stays at the visible bottom and can reach every column without first scrolling through all rows. Check 12 summary cards in 3 columns by 4 rows, including Average net P/L immediately after Best and Worst, with losses and with no completed trades.
- In Cycle audit Timeline, hover both graphs, zoom/pan, hover near each visible time boundary, and leave the canvas. Only the hovered plot must draw its local time/price crosshair and tooltip. The other plot must remain unchanged, with no synchronized guide or tooltip. Moving to the other plot must clear the previous plot's overlay; moving through the gap or leaving must clear all hover guides. The underlying market/action data, price ranges and time mapping must remain unchanged by hover. The Summary actions-only chart and other charts must remain independent. Test missing market capture and no action points. Confirm lower tables have aligned top and bottom edges and independent scrolling.
- Check the compact Strategy input map at narrow and wide widths: all sixteen blocks, lane labels and footer must fit without overlapping, with title/value/detail text inside each block.
- In Orders and Executions, test zero, two and many rows, resizing/maximizing after lazy tab opening: columns must span the available width, with OrderRef absorbing spare space and vertical scrolling available.
- In Market capture, test no capture files/rows, a file-only list, and populated captures. The summary must start at the top, summary/preview columns must span the width, and internal scrolling must remain usable without a page-level vertical scrollbar.
- In Decision events, test zero and many rows, maximize/resize the dialog, and confirm the table fills the available tab and Message absorbs spare horizontal width. Check Orders, Executions and Raw log still load lazily; Timeline/Market capture share one background preparation job and Decision cells fill in bounded GUI batches.

No broker order needs to be placed merely to verify these GUI changes. Use synthetic/demo fixtures or an isolated paper account; do not disable production safeguards for a visual check.

## v5.0.0 release gates

Run the complete pytest, coverage, quality and simulation scripts under standard CPython 3.14 on Windows; test the built executable separately. Exercise post-send exceptions, reconnect/restart, colliding order IDs, partial and late SELL fills, shortages, legacy account binding and alternate-ticker Start. Verify protection cancel-confirm-replace across repeated status polls and new quotes, route-rounding rejection, absent protection and cancellation failures. Check size-only/old-generation events, spread master off, stale bid/ask, settings edits, RTH metadata loss and restore rejection. At 100/125/150/200% Windows scaling, check the shorter five-stage ribbon retains its text and the eight Reconciliation rows remain reachable. These are required native/broker checks, not claimed completed host results.

## v5.0.0 audit loading and session evidence

- Open a large audit and immediately select Timeline, Market capture and Decision events: one worker must service all requests; widgets must be created only on the GUI thread. Verify every Decision row/message/tooltip remains available and wrapped heights refit after resizing.
- Close through the button, Escape and window controls during preparation; also destroy the parent before queued callbacks run. Confirm no stale widget access, duplicate job, retained modal cache or broker command.
- Verify a matching fill archive can include same-instrument prices from the next cycle only within its validated window. Reject wrong instrument/contract/currency, unrelated manifest/event and out-of-window context. Verify strict legacy behavior, corrupt archive handling and cancellation releasing ZIP handles.
- Timeline stage markers and table must omit identical/blank stages while preserving all Decision records.
- Unknown disconnected Account displays N/A; known cycle/account values remain visible.
- RTH tests cover US and non-US missing/invalid metadata, missing timezone, holidays, early closes, current/next published sessions and preserved LSE narrowing. No synthetic hours/countdown may appear when authoritative session evidence is absent.
